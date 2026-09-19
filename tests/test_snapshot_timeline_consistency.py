"""Cross-consistency tests for the shared SnapshotTimeline replay engine.

The revision-window endpoints — ``/participants/compare``,
``/participants/{id}/threshold-intervals`` and ``/leaderboard/changes`` (plus
``/audit/correction-forks``) — all replay one event set through
``app.ledger.SnapshotTimeline``. These tests pin the engine's own semantics
(events becoming effective, first correction superseding once, fork leaves,
cross-participant transfers, negative points) and verify every consumer
agrees with the independently-implemented ``/leaderboard`` snapshot query at
every revision, including revision 0 and subranges.
"""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
import app.ledger as ledger
from app.db import read_tx
from app.ledger import OwnershipTransfer, SnapshotTimeline, _diff_snapshots
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "timeline-test.db"))
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
    assert r.status_code == 201, r.text
    return r.json()


def _leaderboard_totals(client, revision):
    entries = client.get("/leaderboard", params={"revision": revision}).json()["entries"]
    return {e["participant_id"]: e["total_points"] for e in entries}


def _timeline(to_revision: int) -> SnapshotTimeline:
    with read_tx() as conn:
        return SnapshotTimeline.from_connection(conn, to_revision)


# --- engine semantics ------------------------------------------------------


def test_engine_accepts_plain_mappings_without_a_database():
    tl = SnapshotTimeline(
        [
            {"event_id": "x", "participant_id": "p", "points": 4,
             "correction_of": None, "revision": 1},
            {"event_id": "y", "participant_id": "q", "points": 9,
             "correction_of": "x", "revision": 2},
        ]
    )
    tl.advance(2)
    # x is superseded at rev 2 by y; its points move p -> q.
    assert tl.total_for("p") == 0
    assert tl.total_for("q") == 9
    assert [l.event_id for l, _root in tl.effective_leaves()] == ["y"]


