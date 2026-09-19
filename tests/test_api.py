"""API tests: idempotency, correction chains, revisions, concurrency."""

import os
import tempfile
from concurrent.futures import ThreadPoolExecutor

# Point the DB at a temp file BEFORE importing the app.
_tmpdir = tempfile.mkdtemp(prefix="ledger-test-")
os.environ["LEDGER_DB_PATH"] = os.path.join(_tmpdir, "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)


def test_health():
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_event_and_revision_increments():
    r1 = client.post("/events", json={
        "event_id": "e1", "participant_id": "alice", "points": 10,
    })
    assert r1.status_code == 201
    assert r1.json()["revision"] == 1

    r2 = client.post("/events", json={
        "event_id": "e2", "participant_id": "bob", "points": 7,
    })
    assert r2.status_code == 201
    assert r2.json()["revision"] == 2


def test_idempotent_replay_returns_original():
    payload = {"event_id": "e1", "participant_id": "alice", "points": 10}
    first = client.post("/events", json=payload)
    replay = client.post("/events", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # key order in payload must not matter
    reordered = client.post("/events", json={
        "points": 10, "participant_id": "alice", "event_id": "e1",
    })
    assert reordered.status_code == 200
    assert reordered.json() == first.json()


def test_conflicting_payload_returns_409():
    r = client.post("/events", json={
        "event_id": "e1", "participant_id": "alice", "points": 99,
    })
    assert r.status_code == 409


def test_correction_must_reference_existing_event():
    r = client.post("/events", json={
        "event_id": "bad", "participant_id": "alice", "points": 1,
        "correction_of": "nope",
    })
    assert r.status_code == 422


def test_correction_chain_counts_only_tip():
    # e1: alice +10  -> corrected by e3 (+12) -> corrected by e4 (+15)
    r3 = client.post("/events", json={
        "event_id": "e3", "participant_id": "alice", "points": 12,
        "correction_of": "e1",
    })
    assert r3.status_code == 201
    rev3 = r3.json()["revision"]

    r4 = client.post("/events", json={
        "event_id": "e4", "participant_id": "alice", "points": 15,
        "correction_of": "e3",
    })
    assert r4.status_code == 201
    rev4 = r4.json()["revision"]

    # At rev3 the tip is e3 (12 pts); at rev4 the tip is e4 (15 pts).
    lb3 = client.get(f"/leaderboard?revision={rev3}").json()
    alice3 = next(e for e in lb3["entries"] if e["participant_id"] == "alice")
    assert alice3["total_points"] == 12
    assert [e["event_id"] for e in alice3["effective_events"]] == ["e3"]

    lb4 = client.get(f"/leaderboard?revision={rev4}").json()
    alice4 = next(e for e in lb4["entries"] if e["participant_id"] == "alice")
    assert alice4["total_points"] == 15
    assert [e["event_id"] for e in alice4["effective_events"]] == ["e4"]

    # At revision 1 the original event still counts.
    lb1 = client.get("/leaderboard?revision=1").json()
    alice1 = next(e for e in lb1["entries"] if e["participant_id"] == "alice")
    assert alice1["total_points"] == 10


def test_leaderboard_ordering_and_unknown_revision():
    lb = client.get("/leaderboard").json()
    entries = lb["entries"]
    keys = [(-e["total_points"], e["participant_id"]) for e in entries]
    assert keys == sorted(keys)
    assert [e["rank"] for e in entries] == list(range(1, len(entries) + 1))

    assert client.get("/leaderboard?revision=0").status_code == 404
    assert client.get("/leaderboard?revision=99999").status_code == 404


def test_concurrent_writes_unique_contiguous_revisions():
    before = client.get("/leaderboard").json()["revision"]
    n = 50

    def write(i):
        return client.post("/events", json={
            "event_id": f"conc-{i}", "participant_id": f"p-{i % 5}", "points": i,
        })

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(write, range(n)))

    assert all(r.status_code == 201 for r in results)
    revisions = sorted(r.json()["revision"] for r in results)
    assert revisions == list(range(before + 1, before + n + 1))

    after = client.get("/leaderboard").json()["revision"]
    assert after == before + n
