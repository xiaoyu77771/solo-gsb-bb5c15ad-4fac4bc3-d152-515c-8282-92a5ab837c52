"""Cursor secret resolution: persisted random secret instead of a fixed default.

Covers:
* no env var -> a random secret is generated once in the data directory,
  and cursors signed with the old public default (or any other guess)
  are rejected (forgery regression);
* atomic shared initialization under thread and process concurrency;
* restart persistence: old cursors keep validating;
* explicit ``LEADERBOARD_CURSOR_SECRET`` always takes precedence and
  rotating it invalidates old cursors (regression for explicit config);
* empty env var behaves like unset;
* missing/unwritable data dir, empty/unreadable secret file abort
  startup rather than falling back to a fixed default.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app import cursor as cursor_mod
from app.main import app

OLD_PUBLIC_DEFAULT = "dev-only-insecure-cursor-secret"
WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _isolated_secret(tmp_path, monkeypatch):
    """Fresh data dir per test, no env secret, empty in-memory cache."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "secret-test.db"))
    monkeypatch.delenv(cursor_mod.CURSOR_SECRET_ENV, raising=False)
    cursor_mod._reset_cached_secret()
    yield
    cursor_mod._reset_cached_secret()


def _client():
    # mirror the lifespan: cursor secret before DB init
    cursor_mod.init_cursor_secret()
    db_module.init_db()
    return TestClient(app)


def _secret_path() -> str:
    return cursor_mod._secret_path()


def _post(client, event_id, participant_id, points):
    r = client.post("/events", json={
        "event_id": event_id, "participant_id": participant_id, "points": points,
    })
    assert r.status_code == 201
    return r.json()


def _first_cursor(client):
    return client.get("/leaderboard/page", params={"limit": 1}).json()["next_cursor"]


def _sign(payload, secret):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    p = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest()
    s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{p}.{s}"


# ---------------------------------------------------------------------------
# Auto-generation
# ---------------------------------------------------------------------------

