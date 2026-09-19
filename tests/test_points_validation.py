"""Regression tests for strict 64-bit ``points`` validation.

``EventIn.points`` must accept only signed 64-bit integer values (SQLite
INTEGER range). Booleans, floats, numeric strings and out-of-range values
are rejected with 422 on POST /events, POST /events/batch and
POST /events/preview before any write happens, so no event is stored and no
revision is consumed. The boundary values themselves must round-trip
through writes, idempotent replays, corrections and previews.
"""

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app.main import app

INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "points-test.db"))
    db_module.init_db()
    return TestClient(app)


def _ev(event_id, points, participant_id="alice", correction_of=None):
    payload = {
        "event_id": event_id,
        "participant_id": participant_id,
        "points": points,
    }
    if correction_of is not None:
        payload["correction_of"] = correction_of
    return payload


# Inputs JSON coerces in lax pydantic mode: bool, whole float, numeric string.
NON_INTEGER_POINTS = [
    pytest.param(True, id="bool-true"),
    pytest.param(False, id="bool-false"),
    pytest.param(1.0, id="whole-float"),
    pytest.param(1.5, id="fractional-float"),
    pytest.param("10", id="numeric-string"),
    pytest.param(None, id="null"),
]

OUT_OF_RANGE_POINTS = [
    pytest.param(INT64_MAX + 1, id="above-max"),
    pytest.param(INT64_MIN - 1, id="below-min"),
]


def _assert_points_error(r):
    assert r.status_code == 422
    # every reported error must pinpoint the points field
    for err in r.json()["detail"]:
        assert err["loc"][-1] == "points"


# --- POST /events ---------------------------------------------------------


@pytest.mark.parametrize("points", NON_INTEGER_POINTS)
def test_single_event_non_integer_points_422(client, points):
    r = client.post("/events", json=_ev("bad", points))
    _assert_points_error(r)
    assert client.get("/events/bad").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 0


def test_single_event_missing_points_422(client):
    r = client.post(
        "/events", json={"event_id": "bad", "participant_id": "alice"}
    )
    _assert_points_error(r)
    assert client.get("/events/bad").status_code == 404


@pytest.mark.parametrize("points", OUT_OF_RANGE_POINTS)
def test_single_event_out_of_range_points_422_not_500(client, points):
    r = client.post("/events", json=_ev("bad", points))
    _assert_points_error(r)
    # rejected at validation time: nothing stored, no revision consumed
    assert client.get("/events/bad").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 0


@pytest.mark.parametrize("points", [INT64_MIN, INT64_MAX, 0, -1])
def test_single_event_boundary_points_accepted(client, points):
    r = client.post("/events", json=_ev("edge", points))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["points"] == points
    assert body["revision"] == 1
    assert client.get("/events/edge").json()["points"] == points


def test_boundary_points_idempotent_replay(client):
    payload = _ev("edge-max", INT64_MAX)
    first = client.post("/events", json=payload)
    assert first.status_code == 201
    replay = client.post("/events", json=payload)
    assert replay.status_code == 200
    assert replay.json() == first.json()

    other = _ev("edge-min", INT64_MIN)
    first_min = client.post("/events", json=other)
    assert first_min.status_code == 201
    replay_min = client.post("/events", json=other)
    assert replay_min.status_code == 200
    assert replay_min.json() == first_min.json()
    assert client.get("/leaderboard").json()["revision"] == 2


