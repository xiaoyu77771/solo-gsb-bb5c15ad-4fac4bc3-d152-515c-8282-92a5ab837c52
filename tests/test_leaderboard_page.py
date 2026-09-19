"""Tests for GET /leaderboard/page: HMAC-signed cursor pagination."""

import base64
import hashlib
import hmac
import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app import cursor as cursor_mod
from app.main import app

SECRET = "test-cursor-secret"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file and a known cursor secret."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "page-test.db"))
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, SECRET)
    db_module.init_db()
    return TestClient(app)


def _post(client, event_id, participant_id, points, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    r = client.post("/events", json=payload)
    assert r.status_code == 201
    return r.json()


def _seed(client, totals):
    """One event per participant; returns the resulting latest revision."""
    revision = 0
    for pid, pts in totals.items():
        revision = _post(client, f"seed-{pid}", pid, pts)["revision"]
    return revision


def _walk(client, limit, **first_params):
    """Follow next_cursor until the last page; returns (pages, all_entries)."""
    pages, entries = [], []
    params = {"limit": limit, **first_params}
    for _ in range(100):
        r = client.get("/leaderboard/page", params=params)
        assert r.status_code == 200
        body = r.json()
        pages.append(body)
        entries.extend(body["entries"])
        cursor = body["next_cursor"]
        if cursor is None:
            return pages, entries
        params = {"limit": limit, "cursor": cursor}
    raise AssertionError("pagination did not terminate")


def _sign(payload, secret=SECRET):
    """Build a cursor token by hand (for forging version/field variants)."""
    raw = json.dumps(payload, separators=(",", ":")).encode()
    p = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    sig = hmac.new(secret.encode(), p.encode(), hashlib.sha256).digest()
    s = base64.urlsafe_b64encode(sig).rstrip(b"=").decode()
    return f"{p}.{s}"


def test_first_page_defaults_to_latest_revision(client):
    rev = _seed(client, {"alice": 10, "bob": 7, "carol": 7})
    r = client.get("/leaderboard/page", params={"limit": 2})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == rev
    assert [(e["participant_id"], e["total_points"]) for e in body["entries"]] == [
        ("alice", 10),
        ("bob", 7),  # tie with carol broken by participant_id ascending
    ]
    assert [e["rank"] for e in body["entries"]] == [1, 2]
    assert body["next_cursor"] is not None
    # effective event details are included, as in /leaderboard
    assert body["entries"][0]["effective_events"][0]["event_id"] == "seed-alice"


def test_walk_all_pages_in_order_with_ties_and_negatives(client):
    _seed(client, {"p-a": 10, "p-b": 10, "p-c": 0, "p-d": -5, "p-e": 10})
    pages, entries = _walk(client, limit=2)
    assert [e["participant_id"] for e in entries] == ["p-a", "p-b", "p-e", "p-c", "p-d"]
    assert [e["rank"] for e in entries] == [1, 2, 3, 4, 5]
    assert len(pages) == 3
    assert pages[-1]["next_cursor"] is None
    assert all(p["revision"] == pages[0]["revision"] for p in pages)


def test_exact_multiple_of_limit_ends_with_null_cursor(client):
    _seed(client, {"a": 1, "b": 2, "c": 3, "d": 4})
    pages, entries = _walk(client, limit=2)
    assert len(pages) == 2
    assert len(entries) == 4
    assert pages[-1]["next_cursor"] is None


def test_empty_leaderboard_returns_empty_page(client):
    r = client.get("/leaderboard/page", params={"limit": 5})
    assert r.status_code == 200
    assert r.json() == {"revision": 0, "entries": [], "next_cursor": None}


def test_cursor_payload_contents(client):
    _seed(client, {"alice": 10, "bob": 7})
    body = client.get("/leaderboard/page", params={"limit": 1}).json()
    payload_b64, _sig = body["next_cursor"].split(".")
    padded = payload_b64 + "=" * (-len(payload_b64) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    assert payload == {"v": 1, "rev": 2, "pts": 10, "pid": "alice"}


def test_cursor_pins_revision_against_concurrent_writes(client):
    _seed(client, {"alice": 10, "bob": 7, "carol": 5})
    first = client.get("/leaderboard/page", params={"limit": 1}).json()
    cursor = first["next_cursor"]
    expected = client.get(
        "/leaderboard/page", params={"cursor": cursor, "limit": 2}
    ).json()

    def write(i):
        return client.post("/events", json={
            "event_id": f"later-{i}", "participant_id": "alice", "points": 100,
        })

    def read(_):
        r = client.get("/leaderboard/page", params={"cursor": cursor, "limit": 2})
        assert r.status_code == 200
        return r.json()

    with ThreadPoolExecutor(max_workers=8) as pool:
        writes = [pool.submit(write, i) for i in range(10)]
        reads = [pool.submit(read, i) for i in range(10)]
        assert all(f.result().status_code == 201 for f in writes)
        bodies = [f.result() for f in reads]

    # every concurrent read resumed from the cursor's pinned snapshot
    assert all(b == expected for b in bodies)
    assert expected["revision"] == first["revision"]
    # ...while the live leaderboard moved on
    assert client.get("/leaderboard").json()["revision"] == first["revision"] + 10


def test_explicit_revision_pins_snapshot(client):
    _seed(client, {"alice": 10, "bob": 7})  # revision 2
    _post(client, "extra", "alice", 100)  # revision 3
    pages, entries = _walk(client, 1, revision=2)
    assert [e["participant_id"] for e in entries] == ["alice", "bob"]
    assert entries[0]["total_points"] == 10
    assert all(p["revision"] == 2 for p in pages)


def test_cursor_with_matching_explicit_revision_ok(client):
    _seed(client, {"alice": 10, "bob": 7})
    first = client.get("/leaderboard/page", params={"limit": 1}).json()
    r = client.get("/leaderboard/page", params={
        "cursor": first["next_cursor"], "revision": first["revision"],
    })
    assert r.status_code == 200
    assert [e["participant_id"] for e in r.json()["entries"]] == ["bob"]


def test_cursor_conflicting_explicit_revision_422(client):
    _seed(client, {"alice": 10, "bob": 7, "carol": 5})
    first = client.get("/leaderboard/page", params={"limit": 1}).json()
    cursor = first["next_cursor"]
    for bad in (first["revision"] + 1, first["revision"] - 1):
        r = client.get("/leaderboard/page", params={"cursor": cursor, "revision": bad})
        assert r.status_code == 422


def test_malformed_cursors_422(client):
    _seed(client, {"alice": 10})
    for bad in ["", "not-a-cursor", "a.b.c", ".", "abc.", ".abc", "é.é"]:
        r = client.get("/leaderboard/page", params={"cursor": bad})
        assert r.status_code == 422, bad


def test_tampered_cursor_422(client):
    _seed(client, {"alice": 10, "bob": 7})
    cursor = client.get("/leaderboard/page", params={"limit": 1}).json()["next_cursor"]
    payload_b64, sig = cursor.split(".")

    # flip a signature character
    flipped = sig[:-1] + ("A" if sig[-1] != "A" else "B")
    r = client.get("/leaderboard/page", params={"cursor": f"{payload_b64}.{flipped}"})
    assert r.status_code == 422

    # flip a payload character (signature no longer matches)
    mid = len(payload_b64) // 2
    ch = "A" if payload_b64[mid] != "A" else "B"
    tampered = payload_b64[:mid] + ch + payload_b64[mid + 1:]
    r = client.get("/leaderboard/page", params={"cursor": f"{tampered}.{sig}"})
    assert r.status_code == 422


def test_cursor_signed_with_other_secret_422(client, monkeypatch):
    _seed(client, {"alice": 10, "bob": 7})
    cursor = client.get("/leaderboard/page", params={"limit": 1}).json()["next_cursor"]
    monkeypatch.setenv(cursor_mod.CURSOR_SECRET_ENV, "another-secret")
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 422


def test_cursor_wrong_version_422(client):
    _seed(client, {"alice": 10})
    token = _sign({"v": 2, "rev": 1, "pts": 10, "pid": "alice"})
    assert client.get("/leaderboard/page", params={"cursor": token}).status_code == 422


def test_cursor_invalid_fields_422(client):
    _seed(client, {"alice": 10})
    bad_payloads = [
        {"v": 1, "rev": 0, "pts": 10, "pid": "alice"},   # rev < 1
        {"v": 1, "rev": 1, "pts": "10", "pid": "alice"},  # pts not an int
        {"v": 1, "rev": 1, "pts": 10},                    # missing pid
        {"v": 1, "rev": 1, "pts": 10, "pid": ""},         # empty pid
    ]
    for payload in bad_payloads:
        r = client.get("/leaderboard/page", params={"cursor": _sign(payload)})
        assert r.status_code == 422, payload


def test_cursor_revision_no_longer_exists_404(client, tmp_path, monkeypatch):
    _seed(client, {"alice": 10, "bob": 7})
    cursor = client.get("/leaderboard/page", params={"limit": 1}).json()["next_cursor"]
    # point the service at a fresh, empty database
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "reset.db"))
    db_module.init_db()
    assert client.get("/leaderboard/page", params={"cursor": cursor}).status_code == 404


def test_invalid_limit_422(client):
    assert client.get("/leaderboard/page", params={"limit": 0}).status_code == 422
    assert client.get("/leaderboard/page", params={"limit": 501}).status_code == 422
    assert client.get("/leaderboard/page", params={"limit": "abc"}).status_code == 422


def test_unknown_revision_404(client):
    _seed(client, {"alice": 10})
    assert client.get("/leaderboard/page", params={"revision": 0}).status_code == 404
    assert client.get("/leaderboard/page", params={"revision": 99}).status_code == 404


def test_correction_chain_reflected_in_pages(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 8)
    _post(client, "e3", "alice", 15, correction_of="e1")
    body = client.get("/leaderboard/page", params={"limit": 10}).json()
    alice = next(e for e in body["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == 15
    assert [ev["event_id"] for ev in alice["effective_events"]] == ["e3"]
    assert body["next_cursor"] is None