def test_no_env_generates_persisted_random_secret_file():
    assert not os.path.exists(_secret_path())
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)

    path = _secret_path()
    assert os.path.exists(path)
    st = os.stat(path)
    # random 32 bytes hex-encoded, owner-only access
    assert stat.S_ISREG(st.st_mode)
    assert stat.S_IMODE(st.st_mode) == 0o600
    with open(path, "rb") as fh:
        on_disk = fh.read().strip()
    assert len(on_disk) == 64
    assert bytes.fromhex(on_disk.decode())  # valid hex

    # the issued cursor verifies against the on-disk secret
    payload_b64, sig = cursor.split(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(on_disk, payload_b64.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert sig == expected


def test_no_temp_files_left_behind():
    _client()
    leftovers = [
        n for n in os.listdir(os.path.dirname(_secret_path()))
        if n.startswith(f".{cursor_mod.CURSOR_SECRET_FILENAME}.")
    ]
    assert leftovers == []


def test_secret_is_not_the_public_default():
    _client()
    with open(_secret_path(), "rb") as fh:
        assert fh.read().strip() != OLD_PUBLIC_DEFAULT.encode()


def test_empty_env_var_is_treated_as_unset():
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = ""
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    assert os.path.exists(_secret_path())
    cursor = _first_cursor(client)
    assert client.get(
        "/leaderboard/page", params={"cursor": cursor}
    ).status_code == 200


# ---------------------------------------------------------------------------
# Forgery: clients cannot mint their own cursors
# ---------------------------------------------------------------------------

def test_cursor_signed_with_old_public_default_422():
    client = _client()
    _post(client, "e1", "alice", 10)
    forged = _sign(
        {"v": 1, "rev": 1, "pts": 10, "pid": "alice"}, OLD_PUBLIC_DEFAULT
    )
    assert client.get("/leaderboard/page", params={"cursor": forged}).status_code == 422


def test_cursor_signed_with_arbitrary_guess_422():
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # client mints a cursor that rewinds to revision 1 and an arbitrary key
    forged = _sign(
        {"v": 1, "rev": 1, "pts": 999999, "pid": "attacker"}, "hunter2"
    )
    r = client.get("/leaderboard/page", params={"cursor": forged})
    assert r.status_code == 422


def test_generated_secret_differs_between_data_dirs(tmp_path, monkeypatch):
    _client()
    first = open(_secret_path(), "rb").read()

    other = tmp_path / "other"
    monkeypatch.setattr(db_module, "DB_PATH", str(other / "db.sqlite"))
    cursor_mod._reset_cached_secret()
    cursor_mod.init_cursor_secret()
    second = open(cursor_mod._secret_path(), "rb").read()
    assert first != second


# ---------------------------------------------------------------------------
# Concurrent initialization
# ---------------------------------------------------------------------------

def test_concurrent_threads_share_one_secret_file():
    n = 16
    barrier_paths = []

    def load_one(_):
        return cursor_mod._load_file_secret()

    with ThreadPoolExecutor(max_workers=n) as pool:
        secrets_ = [pool.submit(load_one, i) for i in range(n)]
        values = {f.result() for f in secrets_}

    assert len(values) == 1
    assert values.pop() == open(_secret_path(), "rb").read().strip()
    # exactly one file, no leftovers
    names = os.listdir(os.path.dirname(_secret_path()))
    assert names.count(cursor_mod.CURSOR_SECRET_FILENAME) == 1
    assert not [n for n in names if n.startswith(".")]


def test_loser_keeps_existing_secret_when_file_appears_mid_race(monkeypatch):
    """If the file is published between read and create, its value wins."""
    real_create = cursor_mod._create_secret_file

    def racer(path):
        # another process publishes its secret first
        with open(path, "wb") as fh:
            fh.write(b"a" * 64)
        real_create(path)  # link fails with FileExistsError -> keep racer's

    monkeypatch.setattr(cursor_mod, "_create_secret_file", racer)
    assert cursor_mod._load_file_secret() == b"a" * 64


def test_concurrent_processes_share_one_secret(tmp_path):
    data_dir = tmp_path / "procs"
    db_path = data_dir / "proc.db"
    script = (
        "import os; "
        "from app.cursor import init_cursor_secret, _secret_path; "
        "init_cursor_secret(); "
        "print(open(_secret_path(), 'rb').read().strip().decode())"
    )
    env = {
        **os.environ,
        "LEDGER_DB_PATH": str(db_path),
        "PYTHONPATH": WORKSPACE,
    }
    env.pop(cursor_mod.CURSOR_SECRET_ENV, None)

    procs = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            cwd=WORKSPACE, env=env, stdout=subprocess.PIPE,
        )
        for _ in range(6)
    ]
    outputs = []
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0, out
        outputs.append(out.decode().strip())

    assert len(set(outputs)) == 1
    secret_file = data_dir / cursor_mod.CURSOR_SECRET_FILENAME
    assert secret_file.read_bytes().strip().decode() == outputs[0]


# ---------------------------------------------------------------------------
# Restart persistence
# ---------------------------------------------------------------------------

def test_restart_reuses_secret_and_keeps_validating_old_cursors():
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)
    secret_on_disk = open(_secret_path(), "rb").read()

    # simulate a fresh process: drop cached secret, resolve again from disk
    cursor_mod._reset_cached_secret()
    cursor_mod.init_cursor_secret()
    assert open(_secret_path(), "rb").read() == secret_on_disk  # not regenerated

    client2 = _client()
    r = client2.get("/leaderboard/page", params={"cursor": cursor})
    assert r.status_code == 200
    assert [e["participant_id"] for e in r.json()["entries"]] == ["bob"]


def test_restart_works_through_full_lifespan():
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)

    cursor_mod._reset_cached_secret()
    with TestClient(app) as restarted:  # lifespan runs init_cursor_secret()
        r = restarted.get("/leaderboard/page", params={"cursor": cursor})
        assert r.status_code == 200
        assert r.json()["entries"][0]["participant_id"] == "bob"


def test_secret_file_survives_unrelated_db_files():
    # secret lives beside the DB; db init ordering must not clobber it
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)
    before = open(_secret_path(), "rb").read()

    # WAL/SHM side files appear during writes; secret stays intact
    assert open(_secret_path(), "rb").read() == before
    cursor_mod._reset_cached_secret()
    assert _client().get(
        "/leaderboard/page", params={"cursor": cursor}
    ).status_code == 200


# ---------------------------------------------------------------------------
# Explicit configuration regressions
# ---------------------------------------------------------------------------

