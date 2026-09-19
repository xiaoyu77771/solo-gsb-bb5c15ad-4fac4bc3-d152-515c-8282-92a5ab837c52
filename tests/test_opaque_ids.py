"""Tests for reading back opaque IDs through URL-encoded paths.

event_id / participant_id are opaque strings: they may contain "/" or equal
a static route name ("changes", "compare"). Every such ID must round-trip
through percent-encoding, while static routes keep their literal behaviour.
"""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "opaque-ids-test.db"))
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


# ---------------------------------------------------------------------------
# event_id equal to a static route name
# ---------------------------------------------------------------------------


def test_event_named_changes_readable_when_encoded(client):
    ev = _post(client, "changes", "alice", 3)
    r = client.get("/events/%63hanges")
    assert r.status_code == 200
    assert r.json() == ev


def test_event_named_changes_readable_fully_encoded(client):
    ev = _post(client, "changes", "alice", 3)
    r = client.get("/events/%63%68%61%6e%67%65%73")
    assert r.status_code == 200
    assert r.json() == ev


def test_static_changes_route_unchanged(client):
    ev = _post(client, "changes", "alice", 3)
    # unencoded literal path still hits the changes feed, not the event
    assert client.get("/events/changes").status_code == 422
    r = client.get("/events/changes", params={"from_revision": 0, "to_revision": 1})
    assert r.status_code == 200
    assert r.json()["changes"][0]["after"] == ev


def test_events_named_like_post_only_routes(client):
    # "batch"/"preview" are POST-only static routes; GET reads the events
    for eid in ("batch", "preview"):
        ev = _post(client, eid, "alice", 1)
        r = client.get(f"/events/{eid}")
        assert r.status_code == 200
        assert r.json() == ev


# ---------------------------------------------------------------------------
# event_id containing reserved characters
# ---------------------------------------------------------------------------


def test_event_id_with_slash_round_trips(client):
    ev = _post(client, "a/b", "bob", 5)
    r = client.get("/events/a%2Fb")
    assert r.status_code == 200
    assert r.json() == ev


def test_event_id_with_multiple_slashes_round_trips(client):
    ev = _post(client, "a/b/c", "bob", 5)
    r = client.get("/events/a%2Fb%2Fc")
    assert r.status_code == 200
    assert r.json() == ev


def test_event_id_with_percent_space_and_unicode(client):
    for eid, encoded in [
        ("a%b", "a%25b"),
        ("a b", "a%20b"),
        ("阿/丽", "%E9%98%BF%2F%E4%B8%BD"),
    ]:
        ev = _post(client, eid, "p", 1)
        r = client.get(f"/events/{encoded}")
        assert r.status_code == 200, eid
        assert r.json() == ev


def test_unencoded_slash_does_not_match_event(client):
    _post(client, "a/b", "bob", 5)
    # an unencoded "/" is a path separator, not part of the ID
    assert client.get("/events/a/b").status_code == 404


def test_unknown_event_ids_still_404(client):
    _post(client, "a/b", "bob", 5)
    assert client.get("/events/missing").status_code == 404
    assert client.get("/events/missing%2Fid").status_code == 404
    assert client.get("/events/a%2Fc").status_code == 404


def test_encoded_id_appears_decoded_in_404_detail(client):
    r = client.get("/events/ghost%2Fid")
    assert r.status_code == 404
    assert r.json()["detail"] == "event 'ghost/id' not found"


# ---------------------------------------------------------------------------
# participant_id containing "/"
# ---------------------------------------------------------------------------


@pytest.fixture()
def slash_client(client):
    _post(client, "e1", "x/y", 7)
    _post(client, "e2", "alice", 10)
    return client


def test_proof_for_participant_with_slash(slash_client):
    r = slash_client.get("/leaderboard/proof/x%2Fy")
    assert r.status_code == 200
    body = r.json()
    assert body["entry"]["participant_id"] == "x/y"
    assert body["entry"]["total_points"] == 7
    # same commitment root as the whole-leaderboard endpoint
    commitment = slash_client.get("/leaderboard/commitment").json()
    assert body["root_hash"] == commitment["root_hash"]


def test_score_breakdown_for_participant_with_slash(slash_client):
    r = slash_client.get("/participants/x%2Fy/score-breakdown")
    assert r.status_code == 200
    body = r.json()
    assert body["total_points"] == 7
    assert [c["chain_root"] for c in body["contributions"]] == ["e1"]


def test_threshold_intervals_for_participant_with_slash(slash_client):
    r = slash_client.get(
        "/participants/x%2Fy/threshold-intervals",
        params={"threshold": 7, "from_revision": 0, "to_revision": 2},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["participant_id"] == "x/y"
    assert body["intervals"] == [
        {"start_revision": 1, "end_revision": 2, "start_points": 7, "end_points": 7}
    ]


def test_unencoded_participant_slash_does_not_match(slash_client):
    assert slash_client.get("/participants/x/y/score-breakdown").status_code == 404
    assert slash_client.get("/leaderboard/proof/x/y").status_code == 404


def test_unknown_encoded_participant_behaves_like_plain(slash_client):
    # score-breakdown: unknown participants report zero, never 404
    r = slash_client.get("/participants/no%2Fbody/score-breakdown")
    assert r.status_code == 200
    assert r.json()["total_points"] == 0
    assert r.json()["contributions"] == []
    # proof: absent participant -> 404
    assert slash_client.get("/leaderboard/proof/no%2Fbody").status_code == 404


# ---------------------------------------------------------------------------
# participant_id equal to a static route name
# ---------------------------------------------------------------------------


def test_participant_named_compare(client):
    _post(client, "e1", "compare", 4)
    r = client.get("/participants/compare/score-breakdown")
    assert r.status_code == 200
    assert r.json()["total_points"] == 4
    r = client.get(
        "/participants/compare/threshold-intervals",
        params={"threshold": 4, "from_revision": 0, "to_revision": 1},
    )
    assert r.status_code == 200
    assert r.json()["participant_id"] == "compare"


def test_static_compare_route_unchanged(client):
    _post(client, "e1", "compare", 4)
    _post(client, "e2", "alice", 6)
    r = client.get(
        "/participants/compare",
        params={
            "left_id": "compare",
            "right_id": "alice",
            "from_revision": 0,
            "to_revision": 2,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["left_id"] == "compare"
    assert body["intervals"][-1]["leader"] == "right"


# ---------------------------------------------------------------------------
# routing hygiene
# ---------------------------------------------------------------------------


def test_trailing_slash_redirect_still_works(client):
    _post(client, "e1", "alice", 1)
    r = client.get("/events/changes/", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"].endswith("/events/changes")


def test_openapi_documents_encoded_ids(client):
    spec = client.get("/openapi.json").json()
    for path, param in [
        ("/events/{event_id}", "event_id"),
        ("/leaderboard/proof/{participant_id}", "participant_id"),
        ("/participants/{participant_id}/score-breakdown", "participant_id"),
        ("/participants/{participant_id}/threshold-intervals", "participant_id"),
    ]:
        assert path in spec["paths"], path
        params = spec["paths"][path]["get"]["parameters"]
        (p,) = [p for p in params if p["name"] == param]
        assert "%2F" in p["description"], path
    # static routes untouched in the schema
    assert "/events/changes" in spec["paths"]
    assert "/participants/compare" in spec["paths"]
