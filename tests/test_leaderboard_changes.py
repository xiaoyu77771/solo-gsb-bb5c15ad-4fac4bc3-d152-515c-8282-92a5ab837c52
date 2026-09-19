"""Tests for GET /leaderboard/changes: rank/points diffs across revisions."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "changes-test.db"))
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


def _changes(client, **params):
    r = client.get("/leaderboard/changes", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_empty_to_first_revision(client):
    _post(client, "e1", "alice", 10)
    body = _changes(client, from_revision=0, to_revision=1)
    assert body == {
        "from_revision": 0,
        "to_revision": 1,
        "changes": [
            {
                "participant_id": "alice",
                "before": None,
                "after": {"rank": 1, "total_points": 10},
                "points_delta": 10,
            }
        ],
    }


def test_unchanged_participant_omitted(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "carol", 3)
    # bob gains 4 pts (5 -> 9) but stays rank 2: only bob appears.
    _post(client, "e4", "bob", 4)
    body = _changes(client, from_revision=3, to_revision=4)
    assert [c["participant_id"] for c in body["changes"]] == ["bob"]
    assert body["changes"][0] == {
        "participant_id": "bob",
        "before": {"rank": 2, "total_points": 5},
        "after": {"rank": 2, "total_points": 9},
        "points_delta": 4,
    }


def test_rank_only_change_without_points_delta(client):
    # carol jumps ahead of bob; bob's points are unchanged but his rank moves.
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "carol", 3)
    _post(client, "e4", "carol", 10)
    body = _changes(client, from_revision=3, to_revision=4)
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    # alice and carol now tie at 13/10? alice=10; carol=13 -> carol rank 1
    assert by_pid["carol"]["after"] == {"rank": 1, "total_points": 13}
    assert by_pid["carol"]["points_delta"] == 10
    assert by_pid["alice"]["before"] == {"rank": 1, "total_points": 10}
    assert by_pid["alice"]["after"] == {"rank": 2, "total_points": 10}
    assert by_pid["alice"]["points_delta"] == 0  # same points, shifted rank
    assert by_pid["bob"]["before"] == {"rank": 2, "total_points": 5}
    assert by_pid["bob"]["after"] == {"rank": 3, "total_points": 5}
    assert by_pid["bob"]["points_delta"] == 0


def test_tie_rank_stable_when_points_unchanged(client):
    _post(client, "e1", "a", 5)
    _post(client, "e2", "b", 5)
    _post(client, "e3", "c", 1)
    # a correction for c that leaves c at the same total: nothing should move.
    _post(client, "e4", "c", 1, correction_of="e3")
    body = _changes(client, from_revision=3, to_revision=4)
    assert body["changes"] == []


def test_correction_moving_event_from_a_to_b_shows_exit_and_entry(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # correction re-attributes the 10-point event from alice to bob's team.
    _post(client, "e3", "carol", 10, correction_of="e1")
    body = _changes(client, from_revision=2, to_revision=3)
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    assert set(by_pid) == {"alice", "carol"}  # bob unaffected
    assert by_pid["alice"] == {
        "participant_id": "alice",
        "before": {"rank": 1, "total_points": 10},
        "after": None,
        "points_delta": -10,
    }
    assert by_pid["carol"] == {
        "participant_id": "carol",
        "before": None,
        "after": {"rank": 1, "total_points": 10},  # tie with bob(5)? carol 10 -> rank 1
        "points_delta": 10,
    }


def test_correction_chain_effective_tip_at_each_snapshot(client):
    _post(client, "e1", "alice", 10)
    rev1 = 1
    _post(client, "e2", "bob", 8)
    rev2 = 2
    _post(client, "e3", "alice", 15, correction_of="e1")
    rev3 = 3
    _post(client, "e4", "alice", 6, correction_of="e3")
    rev4 = 4

    # rev2 -> rev3: alice 10 -> 15; bob(8) stays rank 2 and is omitted.
    body = _changes(client, from_revision=rev2, to_revision=rev3)
    alice = next(c for c in body["changes"] if c["participant_id"] == "alice")
    assert alice["before"] == {"rank": 1, "total_points": 10}
    assert alice["after"] == {"rank": 1, "total_points": 15}
    assert alice["points_delta"] == 5
    assert all(c["participant_id"] != "bob" for c in body["changes"])

    # rev3 -> rev4: alice drops to 6, bob overtakes: ranks swap both ways.
    body = _changes(client, from_revision=rev3, to_revision=rev4)
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    assert by_pid["alice"] == {
        "participant_id": "alice",
        "before": {"rank": 1, "total_points": 15},
        "after": {"rank": 2, "total_points": 6},
        "points_delta": -9,
    }
    assert by_pid["bob"] == {
        "participant_id": "bob",
        "before": {"rank": 2, "total_points": 8},
        "after": {"rank": 1, "total_points": 8},
        "points_delta": 0,
    }

    # whole arc from the empty board: revision echo
    assert _changes(client, from_revision=0, to_revision=rev2)["to_revision"] == rev2


def test_changes_sorted_by_participant_id(client):
    _post(client, "e1", "zoe", 1)
    _post(client, "e2", "amy", 2)
    _post(client, "e3", "mike", 3)
    _post(client, "e4", "zoe", 10)
    _post(client, "e5", "amy", 10)
    body = _changes(client, from_revision=3, to_revision=5)
    pids = [c["participant_id"] for c in body["changes"]]
    assert pids == sorted(pids)


def test_missing_params_422(client):
    assert client.get("/leaderboard/changes").status_code == 422
    assert client.get(
        "/leaderboard/changes", params={"from_revision": 0}
    ).status_code == 422
    assert client.get(
        "/leaderboard/changes", params={"to_revision": 1}
    ).status_code == 422


def test_non_integer_params_422(client):
    r = client.get(
        "/leaderboard/changes",
        params={"from_revision": "abc", "to_revision": 2},
    )
    assert r.status_code == 422


def test_negative_from_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": -1, "to_revision": 1},
    ).status_code == 422


def test_equal_revisions_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 1, "to_revision": 1},
    ).status_code == 422


def test_reversed_order_422(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 2, "to_revision": 1},
    ).status_code == 422


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 0, "to_revision": 2},
    ).status_code == 404
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 5, "to_revision": 6},
    ).status_code == 404


def test_ordering_checked_before_range(client):
    # both out of range and wrongly ordered: ordering wins -> 422
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 9, "to_revision": 5},
    ).status_code == 422


def test_from_zero_on_empty_board_404_when_to_exceeds(client):
    # nothing written: current revision is 0; no valid pair exists
    assert client.get(
        "/leaderboard/changes",
        params={"from_revision": 0, "to_revision": 1},
    ).status_code == 404


def test_full_diff_against_leaderboard_snapshots(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    _post(client, "e3", "carol", 7)
    _post(client, "e4", "bob", 5)

    before = client.get("/leaderboard", params={"revision": 2}).json()["entries"]
    after = client.get("/leaderboard", params={"revision": 4}).json()["entries"]
    body = _changes(client, from_revision=2, to_revision=4)

    def pos(entries, pid):
        e = next((x for x in entries if x["participant_id"] == pid), None)
        return None if e is None else {"rank": e["rank"], "total_points": e["total_points"]}

    expected_pids = {
        e["participant_id"] for entries in (before, after) for e in entries
    }
    for pid in expected_pids:
        b, a = pos(before, pid), pos(after, pid)
        change = next(
            (c for c in body["changes"] if c["participant_id"] == pid), None
        )
        if b == a:
            # unchanged participants must be omitted entirely
            assert change is None, pid
            continue
        assert change is not None, pid
        assert change["before"] == b
        assert change["after"] == a
        assert change["points_delta"] == (
            (a["total_points"] if a else 0) - (b["total_points"] if b else 0)
        )
