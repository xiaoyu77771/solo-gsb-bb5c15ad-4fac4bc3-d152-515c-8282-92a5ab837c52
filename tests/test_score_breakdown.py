"""Tests for GET /participants/{participant_id}/score-breakdown."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "breakdown-test.db"))
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


def _breakdown(client, participant_id, **params):
    r = client.get(f"/participants/{participant_id}/score-breakdown", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_empty_ledger_defaults_to_revision_zero(client):
    body = _breakdown(client, "alice")
    assert body == {"revision": 0, "total_points": 0, "contributions": []}


def test_unknown_participant_gets_zero_and_empty_list(client):
    _post(client, "e1", "alice", 10)
    body = _breakdown(client, "nobody")
    assert body == {"revision": 1, "total_points": 0, "contributions": []}


def test_single_event_contribution(client):
    e1 = _post(client, "e1", "alice", 10)
    body = _breakdown(client, "alice")
    assert body["revision"] == 1
    assert body["total_points"] == 10
    assert body["contributions"] == [
        {"chain_root": "e1", "effective_event": e1, "history": [e1]}
    ]


def test_correction_chain_counts_tip_with_full_history(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 15, correction_of="e2")
    body = _breakdown(client, "alice")
    assert body["revision"] == 3
    assert body["total_points"] == 15
    assert body["contributions"] == [
        {
            "chain_root": "e1",
            "effective_event": e3,
            # root -> tip, revision ascending
            "history": [e1, e2, e3],
        }
    ]


def test_historical_revision_pins_snapshot(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")

    body = _breakdown(client, "alice", revision=2)
    assert body["revision"] == 2
    assert body["total_points"] == 12
    assert body["contributions"] == [
        {"chain_root": "e1", "effective_event": e2, "history": [e1, e2]}
    ]

    body = _breakdown(client, "alice", revision=1)
    assert body["total_points"] == 10
    assert body["contributions"] == [
        {"chain_root": "e1", "effective_event": e1, "history": [e1]}
    ]


def test_revision_zero_is_empty_snapshot(client):
    _post(client, "e1", "alice", 10)
    body = _breakdown(client, "alice", revision=0)
    assert body == {"revision": 0, "total_points": 0, "contributions": []}


def test_correction_moving_points_transfers_chain(client):
    e1 = _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 10, correction_of="e1")  # re-attribute to bob

    # alice no longer holds the chain at the current snapshot...
    body = _breakdown(client, "alice")
    assert body == {"revision": 2, "total_points": 0, "contributions": []}
    # ...but did at revision 1.
    body = _breakdown(client, "alice", revision=1)
    assert body["total_points"] == 10
    assert [c["chain_root"] for c in body["contributions"]] == ["e1"]

    # bob owns the chain now; history still shows the earlier attribution.
    body = _breakdown(client, "bob")
    assert body["total_points"] == 10
    (contrib,) = body["contributions"]
    assert contrib["chain_root"] == "e1"
    assert contrib["effective_event"]["participant_id"] == "bob"
    assert [h["participant_id"] for h in contrib["history"]] == ["alice", "bob"]
    assert [h["event_id"] for h in contrib["history"]] == ["e1", "e2"]
    assert e1["participant_id"] == "alice"


def test_multiple_chains_sorted_by_chain_root(client):
    _post(client, "z1", "alice", 1)
    _post(client, "a1", "alice", 2)
    _post(client, "m1", "alice", 3)
    _post(client, "a2", "alice", 5, correction_of="a1")
    _post(client, "b1", "bob", 100)  # other participant: excluded
    body = _breakdown(client, "alice")
    assert body["total_points"] == 1 + 5 + 3
    assert [c["chain_root"] for c in body["contributions"]] == ["a1", "m1", "z1"]
    assert [c["effective_event"]["event_id"] for c in body["contributions"]] == [
        "a2",
        "m1",
        "z1",
    ]


def test_branched_chain_yields_one_contribution_per_tip(client):
    e1 = _post(client, "e1", "alice", 10)
    e2 = _post(client, "e2", "alice", 12, correction_of="e1")
    e3 = _post(client, "e3", "alice", 20, correction_of="e1")  # branch off e1
    body = _breakdown(client, "alice")
    # both branch tips are effective; each is its own contribution
    assert body["total_points"] == 12 + 20
    assert body["contributions"] == [
        {"chain_root": "e1", "effective_event": e2, "history": [e1, e2]},
        {"chain_root": "e1", "effective_event": e3, "history": [e1, e3]},
    ]


def test_total_matches_leaderboard_entry(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    _post(client, "e3", "alice", 4)
    _post(client, "e4", "alice", 20, correction_of="e1")
    _post(client, "e5", "bob", 1, correction_of="e2")

    for rev in (1, 2, 3, 4, 5):
        body = _breakdown(client, "alice", revision=rev)
        lb = client.get("/leaderboard", params={"revision": rev}).json()
        entry = next(
            (e for e in lb["entries"] if e["participant_id"] == "alice"), None
        )
        expected = entry["total_points"] if entry else 0
        assert body["revision"] == rev
        assert body["total_points"] == expected
        # contributions explain the total exactly
        assert sum(c["effective_event"]["points"] for c in body["contributions"]) == (
            expected
        )


def test_negative_revision_422(client):
    _post(client, "e1", "alice", 10)
    r = client.get("/participants/alice/score-breakdown", params={"revision": -1})
    assert r.status_code == 422


def test_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    r = client.get("/participants/alice/score-breakdown", params={"revision": 2})
    assert r.status_code == 404
    # also on an empty ledger: revision 1 does not exist yet
    r = client.get("/participants/bob/score-breakdown", params={"revision": 99})
    assert r.status_code == 404


def test_non_integer_revision_422(client):
    r = client.get("/participants/alice/score-breakdown", params={"revision": "abc"})
    assert r.status_code == 422


def test_default_revision_is_pinned_latest(client):
    _post(client, "e1", "alice", 10)
    body = _breakdown(client, "alice")
    assert body["revision"] == 1
    _post(client, "e2", "alice", 5)
    body = _breakdown(client, "alice")
    assert body["revision"] == 2
    assert body["total_points"] == 15
