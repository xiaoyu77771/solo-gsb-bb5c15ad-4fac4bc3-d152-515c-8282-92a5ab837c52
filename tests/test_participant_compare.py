"""Tests for GET /participants/compare: per-revision two-way score race."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "compare-test.db"))
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


def _compare(client, **params):
    params.setdefault("left_id", "alice")
    params.setdefault("right_id", "bob")
    r = client.get("/participants/compare", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_intervals_merge_consecutive_identical_snapshots(client):
    _post(client, "e1", "alice", 10)  # rev1: 10-0
    _post(client, "e2", "bob", 5)  # rev2: 10-5
    _post(client, "e3", "carol", 100)  # rev3: 10-5 (third party: race unchanged)
    _post(client, "e4", "bob", 5)  # rev4: 10-10
    _post(client, "e5", "bob", 5)  # rev5: 10-15
    _post(client, "e6", "alice", 10)  # rev6: 20-15

    body = _compare(client, from_revision=0, to_revision=6)
    assert body["left_id"] == "alice"
    assert body["right_id"] == "bob"
    assert body["from_revision"] == 0
    assert body["to_revision"] == 6
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "left_points": 0, "right_points": 0, "leader": "tie"},
        {"start_revision": 1, "end_revision": 1, "left_points": 10, "right_points": 0, "leader": "left"},
        # rev2 and rev3 merge: both sides' totals unchanged by carol's event
        {"start_revision": 2, "end_revision": 3, "left_points": 10, "right_points": 5, "leader": "left"},
        {"start_revision": 4, "end_revision": 4, "left_points": 10, "right_points": 10, "leader": "tie"},
        {"start_revision": 5, "end_revision": 5, "left_points": 10, "right_points": 15, "leader": "right"},
        {"start_revision": 6, "end_revision": 6, "left_points": 20, "right_points": 15, "leader": "left"},
    ]


def test_subrange_excludes_earlier_revisions(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "bob", 5)
    body = _compare(client, from_revision=2, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 2, "end_revision": 2, "left_points": 10, "right_points": 5, "leader": "left"},
        {"start_revision": 3, "end_revision": 3, "left_points": 10, "right_points": 10, "leader": "tie"},
    ]


def test_equal_from_and_to_yields_single_snapshot_interval(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    body = _compare(client, from_revision=2, to_revision=2)
    assert body["intervals"] == [
        {"start_revision": 2, "end_revision": 2, "left_points": 10, "right_points": 5, "leader": "left"}
    ]


def test_correction_reattribution_moves_points_in_same_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # correction re-attributes the 10-point event from alice to bob
    _post(client, "e3", "bob", 10, correction_of="e1")
    body = _compare(client, from_revision=1, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 1, "left_points": 10, "right_points": 0, "leader": "left"},
        {"start_revision": 2, "end_revision": 2, "left_points": 10, "right_points": 5, "leader": "left"},
        # at the correction's own revision alice is debited and bob credited
        {"start_revision": 3, "end_revision": 3, "left_points": 0, "right_points": 15, "leader": "right"},
    ]


def test_correction_chain_counts_only_the_tip(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")
    body = _compare(client, from_revision=1, to_revision=3)
    assert [i["left_points"] for i in body["intervals"]] == [10, 12, 15]
    assert all(i["leader"] == "left" for i in body["intervals"])


def test_branched_correction_superseded_only_once(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # branch off e1
    # both branch tips are effective; e1 is subtracted exactly once
    body = _compare(client, from_revision=3, to_revision=3)
    assert body["intervals"] == [
        {"start_revision": 3, "end_revision": 3, "left_points": 32, "right_points": 0, "leader": "left"}
    ]


def test_scoreless_pair_returns_single_tie_interval(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 5)
    body = _compare(client, left_id="carol", right_id="dave", from_revision=0, to_revision=2)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 2, "left_points": 0, "right_points": 0, "leader": "tie"}
    ]


def test_unknown_participants_do_not_error(client):
    _post(client, "e1", "alice", 10)
    body = _compare(client, left_id="nobody", right_id="ghost", from_revision=0, to_revision=1)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "left_points": 0, "right_points": 0, "leader": "tie"}
    ]
    # one-sided unknown: the known side still scores
    body = _compare(client, left_id="alice", right_id="ghost", from_revision=1, to_revision=1)
    assert body["intervals"][0]["leader"] == "left"
    assert body["intervals"][0]["left_points"] == 10


def test_same_participant_on_both_sides_is_always_tie(client):
    _post(client, "e1", "alice", 10)
    body = _compare(client, left_id="alice", right_id="alice", from_revision=0, to_revision=1)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "left_points": 0, "right_points": 0, "leader": "tie"},
        {"start_revision": 1, "end_revision": 1, "left_points": 10, "right_points": 10, "leader": "tie"},
    ]


def test_empty_ledger_revision_zero_pair(client):
    body = _compare(client, from_revision=0, to_revision=0)
    assert body["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "left_points": 0, "right_points": 0, "leader": "tie"}
    ]


def test_intervals_cover_whole_range_without_gaps(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 20)
    _post(client, "e3", "alice", 5, correction_of="e1")
    _post(client, "e4", "bob", 1, correction_of="e2")
    body = _compare(client, from_revision=0, to_revision=4)
    cursor = 0
    for interval in body["intervals"]:
        assert interval["start_revision"] == cursor
        assert interval["end_revision"] >= interval["start_revision"]
        cursor = interval["end_revision"] + 1
    assert cursor == 5


def test_totals_match_leaderboard_at_every_revision(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    _post(client, "e3", "alice", 20, correction_of="e1")
    _post(client, "e4", "bob", 1, correction_of="e2")
    _post(client, "e5", "carol", 100)  # third party: irrelevant to the race

    body = _compare(client, from_revision=0, to_revision=5)
    by_revision = {}
    for interval in body["intervals"]:
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            by_revision[rev] = interval

    # revision 0 is the empty ledger (not queryable via /leaderboard)
    assert (by_revision[0]["left_points"], by_revision[0]["right_points"]) == (0, 0)

    for rev in range(1, 6):
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        totals = {e["participant_id"]: e["total_points"] for e in lb["entries"]}
        interval = by_revision[rev]
        assert interval["left_points"] == totals.get("alice", 0), rev
        assert interval["right_points"] == totals.get("bob", 0), rev
        expected = (
            "tie"
            if interval["left_points"] == interval["right_points"]
            else "left" if interval["left_points"] > interval["right_points"] else "right"
        )
        assert interval["leader"] == expected, rev


def test_missing_params_422(client):
    assert client.get("/participants/compare").status_code == 422
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 0},
    ).status_code == 422
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "from_revision": 0, "to_revision": 1},
    ).status_code == 422
    assert client.get(
        "/participants/compare",
        params={"right_id": "bob", "from_revision": 0, "to_revision": 1},
    ).status_code == 422


def test_non_integer_params_422(client):
    r = client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": "abc", "to_revision": 2},
    )
    assert r.status_code == 422


def test_negative_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": -1, "to_revision": 1},
    ).status_code == 422


def test_from_greater_than_to_422(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 2, "to_revision": 1},
    ).status_code == 422


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 0, "to_revision": 2},
    ).status_code == 404
    # empty ledger: any to_revision > 0 is out of range
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 0, "to_revision": 99},
    ).status_code == 404


def test_ordering_checked_before_range(client):
    # both out of range and wrongly ordered: ordering wins -> 422
    assert client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 9, "to_revision": 5},
    ).status_code == 422


def test_compare_route_not_shadowed_by_participant_routes(client):
    # the literal /participants/compare path must win over
    # /participants/{participant_id}/score-breakdown style matching
    _post(client, "e1", "alice", 10)
    r = client.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob", "from_revision": 0, "to_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["left_id"] == "alice"