def test_linear_correction_supersedes_each_tip_exactly_once(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")

    tl = _timeline(3)
    assert tl.revision == 0
    assert tl.total_for("alice") == 0

    tl.advance(1)
    assert tl.total_for("alice") == 10
    assert tl.effect_at(1).added == ("e1",)
    assert tl.effect_at(1).first_corrected == ()
    assert tl.effect_at(1).transfers == ()

    tl.advance(2)
    assert tl.total_for("alice") == 12
    eff2 = tl.effect_at(2)
    assert eff2.added == ("e2",)
    assert eff2.first_corrected == ("e1",)

    tl.advance(3)
    assert tl.total_for("alice") == 15
    eff3 = tl.effect_at(3)
    assert eff3.added == ("e3",)
    assert eff3.first_corrected == ("e2",)

    leaves = [(l.event_id, root) for l, root in tl.effective_leaves()]
    assert leaves == [("e3", "e1")]
    assert tl.chain_path("e3") == ["e1", "e2", "e3"]
    assert tl.fork_roots() == []
    # same-participant corrections never record an ownership transfer
    assert tl.all_transfers() == []


def test_fork_keeps_every_branch_leaf_and_supersedes_ancestor_once(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")  # second branch
    _post(client, "e4", "bob", 25, correction_of="e3")   # chained onto one branch

    tl = _timeline(4)
    totals = {}
    for rev in range(0, 5):
        tl.advance(rev)
        totals[rev] = tl.total_for("alice")
    # rev4: e3 is superseded by e4 (and re-attributed to bob), leaving alice
    # only the e2 branch tip.
    assert totals == {0: 0, 1: 10, 2: 12, 3: 32, 4: 12}

    # e1 is first-corrected at rev2; the forking correction at rev3 and the
    # chained correction at rev4 never supersede it (or e3) a second time.
    assert tl.effect_at(3).first_corrected == ()
    assert tl.effect_at(4).first_corrected == ("e3",)

    assert tl.fork_roots() == ["e1"]
    by_root: dict[str, list[tuple]] = {}
    for leaf, root in tl.effective_leaves():
        by_root.setdefault(root, []).append(
            (leaf.event_id, leaf.participant_id, leaf.points)
        )
    assert by_root == {
        "e1": [("e2", "alice", 12), ("e4", "bob", 25)],
    }
    assert tl.chain_path("e4") == ["e1", "e3", "e4"]
    assert tl.chain_path("e2") == ["e1", "e2"]


def test_cross_participant_correction_records_transfer(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "bob", 10, correction_of="e1")  # e1 moves alice -> bob

    tl = _timeline(3)
    tl.advance(3)
    assert tl.total_for("alice") == 0
    assert tl.total_for("bob") == 15
    assert tl.all_transfers() == [
        OwnershipTransfer(
            revision=3,
            event_id="e1",
            from_participant="alice",
            to_participant="bob",
            points=10,
        )
    ]

    # the all-zero starting state at revision 0
    fresh = _timeline(3)
    assert fresh.revision == 0
    assert fresh.rank_positions() == {}
    assert list(fresh.effective_leaves()) == []
    assert fresh.all_transfers() == []


def test_fork_correction_to_another_participant_is_not_a_transfer(client):
    # The *first* correction re-attributes; a later forking correction to a
    # third party adds a new branch leaf but never debits the ancestor again.
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10, correction_of="e1")   # first: alice -> bob
    _post(client, "e3", "carol", 30, correction_of="e1")  # fork branch

    tl = _timeline(3)
    tl.advance(3)
    assert tl.total_for("alice") == 0
    assert tl.total_for("bob") == 10
    assert tl.total_for("carol") == 30
    assert [(t.event_id, t.from_participant, t.to_participant)
            for t in tl.all_transfers()] == [("e1", "alice", "bob")]
    assert tl.fork_roots() == ["e1"]


def test_negative_points_keep_participant_ranked(client):
    _post(client, "e1", "alice", -3)
    _post(client, "e2", "alice", 5)
    _post(client, "e3", "bob", -1)

    tl = _timeline(3)
    tl.advance(1)
    assert tl.total_for("alice") == -3
    # a participant with negative effective points is still a snapshot member
    assert tl.rank_positions()["alice"].model_dump() == {"rank": 1, "total_points": -3}
    tl.advance(3)
    assert tl.total_for("alice") == 2
    assert tl.total_for("bob") == -1
    assert [p for p in tl.rank_positions()] == ["alice", "bob"]

    # threshold -3: rev0 (empty, 0 >= -3) through rev1 (-3 >= -3) is one run
    r = client.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": -3, "from_revision": 0, "to_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["intervals"] == [
        {"start_revision": 0, "end_revision": 1, "start_points": 0, "end_points": -3}
    ]


# --- scenario ledger shared by the cross-endpoint checks -------------------


@pytest.fixture()
def scenario(client):
    """Revisions 1..8: two chains, both forking, two cross-person corrections.

    rev1 e1 alice +10        rev4 e4 bob  +10 corrects e1 (alice -> bob)
    rev2 e2 bob  +5          rev5 e5 dave +20 corrects e2 (bob -> dave)
    rev3 e3 carol +7         rev6 e6 alice +30 corrects e1 (forks e1)
                             rev7 e7 alice -5
                             rev8 e8 bob  +8 corrects e2 (forks e2)
    """
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    _post(client, "e3", "carol", 7)
    _post(client, "e4", "bob", 10, correction_of="e1")
    _post(client, "e5", "dave", 20, correction_of="e2")
    _post(client, "e6", "alice", 30, correction_of="e1")
    _post(client, "e7", "alice", -5)
    _post(client, "e8", "bob", 8, correction_of="e2")
    return client


_EXPECTED_TOTALS = {
    0: {},
    1: {"alice": 10},
    2: {"alice": 10, "bob": 5},
    3: {"alice": 10, "bob": 5, "carol": 7},
    4: {"bob": 15, "carol": 7},
    5: {"bob": 10, "carol": 7, "dave": 20},
    6: {"alice": 30, "bob": 10, "carol": 7, "dave": 20},
    7: {"alice": 25, "bob": 10, "carol": 7, "dave": 20},
    8: {"alice": 25, "bob": 18, "carol": 7, "dave": 20},
}


def test_engine_totals_and_ranks_match_leaderboard_at_every_revision(scenario):
    for rev in range(0, 9):
        tl = _timeline(8)
        tl.advance(rev)
        expected = _EXPECTED_TOTALS[rev]
        for pid in ("alice", "bob", "carol", "dave", "ghost"):
            assert tl.total_for(pid) == expected.get(pid, 0), (rev, pid)

        if rev >= 1:
            lb_totals = _leaderboard_totals(scenario, rev)
            assert lb_totals == expected, rev
            assert {p: pos.total_points for p, pos in tl.rank_positions().items()} == lb_totals
            api_ranks = {
                e["participant_id"]: e["rank"]
                for e in scenario.get("/leaderboard", params={"revision": rev}).json()["entries"]
            }
            assert {p: pos.rank for p, pos in tl.rank_positions().items()} == api_ranks
        else:
            assert tl.rank_positions() == {}


def test_compare_totals_match_leaderboard_across_full_range(scenario):
    r = scenario.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob",
                "from_revision": 0, "to_revision": 8},
    )
    assert r.status_code == 200
    by_rev = {}
    for interval in r.json()["intervals"]:
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            by_rev[rev] = interval
    assert set(by_rev) == set(range(0, 9))
    for rev, interval in by_rev.items():
        expected = _EXPECTED_TOTALS[rev]
        assert interval["left_points"] == expected.get("alice", 0)
        assert interval["right_points"] == expected.get("bob", 0)
        leader = (
            "tie"
            if interval["left_points"] == interval["right_points"]
            else "left" if interval["left_points"] > interval["right_points"] else "right"
        )
        assert interval["leader"] == leader


