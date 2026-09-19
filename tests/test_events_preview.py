"""Tests for POST /events/preview: read-only scenario projection."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "preview-test.db"))
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


def _post(client, event_id, participant_id, points, correction_of=None):
    r = client.post("/events", json=_ev(event_id, participant_id, points, correction_of))
    assert r.status_code == 201
    return r.json()


def _preview(client, base_revision, events):
    r = client.post(
        "/events/preview",
        json={"base_revision": base_revision, "events": events},
    )
    assert r.status_code == 200, r.text
    return r.json()


def test_projection_on_empty_ledger(client):
    body = _preview(
        client,
        0,
        [_ev("d1", "alice", 10), _ev("d2", "bob", 5)],
    )
    assert body["base_revision"] == 0
    projected = body["projected_leaderboard"]
    # virtual revision: base_revision + number of drafts
    assert projected["revision"] == 2
    assert [(e["rank"], e["participant_id"], e["total_points"]) for e in projected["entries"]] == [
        (1, "alice", 10),
        (2, "bob", 5),
    ]
    # drafts carry their virtual revisions in the effective events
    assert projected["entries"][0]["effective_events"] == [
        {"event_id": "d1", "points": 10, "revision": 1, "correction_of": None}
    ]
    assert body["changes"] == [
        {
            "participant_id": "alice",
            "before": None,
            "after": {"rank": 1, "total_points": 10},
            "points_delta": 10,
        },
        {
            "participant_id": "bob",
            "before": None,
            "after": {"rank": 2, "total_points": 5},
            "points_delta": 5,
        },
    ]


def test_projection_stacks_on_baseline(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)

    body = _preview(client, 2, [_ev("d1", "alice", 5)])
    projected = body["projected_leaderboard"]
    assert projected["revision"] == 3
    assert [(e["participant_id"], e["total_points"]) for e in projected["entries"]] == [
        ("alice", 15),
        ("bob", 7),
    ]
    # alice's effective events: the baseline event plus the draft
    alice = projected["entries"][0]
    assert [e["event_id"] for e in alice["effective_events"]] == ["e1", "d1"]
    assert [e["revision"] for e in alice["effective_events"]] == [1, 3]
    assert body["changes"] == [
        {
            "participant_id": "alice",
            "before": {"rank": 1, "total_points": 10},
            "after": {"rank": 1, "total_points": 15},
            "points_delta": 5,
        }
    ]


def test_base_revision_pins_the_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 50)  # beyond the baseline: ignored

    body = _preview(client, 1, [_ev("d1", "carol", 3)])
    projected = body["projected_leaderboard"]
    assert projected["revision"] == 2
    # bob's rev-2 event is invisible at base_revision 1
    assert [e["participant_id"] for e in projected["entries"]] == ["alice", "carol"]
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    assert set(by_pid) == {"carol"}
    assert by_pid["carol"]["before"] is None
    assert by_pid["carol"]["after"] == {"rank": 2, "total_points": 3}


def test_base_revision_zero_ignores_whole_ledger(client):
    _post(client, "e1", "alice", 10)
    body = _preview(client, 0, [_ev("d1", "bob", 4)])
    assert [e["participant_id"] for e in body["projected_leaderboard"]["entries"]] == ["bob"]
    assert body["projected_leaderboard"]["revision"] == 1


def test_draft_correcting_baseline_event_supersedes_it(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 8)

    body = _preview(client, 2, [_ev("d1", "alice", 25, correction_of="e1")])
    projected = body["projected_leaderboard"]
    alice = next(e for e in projected["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == 25
    # only the correction tip is effective
    assert [e["event_id"] for e in alice["effective_events"]] == ["d1"]
    assert alice["effective_events"][0]["correction_of"] == "e1"
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    assert by_pid["alice"]["points_delta"] == 15
    assert "bob" not in by_pid  # unaffected participant omitted


def test_draft_chain_within_batch(client):
    _post(client, "e1", "alice", 10)

    body = _preview(
        client,
        1,
        [
            _ev("d1", "alice", 12, correction_of="e1"),
            _ev("d2", "alice", 15, correction_of="d1"),
        ],
    )
    projected = body["projected_leaderboard"]
    assert projected["revision"] == 3
    alice = projected["entries"][0]
    assert alice["total_points"] == 15
    assert [e["event_id"] for e in alice["effective_events"]] == ["d2"]
    assert alice["effective_events"][0]["revision"] == 3


def test_draft_forking_baseline_chain_keeps_both_leaves(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")

    # the draft corrects e1 as well: e1 forks into the e2 and d1 branches
    body = _preview(client, 2, [_ev("d1", "bob", 20, correction_of="e1")])
    projected = body["projected_leaderboard"]
    by_pid = {e["participant_id"]: e for e in projected["entries"]}
    assert by_pid["alice"]["total_points"] == 12
    assert by_pid["bob"]["total_points"] == 20
    assert [e["event_id"] for e in by_pid["alice"]["effective_events"]] == ["e2"]
    assert [e["event_id"] for e in by_pid["bob"]["effective_events"]] == ["d1"]


def test_draft_reattributing_event_shows_exit_and_entry(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)

    body = _preview(client, 2, [_ev("d1", "carol", 10, correction_of="e1")])
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
        "after": {"rank": 1, "total_points": 10},
        "points_delta": 10,
    }


def test_ties_sorted_by_participant_id_with_sequential_ranks(client):
    body = _preview(
        client,
        0,
        [_ev("d1", "carol", 10), _ev("d2", "alice", 10), _ev("d3", "bob", 10)],
    )
    entries = body["projected_leaderboard"]["entries"]
    assert [(e["rank"], e["participant_id"]) for e in entries] == [
        (1, "alice"),
        (2, "bob"),
        (3, "carol"),
    ]
    # nobody moved between baseline and projection ranks? all are new entries
    assert [c["participant_id"] for c in body["changes"]] == ["alice", "bob", "carol"]


def test_rank_only_change_reported(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)

    # carol jumps ahead of alice; both incumbents keep their points but
    # their ranks move — rank-only changes are reported for both
    body = _preview(client, 2, [_ev("d1", "carol", 20)])
    by_pid = {c["participant_id"]: c for c in body["changes"]}
    assert by_pid["alice"] == {
        "participant_id": "alice",
        "before": {"rank": 1, "total_points": 10},
        "after": {"rank": 2, "total_points": 10},
        "points_delta": 0,
    }
    assert by_pid["bob"] == {
        "participant_id": "bob",
        "before": {"rank": 2, "total_points": 5},
        "after": {"rank": 3, "total_points": 5},
        "points_delta": 0,
    }


def test_unchanged_projection_yields_empty_changes(client):
    _post(client, "e1", "alice", 10)
    # a correction that keeps alice's total and rank exactly the same
    body = _preview(client, 1, [_ev("d1", "alice", 10, correction_of="e1")])
    assert body["changes"] == []
    alice = body["projected_leaderboard"]["entries"][0]
    assert [e["event_id"] for e in alice["effective_events"]] == ["d1"]


def test_conflicting_draft_id_against_baseline_event_409(client):
    _post(client, "e1", "alice", 10)
    r = client.post(
        "/events/preview",
        json={"base_revision": 1, "events": [_ev("e1", "alice", 10)]},
    )
    assert r.status_code == 409


def test_conflicting_draft_id_beyond_base_revision_409(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # e2 is invisible at base_revision 1 but still conflicts ledger-wide
    r = client.post(
        "/events/preview",
        json={"base_revision": 1, "events": [_ev("e2", "bob", 5)]},
    )
    assert r.status_code == 409


def test_conflict_check_precedes_reference_validation(client):
    _post(client, "e1", "alice", 10)
    r = client.post(
        "/events/preview",
        json={
            "base_revision": 1,
            "events": [_ev("e1", "alice", 99), _ev("d1", correction_of="ghost")],
        },
    )
    assert r.status_code == 409


def test_correction_of_later_draft_422(client):
    r = client.post(
        "/events/preview",
        json={
            "base_revision": 0,
            "events": [
                _ev("d1", "alice", 10, correction_of="d2"),
                _ev("d2", "alice", 12),
            ],
        },
    )
    assert r.status_code == 422


def test_correction_of_unknown_event_422(client):
    r = client.post(
        "/events/preview",
        json={"base_revision": 0, "events": [_ev("d1", correction_of="ghost")]},
    )
    assert r.status_code == 422


def test_correction_of_event_beyond_base_revision_422(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    # e2 exists in the ledger but is not visible at base_revision 1
    r = client.post(
        "/events/preview",
        json={"base_revision": 1, "events": [_ev("d1", "bob", 9, correction_of="e2")]},
    )
    assert r.status_code == 422


def test_self_referencing_draft_422(client):
    r = client.post(
        "/events/preview",
        json={"base_revision": 0, "events": [_ev("d1", correction_of="d1")]},
    )
    assert r.status_code == 422


def test_duplicate_event_ids_within_batch_422(client):
    r = client.post(
        "/events/preview",
        json={
            "base_revision": 0,
            "events": [_ev("dup"), _ev("other"), _ev("dup", points=9)],
        },
    )
    assert r.status_code == 422


def test_base_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    for bad in (2, 99999):
        r = client.post(
            "/events/preview",
            json={"base_revision": bad, "events": [_ev("d1")]},
        )
        assert r.status_code == 404, bad


def test_base_revision_validation_422(client):
    _post(client, "e1", "alice", 10)
    for bad in (-1, "abc", 1.5, None):
        r = client.post(
            "/events/preview",
            json={"base_revision": bad, "events": [_ev("d1")]},
        )
        assert r.status_code == 422, bad
    # base_revision is required
    assert client.post("/events/preview", json={"events": [_ev("d1")]}).status_code == 422


def test_events_array_validation_422(client):
    # empty, missing, over 100, non-array
    assert client.post(
        "/events/preview", json={"base_revision": 0, "events": []}
    ).status_code == 422
    assert client.post("/events/preview", json={"base_revision": 0}).status_code == 422
    assert client.post(
        "/events/preview",
        json={"base_revision": 0, "events": [_ev(f"too-many-{i}") for i in range(101)]},
    ).status_code == 422
    assert client.post(
        "/events/preview", json={"base_revision": 0, "events": "nope"}
    ).status_code == 422


def test_exactly_100_drafts_ok(client):
    body = _preview(
        client, 0, [_ev(f"d{i}", f"p-{i % 5}", i) for i in range(100)]
    )
    assert body["projected_leaderboard"]["revision"] == 100
    assert len(body["projected_leaderboard"]["entries"]) == 5


def test_preview_does_not_write_or_consume_revision(client):
    _post(client, "e1", "alice", 10)
    before_events = client.get("/events").json()
    before_revision = client.get("/leaderboard").json()["revision"]

    _preview(client, 1, [_ev("d1", "bob", 50), _ev("d2", "carol", 30)])
    # failed previews are read-only too
    assert client.post(
        "/events/preview", json={"base_revision": 99, "events": [_ev("d3")]}
    ).status_code == 404
    assert client.post(
        "/events/preview", json={"base_revision": 1, "events": [_ev("e1")]}
    ).status_code == 409
    assert client.post(
        "/events/preview",
        json={"base_revision": 1, "events": [_ev("d4", correction_of="ghost")]},
    ).status_code == 422

    assert client.get("/events").json() == before_events
    assert client.get("/leaderboard").json()["revision"] == before_revision
    # the next real write gets the revision the preview would have used
    assert _post(client, "e2", "bob", 1)["revision"] == 2


def test_projection_matches_real_leaderboard_after_writing_drafts(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    drafts = [
        _ev("w1", "carol", 20),
        _ev("w2", "alice", 30, correction_of="e1"),
        _ev("w3", "bob", 3),
    ]
    preview = _preview(client, 2, drafts)

    for d in drafts:
        r = client.post("/events", json=d)
        assert r.status_code == 201
    real = client.get("/leaderboard").json()

    projected = preview["projected_leaderboard"]
    assert projected["revision"] == real["revision"]
    assert [
        (e["rank"], e["participant_id"], e["total_points"], e["effective_events"])
        for e in projected["entries"]
    ] == [
        (e["rank"], e["participant_id"], e["total_points"], e["effective_events"])
        for e in real["entries"]
    ]
    # and the changes match the real /leaderboard/changes over the same span
    real_changes = client.get(
        "/leaderboard/changes",
        params={"from_revision": 2, "to_revision": real["revision"]},
    ).json()["changes"]
    assert preview["changes"] == real_changes
