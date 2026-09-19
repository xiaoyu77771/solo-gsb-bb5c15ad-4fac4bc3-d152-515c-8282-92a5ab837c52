"""Tests for GET /events/changes: effective-event diffs across revisions."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "event-changes-test.db"))
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
    r = client.get("/events/changes", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_added_events_from_empty_ledger(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "bob", 5)
    body = _changes(client, from_revision=0, to_revision=2)
    assert body["from_revision"] == 0
    assert body["to_revision"] == 2
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "added",
            "before": None,
            "after": e1,
        },
        {
            "chain_root": "e2",
            "change_type": "added",
            "before": None,
            "after": e2,
        },
    ]


def test_correction_is_reported_as_replaced(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    body = _changes(client, from_revision=1, to_revision=2)
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "replaced",
            "before": e1,
            "after": e2,
        }
    ]


def test_correction_chain_collapses_to_single_replaced(client):
    e1 = _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 15, correction_of="e2")
    # across the whole chain: effective tip goes e1 -> e3 in one replaced item
    body = _changes(client, from_revision=1, to_revision=3)
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "replaced",
            "before": e1,
            "after": e3,
        }
    ]


def test_unchanged_chain_tip_cancels_out(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "bob", 5)
    # e2 is the effective tip of chain e1 at both revisions: only e3 is new.
    body = _changes(client, from_revision=2, to_revision=3)
    assert body["changes"] == [
        {
            "chain_root": "e3",
            "change_type": "added",
            "before": None,
            "after": e3,
        }
    ]


def test_from_zero_reports_only_chain_tip_as_added(client):
    _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "bob", 5)
    body = _changes(client, from_revision=0, to_revision=3)
    # superseded e1 never surfaces; the chain contributes only its tip e2
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "added",
            "before": None,
            "after": e2,
        },
        {
            "chain_root": "e3",
            "change_type": "added",
            "before": None,
            "after": e3,
        },
    ]


def test_branching_correction_is_added_under_same_root(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 20, correction_of="e1")  # branch off e1
    # rev2 effective: {e2}; rev3 effective: {e2, e3} -> pure addition in chain e1
    body = _changes(client, from_revision=2, to_revision=3)
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "added",
            "before": None,
            "after": e3,
        }
    ]


def test_unbalanced_remainder_is_not_merged(client):
    _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 15, correction_of="e2")
    e4 = _post(client, "e4", "alice", 20, correction_of="e1")  # branch off e1
    # rev2 effective: {e2}; rev4 effective: {e3, e4}
    # one out (e2) but two in (e3, e4) -> no merge: 1 removed + 2 added
    body = _changes(client, from_revision=2, to_revision=4)
    assert body["changes"] == [
        {
            "chain_root": "e1",
            "change_type": "removed",
            "before": e2,
            "after": None,
        },
        {
            "chain_root": "e1",
            "change_type": "added",
            "before": None,
            "after": e3,
        },
        {
            "chain_root": "e1",
            "change_type": "added",
            "before": None,
            "after": e4,
        },
    ]


def test_change_items_carry_full_events(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    body = _changes(client, from_revision=1, to_revision=2)
    change = body["changes"][0]
    for side in ("before", "after"):
        assert set(change[side]) == {
            "event_id",
            "participant_id",
            "points",
            "correction_of",
            "revision",
            "created_at",
        }
    assert change["before"] == client.get("/events/e1").json()
    assert change["after"] == client.get("/events/e2").json()


def test_sorted_by_chain_root_then_revision_then_event_id(client):
    _post(client, "z1", "zoe", 1)
    _post(client, "a1", "amy", 2)
    _post(client, "m1", "mike", 3)
    _post(client, "a2", "amy", 5, correction_of="a1")
    body = _changes(client, from_revision=0, to_revision=4)
    # chain_root sorts first: a1 (tip a2) < m1 < z1, regardless of revision
    assert [c["chain_root"] for c in body["changes"]] == ["a1", "m1", "z1"]
    assert [c["after"]["event_id"] for c in body["changes"]] == ["a2", "m1", "z1"]


def test_replaced_sorts_by_after_revision_within_root(client):
    # two roots interleaved in revision order; output must group by root
    _post(client, "b1", "bob", 5)
    _post(client, "a1", "amy", 7)
    b2 = _post(client, "b2", "bob", 9, correction_of="b1")
    a2 = _post(client, "a2", "amy", 8, correction_of="a1")
    body = _changes(client, from_revision=2, to_revision=4)
    assert [(c["chain_root"], c["change_type"]) for c in body["changes"]] == [
        ("a1", "replaced"),
        ("b1", "replaced"),
    ]
    assert body["changes"][0]["after"] == a2
    assert body["changes"][1]["after"] == b2


def test_missing_params_422(client):
    assert client.get("/events/changes").status_code == 422
    assert client.get(
        "/events/changes", params={"from_revision": 0}
    ).status_code == 422
    assert client.get(
        "/events/changes", params={"to_revision": 1}
    ).status_code == 422


def test_non_integer_params_422(client):
    r = client.get(
        "/events/changes",
        params={"from_revision": "abc", "to_revision": 2},
    )
    assert r.status_code == 422


def test_negative_from_revision_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": -1, "to_revision": 1},
    ).status_code == 422


def test_equal_revisions_422(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": 1, "to_revision": 1},
    ).status_code == 422


def test_reversed_order_422(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5)
    assert client.get(
        "/events/changes",
        params={"from_revision": 2, "to_revision": 1},
    ).status_code == 422


def test_to_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    assert client.get(
        "/events/changes",
        params={"from_revision": 0, "to_revision": 2},
    ).status_code == 404
    assert client.get(
        "/events/changes",
        params={"from_revision": 5, "to_revision": 6},
    ).status_code == 404


def test_ordering_checked_before_range(client):
    # both out of range and wrongly ordered: ordering wins -> 422
    assert client.get(
        "/events/changes",
        params={"from_revision": 9, "to_revision": 5},
    ).status_code == 422


def test_from_zero_on_empty_ledger_404_when_to_exceeds(client):
    # nothing written: current revision is 0; no valid pair exists
    assert client.get(
        "/events/changes",
        params={"from_revision": 0, "to_revision": 1},
    ).status_code == 404


def test_route_not_shadowed_by_event_id_path(client):
    # /events/changes must win over /events/{event_id}: without the required
    # query params it answers 422 (missing params), not the event "changes".
    ev = _post(client, "changes", "alice", 3)
    assert client.get("/events/changes").status_code == 422
    body = _changes(client, from_revision=0, to_revision=1)
    assert body["changes"][0]["after"] == ev
