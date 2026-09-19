"""Tests for leaderboard snapshot commitments and Merkle inclusion proofs.

The proofs are verified by *independently* re-implementing the documented
encoding here (canonical JSON + SHA-256 domain prefixes), so these tests pin
the wire format clients must reproduce.
"""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

import app.db as db_module
from app import commitment
from app.main import app

EMPTY_ROOT = hashlib.sha256(b"").hexdigest()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """TestClient backed by an isolated SQLite file."""
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "commitment-test.db"))
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


def _client_leaf(entry):
    """Leaf digest recomputed purely from the entry payload (documented rule)."""
    payload = json.dumps(
        {
            "rank": entry["rank"],
            "participant_id": entry["participant_id"],
            "total_points": entry["total_points"],
            "event_ids": [e["event_id"] for e in entry["effective_events"]],
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(b"\x00" + payload).digest()


def _verify_proof(body) -> str:
    """Recompute the root from one proof response; return lowercase hex."""
    digest = _client_leaf(body["entry"])
    index = body["leaf_index"]
    for step in body["proof"]:
        sibling = bytes.fromhex(step["sibling"])
        position = step["position"]
        if position == "left":
            assert index % 2 == 1
            digest = hashlib.sha256(b"\x01" + sibling + digest).digest()
        elif position == "right":
            assert index % 2 == 0
            digest = hashlib.sha256(b"\x01" + digest + sibling).digest()
        else:
            assert position == "self"
            assert index % 2 == 0
            digest = hashlib.sha256(b"\x01" + digest + digest).digest()
        index //= 2
    return digest.hex()


def _seed_three(client):
    """alice and carol tie on 10 (alice ranks first by id); bob has 7."""
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    rev3 = _post(client, "e3", "carol", 10)["revision"]
    return rev3


def test_empty_leaderboard_root_is_sha256_of_empty_bytes(client):
    for url in ("/leaderboard/commitment", "/leaderboard/commitment?revision=0"):
        body = client.get(url).json()
        assert body == {"revision": 0, "leaf_count": 0, "root_hash": EMPTY_ROOT}
    assert EMPTY_ROOT == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    # No membership proof exists against the empty snapshot.
    assert client.get("/leaderboard/proof/alice?revision=0").status_code == 404
    assert client.get("/leaderboard/proof/alice").status_code == 404


def test_leaf_count_revision_and_root_recomputable(client):
    rev = _seed_three(client)
    r = client.get(f"/leaderboard/commitment?revision={rev}")
    assert r.status_code == 200
    body = r.json()
    assert body["revision"] == rev
    assert body["leaf_count"] == 3

    # Root must match the independently-built tree over /leaderboard entries.
    lb = client.get(f"/leaderboard?revision={rev}").json()
    levels = commitment.build_levels([_client_leaf(e) for e in lb["entries"]])
    assert body["root_hash"] == commitment.root_from_levels(levels).hex()
    assert body["root_hash"] != EMPTY_ROOT


def test_default_commitment_tracks_latest_revision(client):
    rev = _seed_three(client)
    body = client.get("/leaderboard/commitment").json()
    assert body["revision"] == rev

    rev4 = _post(client, "e4", "dave", 99)["revision"]
    body = client.get("/leaderboard/commitment").json()
    assert body["revision"] == rev4
    assert body["leaf_count"] == 4


def test_proof_entry_matches_leaderboard_and_index_is_rank_minus_one(client):
    rev = _seed_three(client)
    lb = client.get(f"/leaderboard?revision={rev}").json()
    by_id = {e["participant_id"]: e for e in lb["entries"]}

    for expected_index, pid in enumerate(("alice", "carol", "bob")):
        r = client.get(f"/leaderboard/proof/{pid}?revision={rev}")
        assert r.status_code == 200
        body = r.json()
        assert body["revision"] == rev
        assert body["leaf_index"] == expected_index
        assert body["leaf_count"] == 3
        assert body["entry"] == by_id[pid]


def test_every_proof_recomputes_to_commitment_root(client):
    rev = _seed_three(client)
    root = client.get(f"/leaderboard/commitment?revision={rev}").json()["root_hash"]

    for pid in ("alice", "carol", "bob"):
        body = client.get(f"/leaderboard/proof/{pid}?revision={rev}").json()
        assert body["root_hash"] == root  # endpoints agree on the same revision
        assert _verify_proof(body) == root


def test_odd_layer_last_node_pairs_with_itself(client):
    # 3 leaves: layer 0 has an odd last node; 5 leaves forces the same at
    # layer 0, and leaf 4 must climb via "self" pairings.
    _seed_three(client)
    _post(client, "e4", "dave", 3)
    _post(client, "e5", "erin", 1)
    rev = 5

    root = client.get(f"/leaderboard/commitment?revision={rev}").json()["root_hash"]
    order = client.get(f"/leaderboard?revision={rev}").json()["entries"]
    assert [e["participant_id"] for e in order] == [
        "alice",
        "carol",
        "bob",
        "dave",
        "erin",
    ]

    # Last leaf (erin, index 4): first step must duplicate itself.
    body = client.get(f"/leaderboard/proof/erin?revision={rev}").json()
    assert body["leaf_index"] == 4
    first = body["proof"][0]
    assert first["level"] == 0
    assert first["position"] == "self"
    assert first["sibling"] == _client_leaf(body["entry"]).hex()
    assert _verify_proof(body) == root

    # Every other leaf verifies too (covers all sibling positions).
    for entry in order:
        body = client.get(
            f"/leaderboard/proof/{entry['participant_id']}?revision={rev}"
        ).json()
        assert _verify_proof(body) == root


def test_proof_step_layout_for_three_leaves(client):
    rev = _seed_three(client)
    lb = client.get(f"/leaderboard?revision={rev}").json()
    digests = [_client_leaf(e) for e in lb["entries"]]
    node0 = hashlib.sha256(b"\x01" + digests[0] + digests[1]).digest()
    node1 = hashlib.sha256(b"\x01" + digests[2] + digests[2]).digest()

    alice = client.get(f"/leaderboard/proof/alice?revision={rev}").json()
    assert [(s["level"], s["position"]) for s in alice["proof"]] == [
        (0, "right"),
        (1, "right"),
    ]
    assert bytes.fromhex(alice["proof"][0]["sibling"]) == digests[1]
    assert bytes.fromhex(alice["proof"][1]["sibling"]) == node1

    carol = client.get(f"/leaderboard/proof/carol?revision={rev}").json()
    assert [(s["level"], s["position"]) for s in carol["proof"]] == [
        (0, "left"),
        (1, "right"),
    ]
    assert bytes.fromhex(carol["proof"][0]["sibling"]) == digests[0]
    assert bytes.fromhex(carol["proof"][1]["sibling"]) == node1

    bob = client.get(f"/leaderboard/proof/bob?revision={rev}").json()
    assert [(s["level"], s["position"]) for s in bob["proof"]] == [
        (0, "self"),
        (1, "left"),
    ]
    assert bytes.fromhex(bob["proof"][1]["sibling"]) == node0


def test_single_leaf_root_is_leaf_digest_and_proof_empty(client):
    _post(client, "e1", "alice", 10)
    body = client.get("/leaderboard/commitment").json()
    assert body["leaf_count"] == 1

    expected_payload = (
        '{"event_ids":["e1"],"participant_id":"alice","rank":1,"total_points":10}'
    )
    expected = hashlib.sha256(b"\x00" + expected_payload.encode("utf-8")).hexdigest()
    assert body["root_hash"] == expected

    proof = client.get("/leaderboard/proof/alice").json()
    assert proof["leaf_index"] == 0
    assert proof["proof"] == []
    assert proof["root_hash"] == expected
    assert _verify_proof(proof) == expected


def test_two_leaves_pair_without_self_step(client):
    _post(client, "e1", "alice", 10)
    _post(client, "e2", "bob", 7)
    proof = client.get("/leaderboard/proof/bob").json()
    assert [(s["level"], s["position"]) for s in proof["proof"]] == [(0, "left")]
    assert _verify_proof(proof) == client.get("/leaderboard/commitment").json()[
        "root_hash"
    ]


def test_utf8_participant_id_payload(client):
    _post(client, "e1", "alïce", 10)
    body = client.get("/leaderboard/commitment").json()
    # Canonical JSON carries the raw UTF-8 character (no \u escape).
    expected_payload = (
        '{"event_ids":["e1"],"participant_id":"alïce","rank":1,"total_points":10}'
    ).encode("utf-8")
    assert body["root_hash"] == hashlib.sha256(
        b"\x00" + expected_payload
    ).hexdigest()


def test_absent_or_not_yet_born_participant_returns_404(client):
    rev = _seed_three(client)
    assert client.get(f"/leaderboard/proof/nobody?revision={rev}").status_code == 404

    # dave only appears at revision 4.
    _post(client, "e4", "dave", 99)
    r = client.get(f"/leaderboard/proof/dave?revision={rev}")
    assert r.status_code == 404


def test_correction_changes_effective_event_ids_and_root(client):
    _post(client, "e1", "alice", 10)
    rev1 = 1
    root1 = client.get(f"/leaderboard/commitment?revision={rev1}").json()["root_hash"]

    _post(client, "e2", "alice", 12, correction_of="e1")
    rev2 = 2
    root2 = client.get(f"/leaderboard/commitment?revision={rev2}").json()["root_hash"]
    assert root1 != root2

    proof = client.get(f"/leaderboard/proof/alice?revision={rev2}").json()
    assert [e["event_id"] for e in proof["entry"]["effective_events"]] == ["e2"]
    assert proof["entry"]["total_points"] == 12
    assert _verify_proof(proof) == root2


def test_historical_snapshot_immutable_under_later_writes(client):
    rev = _seed_three(client)
    pinned = client.get(f"/leaderboard/commitment?revision={rev}").json()
    pinned_proof = client.get(f"/leaderboard/proof/bob?revision={rev}").json()

    # Later writes: one correction reshuffles alice's events, more join, ranks move.
    _post(client, "e4", "alice", 100)
    _post(client, "e5", "zoe", 50)
    _post(client, "e6", "xander", 12, correction_of="e1")

    again = client.get(f"/leaderboard/commitment?revision={rev}").json()
    assert again == pinned
    proof_again = client.get(f"/leaderboard/proof/bob?revision={rev}").json()
    assert proof_again == pinned_proof
    assert _verify_proof(proof_again) == pinned["root_hash"]


def test_commitment_and_proofs_stay_consistent_for_every_revision(client):
    for i in range(1, 7):
        _post(client, f"e{i}", f"p{i}", i * 3)
        root = client.get(f"/leaderboard/commitment?revision={i}").json()["root_hash"]
        lb = client.get(f"/leaderboard?revision={i}").json()
        for entry in lb["entries"]:
            body = client.get(
                f"/leaderboard/proof/{entry['participant_id']}?revision={i}"
            ).json()
            assert body["root_hash"] == root
            assert _verify_proof(body) == root


def test_invalid_revision_status_codes(client):
    _seed_three(client)
    for path in (
        "/leaderboard/commitment?revision=999",
        "/leaderboard/proof/alice?revision=999",
        "/leaderboard/commitment?revision=-1",
        "/leaderboard/proof/alice?revision=-1",
        "/leaderboard/commitment?revision=abc",
        "/leaderboard/proof/alice?revision=abc",
    ):
        r = client.get(path)
        assert r.status_code in (404, 422), path
    assert client.get("/leaderboard/commitment?revision=999").status_code == 404
    assert client.get("/leaderboard/proof/alice?revision=999").status_code == 404
    assert client.get("/leaderboard/commitment?revision=-1").status_code == 422
    assert client.get("/leaderboard/proof/alice?revision=-1").status_code == 422
    assert client.get("/leaderboard/commitment?revision=abc").status_code == 422
