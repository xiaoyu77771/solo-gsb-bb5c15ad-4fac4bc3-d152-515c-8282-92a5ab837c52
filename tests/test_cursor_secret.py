"""Tests for cursor secret resolution (app/cursor.py).

Covers: explicit LEADERBOARD_CURSOR_SECRET precedence, one-time random
generation persisted next to the database, atomic creation under
concurrent startups, verification of old cursors across restarts, and
fail-fast behaviour (no fallback to any fixed public default).
"""

import base64
import hashlib
import hmac
import json
import os
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app import cursor as cursor_mod
from app.main import app

# The publicly known fallback that used to be baked into the code and
# docker-compose.yml. Tests below pin that it is gone for good.
OLD_PUBLIC_DEFAULT = "dev-only-insecure-cursor-secret"


@pytest.fixture()
def cursor_state(tmp_path, monkeypatch):
    """Isolated data dir, no explicit secret env var, reset secret cache."""
    monkeypatch.delenv(cursor_mod.CURSOR_SECRET_ENV, raising=False)
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "ledger.db"))
    db_module.init_db()
    cursor_mod._cached_secret = None
    yield tmp_path
    cursor_mod._cached_secret = None


def _secret_path(tmp_path):
    return tmp_path / cursor_mod.SECRET_FILE_NAME


def _restart():
    """Simulate a process restart: drop the cached secret, re-resolve."""
    cursor_mod._cached_secret = None
    return cursor_mod.init_cursor_secret()


