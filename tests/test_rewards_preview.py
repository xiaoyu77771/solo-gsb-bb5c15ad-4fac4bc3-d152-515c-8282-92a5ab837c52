"""Tests for POST /rewards/preview: dry-run prize settlement."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "rewards-test.db"))
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


def _preview(client, prize_cents, revision=None):
    payload = {"prize_cents": prize_cents}
    if revision is not None:
        payload["revision"] = revision
    r = client.post("/rewards/preview", json=payload)
    assert r.status_code == 200, r.text
    return r.json()


def test_empty_ledger_returns_no_winners(client):
    assert _preview(client, [100, 50]) == {"revision": 0, "winners": []}


def test_distinct_totals_paid_by_rank(client):
    _post(client, "e1", "alice", 30)
    _post(client, "e2", "bob", 20)
    _post(client, "e3", "carol", 10)

    body = _preview(client, [1000, 500, 250, 100])
    assert body["revision"] == 3
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 30, "prize_cents": 1000},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 2,
         "total_points": 20, "prize_cents": 500},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 10, "prize_cents": 250},
    ]


def test_tie_group_splits_merged_pool(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 5)

    body = _preview(client, [100, 50, 30])
    # alice/bob tie over ranks 1-2: pool 100+50=150, split 75 each.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "bob", "rank_start": 1, "rank_end": 2,
         "total_points": 10, "prize_cents": 75},
        {"participant_id": "carol", "rank_start": 3, "rank_end": 3,
         "total_points": 5, "prize_cents": 30},
    ]


def test_leftover_cents_go_to_lowest_participant_id(client):
    _post(client, "e1", "carol", 10)
    _post(client, "e2", "alice", 10)
    _post(client, "e3", "bob", 10)

    body = _preview(client, [100])
    # Three-way tie over ranks 1-3, only rank 1 in the zone: pool 100,
    # share 33 each, the 1 leftover cent goes to the lowest participant_id.
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 34},
        {"participant_id": "bob", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 33},
        {"participant_id": "carol", "rank_start": 1, "rank_end": 3,
         "total_points": 10, "prize_cents": 33},
    ]
    assert sum(w["prize_cents"] for w in body["winners"]) == 100


def test_group_straddling_prize_zone_boundary(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10)

    body = _preview(client, [101])
    # Tie over ranks 1-2 but only rank 1 is prized: pool 101, split 50/51,
    # the leftover cent goes to alice (lower participant_id).
    assert [w["prize_cents"] for w in body["winners"]] == [51, 50]
    assert sum(w["prize_cents"] for w in body["winners"]) == 101


def test_group_beyond_prize_zone_gets_nothing(client):
    _post(client, "e1", "alice", 20)
    _post(client, "e2", "bob", 10)
    _post(client, "e3", "carol", 10)

    body = _preview(client, [100])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 20, "prize_cents": 100},
    ]


def test_zero_prize_rank_is_still_in_the_zone(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)

    body = _preview(client, [0, 50])
    assert body["winners"] == [
        {"participant_id": "alice", "rank_start": 1, "rank_end": 1,
         "total_points": 10, "prize_cents": 0},
        {"participant_id": "bob", "rank_start": 2, "rank_end": 2,
         "total_points": 5, "prize_cents": 50},
    ]


def test_revision_pins_the_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)

    rev1 = _preview(client, [100], revision=1)
    assert rev1["revision"] == 1
    assert [w["participant_id"] for w in rev1["winners"]] == ["alice"]

    latest = _preview(client, [100])
    assert latest["revision"] == 2
    assert [w["participant_id"] for w in latest["winners"]] == ["bob"]


def test_correction_chain_counts_only_tip(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 8)
    # Re-attribute alice's event to bob with +12.
    _post(client, "e3", "bob", 12, correction_of="e1")

    body = _preview(client, [100, 50])
    assert body["winners"] == [
        {"participant_id": "bob", "rank_start": 1, "rank_end": 1,
         "total_points": 20, "prize_cents": 100},
    ]

    rev2 = _preview(client, [100, 50], revision=2)
    assert [w["participant_id"] for w in rev2["winners"]] == ["alice", "bob"]


def test_unknown_revision_returns_404(client):
    _post(client, "e1", "alice", 10)
    for bad in (0, -1, 2, 99999):
        r = client.post("/rewards/preview",
                        json={"revision": bad, "prize_cents": [100]})
        assert r.status_code == 404, bad


def test_prize_cents_validation(client):
    _post(client, "e1", "alice", 10)
    for bad in ([], [-1], [100, -5], ["abc"], [1.5], list(range(101))):
        r = client.post("/rewards/preview", json={"prize_cents": bad})
        assert r.status_code == 422, bad
    # prize_cents is required
    assert client.post("/rewards/preview", json={}).status_code == 422
    # non-integer revision is a validation error, not a 404
    r = client.post("/rewards/preview",
                    json={"revision": "abc", "prize_cents": [100]})
    assert r.status_code == 422


def test_revision_rejects_non_integer_json_types(client):
    """revision takes a JSON integer or null only — no lax coercion."""
    _post(client, "e1", "alice", 10)
    # Booleans, numeric strings and whole-number floats must not be
    # coerced into a snapshot revision.
    for bad in (True, False, "1", "0", 1.0, 0.0, 1.5, [1], {"r": 1}):
        r = client.post("/rewards/preview",
                        json={"revision": bad, "prize_cents": [100]})
        assert r.status_code == 422, bad
        assert [e["loc"] for e in r.json()["detail"]] == [["body", "revision"]], bad


def test_prize_cents_rejects_non_integer_json_types(client):
    """Each prize item takes a non-negative JSON integer — no lax coercion."""
    _post(client, "e1", "alice", 10)
    for bad in (True, False, "100", 100.0, 1.5, None, [100], {"p": 1}):
        r = client.post("/rewards/preview", json={"prize_cents": [bad]})
        assert r.status_code == 422, bad
        assert [e["loc"] for e in r.json()["detail"]] == [["body", "prize_cents", 0]], bad


def test_prize_cents_error_location_points_at_each_offending_item(client):
    """Nested errors report the exact array index of every bad item."""
    r = client.post("/rewards/preview",
                    json={"prize_cents": [100, "50", 250.0, True, 30]})
    assert r.status_code == 422
    assert [e["loc"] for e in r.json()["detail"]] == [
        ["body", "prize_cents", 1],
        ["body", "prize_cents", 2],
        ["body", "prize_cents", 3],
    ]


def test_valid_integer_forms_still_accepted(client):
    """JSON integers (incl. 0 and arbitrary precision) and null revision."""
    _post(client, "e1", "alice", 10)

    r = client.post("/rewards/preview",
                    json={"revision": 1, "prize_cents": [10**30]})
    assert r.status_code == 200, r.text
    assert r.json()["winners"][0]["prize_cents"] == 10**30

    # Explicit null revision behaves like an omitted one: latest snapshot.
    r = client.post("/rewards/preview",
                    json={"revision": None, "prize_cents": [0, 100]})
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 1
    assert r.json()["winners"][0]["prize_cents"] == 0


def test_rejected_previews_do_not_write_to_the_ledger(client):
    """A 422 preview leaves events and the current revision untouched."""
    _post(client, "e1", "alice", 10)
    before_events = client.get("/events").json()
    before_revision = client.get("/leaderboard").json()["revision"]

    bad_payloads = [
        {"revision": True, "prize_cents": [100]},
        {"revision": "1", "prize_cents": [100]},
        {"revision": 1.0, "prize_cents": [100]},
        {"prize_cents": [False]},
        {"prize_cents": ["100"]},
        {"prize_cents": [100.0]},
        {"prize_cents": [100, -1]},
        {"prize_cents": []},
    ]
    for payload in bad_payloads:
        r = client.post("/rewards/preview", json=payload)
        assert r.status_code == 422, payload

    assert client.get("/events").json() == before_events
    assert client.get("/leaderboard").json()["revision"] == before_revision


def test_preview_does_not_write_to_the_ledger(client):
    _post(client, "e1", "alice", 10)
    before_events = client.get("/events").json()
    before_revision = client.get("/leaderboard").json()["revision"]

    _preview(client, [100, 50])

    assert client.get("/events").json() == before_events
    assert client.get("/leaderboard").json()["revision"] == before_revision
