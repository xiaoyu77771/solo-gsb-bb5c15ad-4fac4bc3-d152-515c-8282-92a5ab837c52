"""Tests for GET /participants/{participant_id}/threshold-intervals."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "threshold-test.db"))
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


def _query(client, participant_id="alice", **params):
    params.setdefault("threshold", 10)
    params.setdefault("from_revision", 0)
    params.setdefault("to_revision", 6)
    r = client.get(f"/participants/{participant_id}/threshold-intervals", params=params)
    return r


def test_basic_intervals_above_threshold(client):
    # rev1: 10, rev2: 10 (third party), rev3: 15, rev4: 5, rev5: 5, rev6: 3
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 100)
    _post(client, "e3", "alice", 5)
    _post(client, "e4", "alice", -10)
    _post(client, "e5", "carol", 7)
    _post(client, "e6", "alice", -2)

    body = _query(client, threshold=10, from_revision=0, to_revision=6).json()
    assert body["participant_id"] == "alice"
    assert body["threshold"] == 10
    assert body["from_revision"] == 0
    assert body["to_revision"] == 6
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 15},
    ]
    # a later correction puts her back above the threshold
    _post(client, "e7", "alice", 7)  # rev7: 10
    body = _query(client, threshold=10, from_revision=4, to_revision=7).json()
    assert body["intervals"] == [
        {"start_revision": 7, "end_revision": 7, "start_points": 10, "end_points": 10},
    ]


def test_boundary_is_inclusive(client):
    _post(client, "e1", "alice", 5)
    _post(client, "e2", "alice", 5)  # rev2: exactly 10
    _post(client, "e3", "alice", 1)  # rev3: 11
    body = _query(client, threshold=10, from_revision=0, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 2, "end_revision": 3, "start_points": 10, "end_points": 11},
    ]


def test_endpoints_can_differ_inside_one_interval(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 5)   # 15, still >= 10
    _post(client, "e3", "alice", -2)  # 13, still >= 10
    body = _query(client, threshold=10, from_revision=0, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 13},
    ]


def test_subrange_within_a_long_run_starts_inside(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 5)
    _post(client, "e3", "alice", 5)
    body = _query(client, threshold=10, from_revision=2, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 2, "end_revision": 3, "start_points": 15, "end_points": 20},
    ]


def test_equal_from_and_to_single_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", -5)
    above = _query(client, threshold=10, from_revision=1, to_revision=1).json()
    assert above["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 10, "end_points": 10},
    ]
    below = _query(client, threshold=10, from_revision=2, to_revision=2).json()
    assert below["intervals"] == []


def test_negative_threshold_and_revision_zero(client):
    _post(client, "e1", "alice", -3)
    # threshold 0: rev0 (empty, 0 >= 0) qualifies but rev1 (-3) does not
    body = _query(client, threshold=0, from_revision=0, to_revision=1).json()
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "start_points": 0, "end_points": 0},
    ]
    body = _query(client, threshold=-3, from_revision=0, to_revision=1).json()
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "start_points": 0, "end_points": -3},
    ]
    body = _query(client, threshold=1, from_revision=0, to_revision=1).json()
    assert body["intervals"] == []


def test_unknown_participant_treated_as_zero(client):
    _post(client, "e1", "alice", 10)
    body = _query(client, "ghost", threshold=0, from_revision=0, to_revision=1).json()
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "start_points": 0, "end_points": 0},
    ]
    body = _query(client, "ghost", threshold=1, from_revision=0, to_revision=1).json()
    assert body["intervals"] == []


def test_empty_ledger(client):
    body = _query(client, threshold=0, from_revision=0, to_revision=0).json()
    assert body == {
        "participant_id": "alice",
        "threshold": 0,
        "from_revision": 0,
        "to_revision": 0,
        "intervals": [
            {"start_revision": 0, "end_revision": 0, "start_points": 0, "end_points": 0}
        ],
    }


def test_correction_reattribution_cross_participant(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # correction moves the 10-point event from alice to bob at rev3
    _post(client, "e3", "bob", 10, correction_of="e1")
    body = _query(client, threshold=10, from_revision=0, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 2, "start_points": 10, "end_points": 10},
        # rev3: alice drops to 0 — interval ends before the correction
    ]
    # the same correction makes bob rise to 15
    body = _query(client, "bob", threshold=10, from_revision=0, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 15, "end_points": 15},
    ]


def test_correction_chain_counts_only_the_tip(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")
    body = _query(client, threshold=11, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 2, "end_revision": 3, "start_points": 12, "end_points": 15},
    ]


def test_branched_correction_fork_keeps_both_tips(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # fork off e1
    # rev1: 10; rev2: 12; rev3: 32 (both branch tips effective, e1 gone once)
    body = _query(client, threshold=15, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 32, "end_points": 32},
    ]
    body = _query(client, threshold=10, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 32},
    ]


def test_fork_reattributing_one_branch_elsewhere(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "bob", 20, correction_of="e1")  # second branch -> bob
    # alice: rev1=10, rev2=12, rev3=12 (only the e2 tip remains hers)
    body = _query(client, threshold=10, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 3, "start_points": 10, "end_points": 12},
    ]
    body = _query(client, "bob", threshold=10, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "start_points": 20, "end_points": 20},
    ]


def test_fall_and_rise_via_two_corrections(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10, correction_of="e1")   # rev2: alice -> 0
    _post(client, "e3", "alice", 25, correction_of="e2")  # rev3: chain back to alice
    body = _query(client, threshold=10, from_revision=1, to_revision=3).json()
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "start_points": 10, "end_points": 10},
        {"start_revision": 3, "end_revision": 3, "start_points": 25, "end_points": 25},
    ]


def test_matches_leaderboard_at_every_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    _post(client, "e3", "alice", 20, correction_of="e1")
    _post(client, "e4", "bob", 1, correction_of="e2")
    _post(client, "e5", "carol", 100)

    threshold = 12
    body = _query(client, threshold=threshold, from_revision=0, to_revision=5).json()
    covered: set[int] = set()
    for interval in body["intervals"]:
        assert interval["start_revision"] <= interval["end_revision"]
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            covered.add(rev)

    expected: set[int] = {0}  # rev0 total is 0, threshold 12 -> not covered
    expected.discard(0)
    for rev in range(1, 6):
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        totals = {e["participant_id"]: e["total_points"] for e in lb["entries"]}
        if totals.get("alice", 0) >= threshold:
            expected.add(rev)
    assert covered == expected

    # endpoints carry the exact recomputed totals
    totals_by_rev = {0: 0}
    for rev in range(1, 6):
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        totals_by_rev[rev] = next(
            (e["total_points"] for e in lb["entries"] if e["participant_id"] == "alice"),
            0,
        )
    for interval in body["intervals"]:
        assert interval["start_points"] == totals_by_rev[interval["start_revision"]]
        assert interval["end_points"] == totals_by_rev[interval["end_revision"]]
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            assert totals_by_rev[rev] >= threshold


def test_intervals_sorted_by_start_revision(client):
    # three separate above-threshold runs
    _post(client, "e1", "alice", 10)   # 10
    _post(client, "e2", "alice", -10)  # 0
    _post(client, "e3", "alice", 10)   # 10
    _post(client, "e4", "alice", -10)  # 0
    _post(client, "e5", "alice", 10)   # 10
    body = _query(client, threshold=10, from_revision=1, to_revision=5).json()
    starts = [i["start_revision"] for i in body["intervals"]]
    assert starts == sorted(starts)
    assert starts == [1, 3, 5]


def test_missing_params_422(client):
    assert client.get("/participants/alice/threshold-intervals").status_code == 422
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 10, "to_revision": 1},
    ).status_code == 422
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 10, "from_revision": 0},
    ).status_code == 422
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"from_revision": 0, "to_revision": 1},
    ).status_code == 422


def test_non_integer_params_422(client):
    r = client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": "high", "from_revision": 0, "to_revision": 2},
    )
    assert r.status_code == 422
    r = client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 10, "from_revision": "x", "to_revision": 2},
    )
    assert r.status_code == 422


def test_negative_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert _query(client, from_revision=-1, to_revision=1).status_code == 422
    assert client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 10, "from_revision": 0, "to_revision": -1},
    ).status_code == 422


def test_from_greater_than_to_422(client):
    _post(client, "e1", "alice", 10)
    assert _query(client, from_revision=2, to_revision=1).status_code == 422


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert _query(client, from_revision=0, to_revision=2).status_code == 404
    # unknown participants still get the out-of-range revision check
    assert client.get(
        "/participants/ghost/threshold-intervals",
        params={"threshold": 0, "from_revision": 0, "to_revision": 9},
    ).status_code == 404


def test_to_revision_beyond_zero_on_empty_ledger_404(client):
    assert _query(client, threshold=0, from_revision=0, to_revision=1).status_code == 404


def test_ordering_checked_before_range(client):
    # both out of range and wrongly ordered: ordering wins -> 422
    assert _query(client, from_revision=9, to_revision=5).status_code == 422


def test_query_is_read_only(client):
    _post(client, "e1", "alice", 10)
    before = client.get("/events").json()
    max_rev_before = max(e["revision"] for e in before)
    r = _query(client, threshold=10, from_revision=0, to_revision=1)
    assert r.status_code == 200
    after = client.get("/events").json()
    assert after == before
    assert max(e["revision"] for e in after) == max_rev_before