def test_boundary_points_correction_chain(client):
    assert client.post("/events", json=_ev("c1", INT64_MAX)).status_code == 201
    corrected = client.post(
        "/events", json=_ev("c2", INT64_MIN, correction_of="c1")
    )
    assert corrected.status_code == 201
    lb = client.get("/leaderboard").json()
    alice = next(e for e in lb["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == INT64_MIN
    assert [e["event_id"] for e in alice["effective_events"]] == ["c2"]


# --- POST /events/batch ---------------------------------------------------


@pytest.mark.parametrize("points", NON_INTEGER_POINTS)
def test_batch_non_integer_points_422_and_rolls_back(client, points):
    events = [_ev("ok-1", 5), _ev("bad", points), _ev("ok-2", 7)]
    r = client.post("/events/batch", json=events)
    _assert_points_error(r)
    assert client.get("/events").json() == []
    assert client.get("/events/ok-1").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 0


@pytest.mark.parametrize("points", OUT_OF_RANGE_POINTS)
def test_batch_out_of_range_points_422_and_rolls_back(client, points):
    events = [_ev("ok-1", 5), _ev("bad", points)]
    r = client.post("/events/batch", json=events)
    _assert_points_error(r)
    assert client.get("/events").json() == []
    assert client.get("/events/ok-1").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 0


def test_batch_invalid_item_consumes_no_revision_after_valid_writes(client):
    assert client.post("/events", json=_ev("prior", 3)).status_code == 201
    r = client.post(
        "/events/batch", json=[_ev("ok", 4), _ev("bad", True)]
    )
    assert r.status_code == 422
    assert client.get("/events/ok").status_code == 404
    assert client.get("/leaderboard").json()["revision"] == 1


def test_batch_boundary_points_accepted_and_replayed(client):
    events = [_ev("edge-max", INT64_MAX), _ev("edge-min", INT64_MIN)]
    first = client.post("/events/batch", json=events)
    assert first.status_code == 201, r.text
    assert [e["points"] for e in first.json()] == [INT64_MAX, INT64_MIN]
    assert [e["revision"] for e in first.json()] == [1, 2]

    replay = client.post("/events/batch", json=events)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert client.get("/leaderboard").json()["revision"] == 2


def test_batch_boundary_points_correction(client):
    events = [
        _ev("b1", INT64_MAX),
        _ev("b2", INT64_MIN, correction_of="b1"),
    ]
    r = client.post("/events/batch", json=events)
    assert r.status_code == 201
    lb = client.get("/leaderboard").json()
    assert lb["revision"] == 2
    alice = next(e for e in lb["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == INT64_MIN


# --- POST /events/preview -------------------------------------------------


@pytest.mark.parametrize("points", NON_INTEGER_POINTS)
def test_preview_non_integer_points_422(client, points):
    r = client.post(
        "/events/preview",
        json={"base_revision": 0, "events": [_ev("d1", 5), _ev("d2", points)]},
    )
    _assert_points_error(r)


@pytest.mark.parametrize("points", OUT_OF_RANGE_POINTS)
def test_preview_out_of_range_points_422(client, points):
    r = client.post(
        "/events/preview",
        json={"base_revision": 0, "events": [_ev("d1", points)]},
    )
    _assert_points_error(r)
    assert client.get("/leaderboard").json()["revision"] == 0


def test_preview_boundary_points_projected(client):
    r = client.post(
        "/events/preview",
        json={
            "base_revision": 0,
            "events": [
                _ev("d-max", INT64_MAX, participant_id="alice"),
                _ev("d-min", INT64_MIN, participant_id="bob"),
            ],
        },
    )
    assert r.status_code == 200, r.text
    projected = r.json()["projected_leaderboard"]
    assert projected["revision"] == 2
    by_id = {
        e["participant_id"]: e["total_points"] for e in projected["entries"]
    }
    assert by_id == {"alice": INT64_MAX, "bob": INT64_MIN}


def test_preview_boundary_points_correction(client):
    # baseline event at the max, a draft correction down to the min
    assert client.post("/events", json=_ev("e1", INT64_MAX)).status_code == 201
    r = client.post(
        "/events/preview",
        json={
            "base_revision": 1,
            "events": [_ev("d1", INT64_MIN, correction_of="e1")],
        },
    )
    assert r.status_code == 200, r.text
    projected = r.json()["projected_leaderboard"]
    alice = next(e for e in projected["entries"] if e["participant_id"] == "alice")
    assert alice["total_points"] == INT64_MIN
    assert [e["event_id"] for e in alice["effective_events"]] == ["d1"]
    # dry run: ledger untouched
    assert client.get("/leaderboard").json()["revision"] == 1