def test_compare_subrange_matches_full_timeline_snapshots(scenario):
    r = scenario.get(
        "/participants/compare",
        params={"left_id": "dave", "right_id": "carol",
                "from_revision": 4, "to_revision": 7},
    )
    assert r.status_code == 200
    by_rev = {}
    for interval in r.json()["intervals"]:
        for rev in range(interval["start_revision"], interval["end_revision"] + 1):
            by_rev[rev] = interval
    assert set(by_rev) == {4, 5, 6, 7}
    for rev in range(4, 8):
        interval = by_rev[rev]
        assert interval["left_points"] == _EXPECTED_TOTALS[rev].get("dave", 0)
        assert interval["right_points"] == _EXPECTED_TOTALS[rev].get("carol", 0)


def test_threshold_intervals_match_leaderboard_in_subrange(scenario):
    # dave is absent (0) through rev4, then 20 from rev5; window [3, 7]
    r = scenario.get(
        "/participants/dave/threshold-intervals",
        params={"threshold": 20, "from_revision": 3, "to_revision": 7},
    )
    assert r.status_code == 200
    assert r.json()["intervals"] == [
        {"start_revision": 5, "end_revision": 7, "start_points": 20, "end_points": 20}
    ]

    # bob across the full range with threshold 15: 15 at rev4, 18 at rev8
    r = scenario.get(
        "/participants/bob/threshold-intervals",
        params={"threshold": 15, "from_revision": 0, "to_revision": 8},
    )
    assert r.json()["intervals"] == [
        {"start_revision": 4, "end_revision": 4, "start_points": 15, "end_points": 15},
        {"start_revision": 8, "end_revision": 8, "start_points": 18, "end_points": 18},
    ]

    # every reported point is exactly the leaderboard total; every unreported
    # revision in the window is strictly below the threshold
    r = scenario.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 20, "from_revision": 0, "to_revision": 8},
    )
    body = r.json()
    assert body["intervals"] == [
        {"start_revision": 6, "end_revision": 8, "start_points": 30, "end_points": 25}
    ]
    covered = {
        rev
        for interval in body["intervals"]
        for rev in range(interval["start_revision"], interval["end_revision"] + 1)
    }
    for rev in range(0, 9):
        total = _EXPECTED_TOTALS[rev].get("alice", 0)
        assert (rev in covered) == (total >= 20), rev


def test_changes_diff_matches_engine_rank_positions(scenario):
    from_rev, to_rev = 4, 6
    r = scenario.get(
        "/leaderboard/changes",
        params={"from_revision": from_rev, "to_revision": to_rev},
    )
    assert r.status_code == 200

    tl = _timeline(to_rev)
    tl.advance(from_rev)
    before = tl.rank_positions()
    tl.advance(to_rev)
    after = tl.rank_positions()
    expected = [c.model_dump() for c in _diff_snapshots(before, after)]
    assert r.json()["changes"] == expected

    # explicit expectations around the fork + cross-person moves
    by_pid = {c["participant_id"]: c for c in r.json()["changes"]}
    assert by_pid["alice"]["before"] is None
    assert by_pid["alice"]["after"] == {"rank": 1, "total_points": 30}
    assert by_pid["alice"]["points_delta"] == 30
    assert by_pid["dave"]["before"] is None
    assert by_pid["dave"]["after"] == {"rank": 2, "total_points": 20}
    assert by_pid["bob"]["before"] == {"rank": 1, "total_points": 15}
    assert by_pid["bob"]["after"] == {"rank": 3, "total_points": 10}
    assert by_pid["bob"]["points_delta"] == -5
    # carol has unchanged points but shifted rank -> still reported
    assert by_pid["carol"]["before"] == {"rank": 2, "total_points": 7}
    assert by_pid["carol"]["after"] == {"rank": 4, "total_points": 7}
    assert by_pid["carol"]["points_delta"] == 0