def _sign(payload, secret):
    """Build a cursor token by hand with an arbitrary secret (forgery)."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    p = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(
        secret.encode() if isinstance(secret, str) else secret,
        p.encode(),
        hashlib.sha256,
    ).digest()
    s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{p}.{s}"


def _seed_and_cursor(client):
    for pid, pts in (("alice", 10), ("bob", 7), ("carol", 5)):
        r = client.post("/events", json={
            "event_id": f"seed-{pid}", "participant_id": pid, "points": pts,
        })
        assert r.status_code == 201
    body = client.get("/leaderboard/page", params={"limit": 1}).json()
    return body["next_cursor"]


# ---------------------------------------------------------------------------
# generation & persistence
# ---------------------------------------------------------------------------

def test_generates_random_secret_file_once(cursor_state):
    secret = cursor_mod.init_cursor_secret()
    path = _secret_path(cursor_state)
    assert path.is_file()
    on_disk = path.read_bytes().strip()
    assert on_disk == secret
    # 256 bits of entropy, hex-encoded; not the old public default
    assert len(on_disk) == 64
    assert all(c in b"0123456789abcdef" for c in on_disk)
    assert on_disk != OLD_PUBLIC_DEFAULT.encode()
    # owner-only permissions
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    # no leftover temp files from the atomic create
    assert [p.name for p in cursor_state.iterdir() if p.name.startswith(".cursor-secret-")] == []


def test_no_hardcoded_fallback_remains():
    assert not hasattr(cursor_mod, "_DEV_SECRET")


def test_distinct_data_dirs_get_distinct_secrets(cursor_state, tmp_path_factory):
    first = cursor_mod.init_cursor_secret()
    other_dir = tmp_path_factory.mktemp("other")
    other_path = str(other_dir / "ledger.db")
    original = db_module.DB_PATH
    db_module.DB_PATH = other_path
    try:
        db_module.init_db()
        cursor_mod._cached_secret = None
        second = cursor_mod.init_cursor_secret()
    finally:
        db_module.DB_PATH = original
    assert first != second


def test_concurrent_creation_converges_on_one_secret(cursor_state):
    """Many threads racing the first-boot creation share a single value."""
    path = str(_secret_path(cursor_state))
    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(
            lambda _: cursor_mod._load_or_create_file_secret(path), range(64)
        ))
    assert len(set(results)) == 1
    assert results[0] == _secret_path(cursor_state).read_bytes().strip()
    # no leftover temp files after the race
    assert [p for p in cursor_state.iterdir() if p.name.startswith(".cursor-secret-")] == []


def test_concurrent_init_cursor_secret(cursor_state):
    with ThreadPoolExecutor(max_workers=16) as pool:
        secrets = list(pool.map(lambda _: cursor_mod.init_cursor_secret(), range(32)))
    assert len(set(secrets)) == 1
    assert secrets[0] == _secret_path(cursor_state).read_bytes().strip()


# ---------------------------------------------------------------------------
# restart & rotation
# ---------------------------------------------------------------------------

def test_old_cursor_still_verifies_after_restart(cursor_state):
    cursor_mod.init_cursor_secret()
    client = TestClient(app)
    cursor = _seed_and_cursor(client)

    _restart()  # same data dir: secret reloaded from disk

    r = client.get("/leaderboard/page", params={"cursor": cursor, "limit": 2})
    assert r.status_code == 200
    assert [e["participant_id"] for e in r.json()["entries"]] == ["bob", "carol"]


def test_secret_file_survives_restart_identically(cursor_state):
    first = cursor_mod.init_cursor_secret()
    assert _restart() == first
    assert _restart() == first


def test_deleted_secret_file_rotates_and_invalidates_old_cursors(cursor_state):
    old_secret = cursor_mod.init_cursor_secret()
    client = TestClient(app)
    cursor = _seed_and_cursor(client)

    _secret_path(cursor_state).unlink()  # lost/rotated secret
    new_secret = _restart()

    assert new_secret != old_secret  # regenerated, not recovered
    r = client.get("/leaderboard/page", params={"cursor": cursor})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# forgery
# ---------------------------------------------------------------------------

def test_cursor_forged_with_old_public_default_rejected(cursor_state):
    cursor_mod.init_cursor_secret()
    client = TestClient(app)
    _seed_and_cursor(client)
    forged = _sign({"v": 1, "rev": 1, "pts": 10, "pid": "alice"}, OLD_PUBLIC_DEFAULT)
    r = client.get("/leaderboard/page", params={"cursor": forged})
    assert r.status_code == 422


def test_cursor_forged_with_guessed_secrets_rejected(cursor_state):
    cursor_mod.init_cursor_secret()
    client = TestClient(app)
    _seed_and_cursor(client)
    for guess in ("secret", "password", "cursor", "x" * 64):
        forged = _sign({"v": 1, "rev": 1, "pts": 10, "pid": "alice"}, guess)
        r = client.get("/leaderboard/page", params={"cursor": forged})
        assert r.status_code == 422, guess


def test_attacker_cannot_mint_cursor_for_arbitrary_revision(cursor_state):
    """A self-signed cursor pinning an older revision must not verify."""
    cursor_mod.init_cursor_secret()
    client = TestClient(app)
    _seed_and_cursor(client)
    forged = _sign({"v": 1, "rev": 1, "pts": 5, "pid": "carol"}, OLD_PUBLIC_DEFAULT)
    r = client.get("/leaderboard/page", params={"cursor": forged})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# explicit configuration
# ---------------------------------------------------------------------------

def test_explicit_env_secret_wins_and_touches_no_file(cursor_state, monkeypatch):
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, "explicit-secret")
    assert cursor_mod.init_cursor_secret() == b"explicit-secret"
    assert not _secret_path(cursor_state).exists()


def test_explicit_env_secret_wins_over_existing_file(cursor_state, monkeypatch):
    file_secret = cursor_mod.init_cursor_secret()  # generate on-disk secret first
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, "explicit-secret")
    assert _restart() == b"explicit-secret"
    assert _secret_path(cursor_state).read_bytes().strip() == file_secret  # untouched

    client = TestClient(app)
    cursor = _seed_and_cursor(client)
    # signed with the env secret: verifies
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 200
    # signed with the on-disk secret: rejected while the env var is set
    forged = _sign({"v": 1, "rev": 3, "pts": 7, "pid": "bob"}, file_secret)
    assert client.get("/leaderboard/page", params={"cursor": forged}).status_code == 422


def test_empty_env_secret_is_treated_as_unset(cursor_state, monkeypatch):
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, "")
    secret = cursor_mod.init_cursor_secret()
    assert secret == _secret_path(cursor_state).read_bytes().strip()


# ---------------------------------------------------------------------------
# failure modes: never fall back to a fixed value
# ---------------------------------------------------------------------------

def test_unreadable_secret_file_fails(cursor_state):
    _secret_path(cursor_state).mkdir()  # a directory where the file should be
    with pytest.raises(cursor_mod.CursorSecretError):
        cursor_mod.init_cursor_secret()


def test_empty_secret_file_fails(cursor_state):
    _secret_path(cursor_state).write_bytes(b"")
    with pytest.raises(cursor_mod.CursorSecretError):
        cursor_mod.init_cursor_secret()


def test_whitespace_only_secret_file_fails(cursor_state):
    _secret_path(cursor_state).write_bytes(b"  \n")
    with pytest.raises(cursor_mod.CursorSecretError):
        cursor_mod.init_cursor_secret()


def test_uncreatable_data_dir_fails(cursor_state, monkeypatch):
    blocker = cursor_state / "not-a-dir"
    blocker.write_text("occupied")  # a file where the DB directory should be
    monkeypatch.setattr(db_module, "DB_PATH", str(blocker / "ledger.db"))
    with pytest.raises(cursor_mod.CursorSecretError):
        cursor_mod.init_cursor_secret()


def test_startup_aborts_when_secret_unresolvable(cursor_state):
    _secret_path(cursor_state).mkdir()  # break secret resolution
    with pytest.raises(cursor_mod.CursorSecretError):
        with TestClient(app):  # lifespan runs init_db + init_cursor_secret
            pass


def test_startup_aborts_instead_of_using_old_default(cursor_state, monkeypatch):
    """Even with the old public default in the environment... it is just a
    regular explicit secret; the broken file state must not be consulted."""
    _secret_path(cursor_state).mkdir()
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, OLD_PUBLIC_DEFAULT)
    # explicit env still works (file never consulted) ...
    assert cursor_mod.init_cursor_secret() == OLD_PUBLIC_DEFAULT.encode()
    # ... but without it, the broken state fails rather than silently
    # falling back to that same public value
    monkeypatch.delenv(cursor_mod.CURSOR_SECRET_ENV)
    with pytest.raises(cursor_mod.CursorSecretError):
        cursor_mod.init_cursor_secret()
