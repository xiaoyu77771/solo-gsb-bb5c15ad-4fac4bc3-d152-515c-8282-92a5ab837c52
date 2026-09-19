"""Tests for POST /events/batch: atomic, ordered, revisioned writes."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "batch-test.db"))
    db_module.init_db()
    return TestClient(app)


def _ev(event_id, participant_id="alice", points=1, correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    return payload


def _post_batch(client, events):
    return client.post("/events/batch", json=events)


def test_batch_creates_events_with_contiguous_revisions_in_order(client):
    events = [_ev("b1", "alice", 10), _ev("b2", "bob", 5), _ev("b3", "carol", 7)]
    r = _post_batch(client, events)
    assert r.status_code == 201
    body = r.json()
    assert [e["event_id"] for e in body] == ["b1", "b2", "b3"]
    assert [e["revision"] for e in body] == [1, 2, 3]

    # revisions continue strictly after the previous block
    more = [_ev("b4"), _ev("b5")]
    r3 = _post_batch(client, more)
    assert r3.status_code == 201
    assert [e["event_id"] for e in r3.json()] == ["b4", "b5"]
    assert [e["revision"] for e in r3.json()] == [4, 5]
    assert client.get("/leaderboard").json()["revision"] == 5


def test_single_event_in_batch_ok(client):
    r = _post_batch(client, [_ev("s1", points=3)])
    assert r.status_code == 201
    assert r.json()[0]["revision"] == 1


def test_empty_batch_422(client):
    assert _post_batch(client, []).status_code == 422


def test_batch_over_100_events_422(client):
    events = [_ev(f"too-many-{i}") for i in range(101)]
    assert _post_batch(client, events).status_code == 422


def test_batch_of_exactly_100_events_ok(client):
    events = [_ev(f"hundred-{i}", points=i) for i in range(100)]
    r = _post_batch(client, events)
    assert r.status_code == 201
    assert [e["revision"] for e in r.json()] == list(range(1, 101))


def test_non_array_body_422(client):
    assert client.post("/events/batch", json={"event_id": "x"}).status_code == 422
    assert client.post("/events/batch", json="nope").status_code == 422


def test_duplicate_event_ids_within_batch_422(client):
    events = [_ev("dup"), _ev("other"), _ev("dup", points=9)]
    r = _post_batch(client, events)
    assert r.status_code == 422
    # nothing stored, no revision consumed
    assert client.get("/events").json() == []
    assert client.get("/leaderboard").json()["revision"] == 0


def test_self_referencing_event_in_batch_422(client):
    r = _post_batch(client, [_ev("self", correction_of="self")])
    assert r.status_code == 422
    assert client.get("/events").json() == []


def test_correction_of_earlier_event_in_same_batch_ok(client):
    events = [
        _ev("c1", "alice", 10),
        _ev("c2", "alice", 12, correction_of="c1"),
        _ev("c3", "alice", 15, correction_of="c2"),
    ]
    r = _post_batch(client, events)
    assert r.status_code == 201
    body = r.json()
    assert [e["revision"] for e in body] == [1, 2, 3]
    assert body[2]["correction_of"] == "c2"
    lb = client.get("/leaderboard").json()
    alice = next(e for e in lb["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == 15
    assert [e["event_id"] for e in alice["effective_events"]] == ["c3"]


def test_correction_of_references_ledger_event(client):
    assert client.post("/events", json=_ev("old", "bob", 4)).status_code == 201
    r = _post_batch(client, [_ev("new", "bob", 6, correction_of="old")])
    assert r.status_code == 201
    assert r.json()[0]["revision"] == 2


def test_correction_of_later_event_in_same_batch_422(client):
    events = [
        _ev("earlier", "alice", 10, correction_of="later"),
        _ev("later", "alice", 12),
    ]
    r = _post_batch(client, events)
    assert r.status_code == 422
    # the whole batch is rejected, including the later, valid-looking item
    assert client.get("/events").json() == []
    assert client.get("/leaderboard").json()["revision"] == 0


def test_correction_of_unknown_event_422(client):
    r = _post_batch(client, [_ev("x", correction_of="ghost")])
    assert r.status_code == 422
    assert client.get("/events").json() == []


def test_later_bad_reference_does_not_store_earlier_items(client):
    # second item references a non-existent id; the first insert must roll back
    events = [_ev("good", "alice", 10), _ev("bad", "bob", 1, correction_of="ghost")]
    assert _post_batch(client, events).status_code == 422
    assert client.get("/events").json() == []
    assert client.get("/events/good").status_code == 404


def test_full_replay_returns_200_original_results_in_order_no_new_revision(client):
    events = [_ev("r1", "alice", 10), _ev("r2", "bob", 5)]
    first = _post_batch(client, events)
    assert first.status_code == 201

    replay = _post_batch(client, events)
    assert replay.status_code == 200
    assert replay.json() == first.json()  # original created_at/revision preserved
    assert [e["revision"] for e in replay.json()] == [1, 2]
    assert client.get("/leaderboard").json()["revision"] == 2

    # same set, different request order: results still follow request order
    reordered = _post_batch(client, [events[1], events[0]])
    assert reordered.status_code == 200
    assert [e["event_id"] for e in reordered.json()] == ["r2", "r1"]
    assert [e["revision"] for e in reordered.json()] == [2, 1]
    assert client.get("/leaderboard").json()["revision"] == 2


def test_replay_payload_key_order_insensitive_200(client):
    first = _post_batch(client, [_ev("k1", "alice", 10)])
    assert first.status_code == 201
    replay = client.post(
        "/events/batch",
        json=[{"points": 10, "event_id": "k1", "participant_id": "alice"}],
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_same_id_different_payload_409(client):
    assert _post_batch(client, [_ev("m1", "alice", 10)]).status_code == 201
    r = _post_batch(client, [_ev("m1", "alice", 99)])
    assert r.status_code == 409
    # original payload intact
    assert client.get("/events/m1").json()["points"] == 10


def test_conflicting_id_alongside_other_items_rolls_back_whole_batch(client):
    assert _post_batch(client, [_ev("m1", "alice", 10)]).status_code == 201
    events = [_ev("fresh-1"), _ev("m1", "alice", 99), _ev("fresh-2")]
    r = _post_batch(client, events)
    assert r.status_code == 409
    # fresh items must not have been stored, no revisions consumed
    assert client.get("/events/fresh-1").status_code == 404
    assert client.get("/events/fresh-2").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 1


def test_partial_replay_409(client):
    assert _post_batch(client, [_ev("p1", "alice", 10)]).status_code == 201
    # one existing id (identical payload) plus new ids: partial replay -> 409
    r = _post_batch(client, [_ev("p1", "alice", 10), _ev("p2"), _ev("p3")])
    assert r.status_code == 409
    assert client.get("/events/p2").status_code == 404
    assert client.get("/events/p3").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 1


def test_batch_interop_with_single_event_endpoint(client):
    # events created singly are visible to batch replay and references
    assert client.post("/events", json=_ev("single", "zoe", 3)).status_code == 201
    assert _post_batch(client, [_ev("single", "zoe", 3)]).status_code == 200
    r = _post_batch(client, [_ev("from-batch", "zoe", 4, correction_of="single")])
    assert r.status_code == 201

    # and batch-created events are reachable from the single endpoint
    single = client.post("/events", json=_ev("single", "zoe", 3))
    assert single.status_code == 200
    follow = client.post(
        "/events", json=_ev("after-batch", "zoe", 5, correction_of="from-batch")
    )
    assert follow.status_code == 201


def test_batch_revision_block_contiguous_under_concurrency(client):
    before = client.get("/leaderboard").json()["revision"]
    size = 20
    batches = 5

    def write(b):
        return _post_batch(
            client, [_ev(f"conc-{b}-{i}", f"p-{i % 4}", i) for i in range(size)]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(write, range(batches)))

    assert all(r.status_code == 201 for r in results)
    all_revisions = sorted(
        rev for r in results for rev in [e["revision"] for e in r.json()]
    )
    assert all_revisions == list(range(before + 1, before + size * batches + 1))
    # each individual response is one contiguous block in request order
    for r in results:
        revs = [e["revision"] for e in r.json()]
        assert revs == list(range(revs[0], revs[0] + size))
    after = client.get("/leaderboard").json()["revision"]
    assert after == before + size * batches


def test_failed_batch_consumes_no_revision_under_concurrency(client):
    # a valid batch and a failing batch race: only the valid one advances rev
    good = [_ev(f"ok-{i}") for i in range(5)]
    bad = [_ev("bad-1"), _ev("bad-2", correction_of="missing-event")]

    def attempt(kind):
        if kind == "good":
            return _post_batch(client, good)
        return _post_batch(client, bad)

    with ThreadPoolExecutor(max_workers=4) as pool:
        rs = [pool.submit(attempt, "good")] + [
            pool.submit(attempt, "bad") for _ in range(5)
        ]
        outcomes = [f.result().status_code for f in rs]

    assert outcomes.count(201) == 1
    assert outcomes.count(422) == 5
    assert client.get("/leaderboard").json()["revision"] == 5
    assert client.get("/events/bad-1").status_code == 404


def test_batch_events_listed_and_correction_chain_effective(client):
    events = [
        _ev("l1", "alice", 10),
        _ev("l2", "alice", 20, correction_of="l1"),
    ]
    assert _post_batch(client, events).status_code == 201
    listed = client.get("/events").json()
    assert [e["event_id"] for e in listed] == ["l1", "l2"]
    # at revision 1 the original event counts; at revision 2 only the tip
    lb1 = client.get("/leaderboard?revision=1").json()
    assert lb1["entries"][0]["total_points"] == 10
    lb2 = client.get("/leaderboard?revision=2").json()
    assert lb2["entries"][0]["total_points"] == 20