def test_fork_audit_matches_engine_leaves(scenario):
    r = scenario.get("/audit/correction-forks", params={"revision": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == 8
    assert [f["chain_root"] for f in body["forks"]] == ["e1", "e2"]
    forks = {f["chain_root"]: f for f in body["forks"]}

    assert [
        (leaf["path"], leaf["participant_id"], leaf["points"])
        for leaf in forks["e1"]["leaves"]
    ] == [
        (["e1", "e4"], "bob", 10),
        (["e1", "e6"], "alice", 30),
    ]
    assert [
        (leaf["path"], leaf["participant_id"], leaf["points"])
        for leaf in forks["e2"]["leaves"]
    ] == [
        (["e2", "e5"], "dave", 20),
        (["e2", "e8"], "bob", 8),
    ]
    assert forks["e1"]["participant_points"] == [
        {"participant_id": "alice", "total_points": 30},
        {"participant_id": "bob", "total_points": 10},
    ]
    assert forks["e2"]["participant_points"] == [
        {"participant_id": "bob", "total_points": 8},
        {"participant_id": "dave", "total_points": 20},
    ]

    # before either fork exists (rev3: two independent linear chains)
    assert scenario.get("/audit/correction-forks", params={"revision": 3}).json() == {
        "revision": 3,
        "forks": [],
    }
    # only e1 has forked at rev6
    r6 = scenario.get("/audit/correction-forks", params={"revision": 6}).json()
    assert [f["chain_root"] for f in r6["forks"]] == ["e1"]


def test_revision_zero_semantics_across_consumers(scenario):
    # compare from 0 and a pure 0->0 window both start from the empty snapshot
    r = scenario.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob",
                "from_revision": 0, "to_revision": 0},
    )
    assert r.json()["intervals"] == [
        {"start_revision": 0, "end_revision": 0,
         "left_points": 0, "right_points": 0, "leader": "tie"}
    ]

    r = scenario.get(
        "/participants/ghost/threshold-intervals",
        params={"threshold": 0, "from_revision": 0, "to_revision": 0},
    )
    assert r.json()["intervals"] == [
        {"start_revision": 0, "end_revision": 0, "start_points": 0, "end_points": 0}
    ]

    r = scenario.get(
        "/leaderboard/changes",
        params={"from_revision": 0, "to_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["changes"][0]["before"] is None

    assert scenario.get("/audit/correction-forks", params={"revision": 0}).json() == {
        "revision": 0, "forks": []
    }


# --- single range read -----------------------------------------------------


def test_each_replay_endpoint_reads_the_event_set_once(scenario, monkeypatch):
    """Replay endpoints must issue exactly one events range read per call.

    Advancing one revision at a time must never re-query the database; the
    whole window is one ``revision <= to_revision`` range scan.
    """
    calls = {"n": 0}
    original = ledger._timeline_rows

    def spy(conn, to_revision):
        calls["n"] += 1
        return original(conn, to_revision)

    monkeypatch.setattr(ledger, "_timeline_rows", spy)

    calls["n"] = 0
    r = scenario.get(
        "/leaderboard/changes",
        params={"from_revision": 2, "to_revision": 8},
    )
    assert r.status_code == 200
    assert calls["n"] == 1

    calls["n"] = 0
    r = scenario.get(
        "/participants/compare",
        params={"left_id": "alice", "right_id": "bob",
                "from_revision": 0, "to_revision": 8},
    )
    assert r.status_code == 200
    assert calls["n"] == 1

    calls["n"] = 0
    r = scenario.get(
        "/participants/alice/threshold-intervals",
        params={"threshold": 10, "from_revision": 0, "to_revision": 8},
    )
    assert r.status_code == 200
    assert calls["n"] == 1

    calls["n"] = 0
    r = scenario.get("/audit/correction-forks", params={"revision": 8})
    assert r.status_code == 200
    assert calls["n"] == 1
