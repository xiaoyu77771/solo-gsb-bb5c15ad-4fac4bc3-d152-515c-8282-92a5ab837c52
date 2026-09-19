"""Tests for GET /audit/correction-forks."""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "forks-test.db"))
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


def _forks(client, **params):
    r = client.get("/audit/correction-forks", params=params)
    assert r.status_code == 200, params
    return r.json()


def test_empty_ledger_defaults_to_revision_zero(client):
    body = _forks(client)
    assert body == {"revision": 0, "forks": []}


def test_revision_zero_is_empty_snapshot(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    body = _forks(client, revision=0)
    assert body == {"revision": 0, "forks": []}


def test_plain_linear_chain_is_not_returned(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 15, correction_of="e2")
    body = _forks(client)
    assert body == {"revision": 3, "forks": []}


def test_single_standalone_event_is_not_returned(client):
    _post(client, "e1", "alice", 10)
    body = _forks(client)
    assert body["forks"] == []


def test_simple_fork_lists_both_branches(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    body = _forks(client)
    assert body["revision"] == 3
    assert body["forks"] == [
        {
            "chain_root": "e1",
            "leaves": [
                {
                    "path": ["e1", "e2"],
                    "participant_id": "alice",
                    "points": 12,
                    "revision": 2,
                },
                {
                    "path": ["e1", "e3"],
                    "participant_id": "alice",
                    "points": 20,
                    "revision": 3,
                },
            ],
            "participant_points": [
                {"participant_id": "alice", "total_points": 32}
            ],
        }
    ]


def test_chained_correction_stays_on_its_branch(client):
    # e1 -> e2 ; e1 -> e3 -> e4 : the fork is at e1, e4 extends only one branch
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    _post(client, "e4", "bob", 25, correction_of="e3")
    body = _forks(client)
    assert body["revision"] == 4
    (fork,) = body["forks"]
    assert fork["chain_root"] == "e1"
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e2"],
        ["e1", "e3", "e4"],
    ]
    assert [(l["participant_id"], l["points"], l["revision"]) for l in fork["leaves"]] == [
        ("alice", 12, 2),
        ("bob", 25, 4),
    ]
    assert fork["participant_points"] == [
        {"participant_id": "alice", "total_points": 12},
        {"participant_id": "bob", "total_points": 25},
    ]


def test_fork_at_non_root_node(client):
    # linear e1 -> e2, then e2 forks into e3 / e4; root is still e1
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 14, correction_of="e2")
    _post(client, "e4", "bob", 16, correction_of="e2")
    body = _forks(client)
    (fork,) = body["forks"]
    assert fork["chain_root"] == "e1"
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e2", "e3"],
        ["e1", "e2", "e4"],
    ]
    assert fork["participant_points"] == [
        {"participant_id": "alice", "total_points": 14},
        {"participant_id": "bob", "total_points": 16},
    ]


def test_multiple_fork_nodes_share_one_root(client):
    # e1 forks into e2 and e5; e2 forks again into e3 and e4
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 5, correction_of="e1")
    _post(client, "e3", "alice", 7, correction_of="e2")
    _post(client, "e4", "alice", 9, correction_of="e2")
    _post(client, "e5", "alice", 11, correction_of="e1")
    body = _forks(client)
    assert [f["chain_root"] for f in body["forks"]] == ["e1"]
    (fork,) = body["forks"]
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["e1", "e2", "e3"],
        ["e1", "e2", "e4"],
        ["e1", "e5"],
    ]
    # one aggregated total, not per branch node
    assert fork["participant_points"] == [
        {"participant_id": "alice", "total_points": 7 + 9 + 11}
    ]