def test_explicit_env_secret_takes_precedence_over_file(tmp_path):
    # pre-existing generated file must be ignored when env is configured
    os.makedirs(os.path.dirname(_secret_path()), exist_ok=True)
    with open(_secret_path(), "wb") as fh:
        fh.write(b"b" * 64)
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = "explicit-secret"
    cursor_mod._reset_cached_secret()

    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)

    payload_b64, sig = cursor.split(".")
    expected = base64.urlsafe_b64encode(
        hmac.new(b"explicit-secret", payload_b64.encode(), hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    assert sig == expected
    # file is untouched, not regenerated
    assert open(_secret_path(), "rb").read() == b"b" * 64


def test_explicit_env_works_even_without_writable_data_dir(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")  # a file where the data dir would be
    monkeypatch.setattr(db_module, "DB_PATH", str(blocker / "db" / "x.db"))
    cursor_mod._reset_cached_secret()
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = "env-only"
    # no data dir access at all when env is explicit
    cursor_mod.init_cursor_secret()  # must not raise


def test_rotating_explicit_secret_invalidates_old_cursors():
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = "secret-one"
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 200

    # rotate: old cursors stop validating, new ones work
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = "secret-two"
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 422
    new_cursor = _first_cursor(client)
    assert client.get(
        "/leaderboard/page", params={"cursor": new_cursor}
    ).status_code == 200


def test_switching_env_secret_to_generated_file_secret_invalidates_cursors():
    os.environ[cursor_mod.CURSOR_SECRET_ENV] = "temp-secret"
    client = _client()
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    cursor = _first_cursor(client)

    del os.environ[cursor_mod.CURSOR_SECRET_ENV]
    cursor_mod._reset_cached_secret()  # restart without env -> file secret
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 422


# ---------------------------------------------------------------------------
# Failure modes: startup must fail, never fall back to a fixed secret
# ---------------------------------------------------------------------------

def test_startup_fails_when_data_dir_cannot_be_created(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"not-a-directory")
    monkeypatch.setattr(db_module, "DB_PATH", str(blocker / "sub" / "x.db"))
    cursor_mod._reset_cached_secret()
    with pytest.raises(RuntimeError, match="cursor secret"):
        cursor_mod.init_cursor_secret()


def test_startup_fails_on_empty_secret_file():
    client = _client()
    _post(client, "e1", "alice", 10)
    with open(_secret_path(), "wb"):
        pass  # truncate to zero bytes
    cursor_mod._reset_cached_secret()
    with pytest.raises(RuntimeError, match="empty"):
        cursor_mod.init_cursor_secret()


def test_startup_fails_on_whitespace_only_secret_file():
    _client()
    with open(_secret_path(), "wb") as fh:
        fh.write(b"  \n\t \n")
    cursor_mod._reset_cached_secret()
    with pytest.raises(RuntimeError, match="empty"):
        cursor_mod.init_cursor_secret()


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses file permission checks"
)
def test_startup_fails_when_secret_file_unreadable():
    _client()
    os.chmod(_secret_path(), 0)
    try:
        cursor_mod._reset_cached_secret()
        with pytest.raises(RuntimeError, match="cannot read cursor secret"):
            cursor_mod.init_cursor_secret()
    finally:
        os.chmod(_secret_path(), 0o600)


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root bypasses directory permission checks"
)
def test_startup_fails_when_data_dir_unwritable(tmp_path, monkeypatch):
    ro = tmp_path / "readonly"
    ro.mkdir()
    os.chmod(ro, 0o500)
    try:
        monkeypatch.setattr(db_module, "DB_PATH", str(ro / "x.db"))
        cursor_mod._reset_cached_secret()
        with pytest.raises(RuntimeError, match="cursor secret"):
            cursor_mod.init_cursor_secret()
    finally:
        os.chmod(ro, 0o700)


def test_lifespan_aborts_when_secret_unusable(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_bytes(b"")
    monkeypatch.setattr(db_module, "DB_PATH", str(blocker / "sub" / "x.db"))
    cursor_mod._reset_cached_secret()
    with pytest.raises(RuntimeError, match="cursor secret"):
        with TestClient(app):
            pass  # startup failure: never serves a request with a fallback secret