def test_historical_revision_before_fork_shows_nothing(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    # revision 2: still a plain linear chain e1 -> e2
    body = _forks(client, revision=2)
    assert body == {"revision": 2, "forks": []}

    _post(client, "e3", "alice", 20, correction_of="e1")
    body = _forks(client, revision=2)
    assert body == {"revision": 2, "forks": []}
    body = _forks(client, revision=3)
    assert [f["chain_root"] for f in body["forks"]] == ["e1"]


def test_branch_tip_superseded_at_later_revision(client):
    # fork at rev3; at rev4 one branch gets a linear follow-up correction
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    _post(client, "e4", "alice", 30, correction_of="e2")

    body = _forks(client, revision=3)
    assert [leaf["path"] for leaf in body["forks"][0]["leaves"]] == [
        ["e1", "e2"],
        ["e1", "e3"],
    ]
    body = _forks(client)
    assert [leaf["path"] for leaf in body["forks"][0]["leaves"]] == [
        ["e1", "e3"],
        ["e1", "e2", "e4"],
    ]
    assert [leaf["revision"] for leaf in body["forks"][0]["leaves"]] == [3, 4]


def test_multiple_roots_sorted_by_chain_root(client):
    # roots z1 and a1 each fork
    _post(client, "z1", "alice", 1)
    _post(client, "z2", "alice", 2, correction_of="z1")
    _post(client, "z3", "alice", 3, correction_of="z1")
    _post(client, "a1", "bob", 4)
    _post(client, "a2", "bob", 5, correction_of="a1")
    _post(client, "a3", "bob", 6, correction_of="a1")
    # a plain linear chain must not intrude
    _post(client, "m1", "carol", 7)
    _post(client, "m2", "carol", 8, correction_of="m1")

    body = _forks(client)
    assert [f["chain_root"] for f in body["forks"]] == ["a1", "z1"]
    assert [
        [(l["participant_id"], l["points"]) for l in f["leaves"]]
        for f in body["forks"]
    ] == [
        [("bob", 5), ("bob", 6)],
        [("alice", 2), ("alice", 3)],
    ]


def test_participant_points_sums_effective_leaves_only(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 100, correction_of="e1")
    _post(client, "e3", "carol", 200, correction_of="e1")
    body = _forks(client)
    (fork,) = body["forks"]
    # the superseded root (alice, 10) does not contribute
    assert fork["participant_points"] == [
        {"participant_id": "bob", "total_points": 100},
        {"participant_id": "carol", "total_points": 200},
    ]


def test_repeated_participant_across_branches_is_aggregated(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 5, correction_of="e1")
    _post(client, "e3", "bob", 7, correction_of="e1")
    _post(client, "e4", "alice", 9, correction_of="e1")
    (fork,) = _forks(client)["forks"]
    assert fork["participant_points"] == [
        {"participant_id": "alice", "total_points": 9},
        {"participant_id": "bob", "total_points": 12},
    ]


def test_default_revision_is_latest(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    body = _forks(client)
    assert body["revision"] == 2
    _post(client, "e3", "alice", 20, correction_of="e1")
    body = _forks(client)
    assert body["revision"] == 3
    assert len(body["forks"]) == 1


def test_revision_beyond_current_404(client):
    _post(client, "e1", "alice", 10)
    r = client.get("/audit/correction-forks", params={"revision": 2})
    assert r.status_code == 404
    r = client.get("/audit/correction-forks", params={"revision": 99})
    assert r.status_code == 404


def test_revision_beyond_current_404_on_empty_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "empty-forks.db"))
    db_module.init_db()
    r = TestClient(app).get("/audit/correction-forks", params={"revision": 1})
    assert r.status_code == 404


def test_negative_revision_422(client):
    r = client.get("/audit/correction-forks", params={"revision": -1})
    assert r.status_code == 422


def test_non_integer_revision_422(client):
    r = client.get("/audit/correction-forks", params={"revision": "abc"})
    assert r.status_code == 422


def test_endpoint_is_read_only(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "alice", 12, correction_of="e1")
    _post(client, "e3", "alice", 20, correction_of="e1")
    before = client.get("/events").json()
    for params in ({}, {"revision": 3}, {"revision": 2}, {"revision": 0}):
        assert client.get("/audit/correction-forks", params=params).status_code == 200
    assert client.get("/events").json() == before


def test_batch_fork_is_visible(client):
    r = client.post(
        "/events/batch",
        json=[
            {"event_id": "b1", "participant_id": "alice", "points": 10},
            {"event_id": "b2", "participant_id": "alice", "points": 12, "correction_of": "b1"},
            {"event_id": "b3", "participant_id": "bob", "points": 20, "correction_of": "b1"},
        ],
    )
    assert r.status_code == 201
    body = _forks(client)
    assert body["revision"] == 3
    (fork,) = body["forks"]
    assert fork["chain_root"] == "b1"
    assert [leaf["path"] for leaf in fork["leaves"]] == [
        ["b1", "b2"],
        ["b1", "b3"],
    ]
    assert fork["participant_points"] == [
        {"participant_id": "alice", "total_points": 12},
        {"participant_id": "bob", "total_points": 20},
    ]
