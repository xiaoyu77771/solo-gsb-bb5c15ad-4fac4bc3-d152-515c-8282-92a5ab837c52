"""Deterministic Merkle commitments over revisioned leaderboard snapshots.

Encoding (shared by ``/leaderboard/commitment`` and
``/leaderboard/proof/{participant_id}`` so clients can recompute the root):

* Leaves follow leaderboard order — ``(-total_points, participant_id)`` — so
  leaf index ``i`` is rank ``i + 1``.
* The leaf *payload* is canonical JSON of
  ``{"rank", "participant_id", "total_points", "event_ids"}``: keys sorted,
  no whitespace, UTF-8; ``event_ids`` lists the participant's effective
  events ordered by revision ascending.
* Leaf digest  = ``SHA-256(0x00 || payload)``.
* Inner digest = ``SHA-256(0x01 || left_digest || right_digest)``; the 0x00/
  0x01 prefixes are domain separation so a leaf can never parse as a node.
* At an odd level the last node is paired with itself.
* The single-leaf root is the leaf digest itself; the empty-tree root is
  fixed at ``SHA-256("")`` (the digest of the empty byte string).
"""

from __future__ import annotations

import hashlib
import json
from typing import List, NamedTuple, Sequence

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"

#: SHA-256 of the empty byte string — the fixed root of the empty leaderboard.
EMPTY_ROOT_HEX = hashlib.sha256(b"").hexdigest()


class ProofStep(NamedTuple):
    """One sibling on the leaf-to-root path.

    ``position`` locates the sibling relative to the running hash at that
    level: ``left``/``right`` say the sibling sits left/right of it, and
    ``self`` marks the duplicated last node of an odd layer (the running hash
    is paired with itself).
    """

    level: int
    position: str  # "left" | "right" | "self"
    sibling: bytes


def encode_leaf(
    rank: int,
    participant_id: str,
    total_points: int,
    event_ids: Sequence[str],
) -> bytes:
    """Canonical JSON payload bytes for one leaderboard entry."""
    payload = {
        "rank": rank,
        "participant_id": participant_id,
        "total_points": total_points,
        "event_ids": list(event_ids),
    }
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def leaf_digest(
    rank: int,
    participant_id: str,
    total_points: int,
    event_ids: Sequence[str],
) -> bytes:
    """Domain-separated SHA-256 leaf digest for one leaderboard entry."""
    return hashlib.sha256(
        LEAF_PREFIX
        + encode_leaf(rank, participant_id, total_points, event_ids)
    ).digest()


def node_digest(left: bytes, right: bytes) -> bytes:
    """Parent digest for two 32-byte child digests."""
    return hashlib.sha256(NODE_PREFIX + left + right).digest()


def build_levels(leaves: Sequence[bytes]) -> List[List[bytes]]:
    """All Merkle levels bottom-up; ``levels[0]`` is the leaf layer.

    Odd layers duplicate their last node (it is paired with itself). For a
    single leaf the returned list contains just that one layer; an empty
    input yields ``[[]]`` (use :data:`EMPTY_ROOT_HEX` for its root).
    """
    levels: List[List[bytes]] = [list(leaves)]
    while len(levels[-1]) > 1:
        current = levels[-1]
        nxt: List[bytes] = []
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else current[i]
            nxt.append(node_digest(left, right))
        levels.append(nxt)
    return levels


def root_from_levels(levels: Sequence[Sequence[bytes]]) -> bytes:
    """Root digest of levels produced by :func:`build_levels`."""
    if not levels or not levels[0]:
        return hashlib.sha256(b"").digest()
    return levels[-1][0]


def merkle_proof(levels: Sequence[Sequence[bytes]], leaf_index: int) -> List[ProofStep]:
    """Bottom-up sibling path for ``leaf_index``.

    For every level except the root, the sibling is the XOR-neighbour at the
    same level. A level's last node with no right neighbour pairs with
    itself (``position == "self"``).
    """
    if not levels[0]:
        raise ValueError("cannot build a proof over the empty tree")
    if not 0 <= leaf_index < len(levels[0]):
        raise ValueError(f"leaf_index {leaf_index} out of range 0..{len(levels[0]) - 1}")

    steps: List[ProofStep] = []
    index = leaf_index
    for level, layer in enumerate(levels[:-1]):
        if index % 2 == 0:
            if index + 1 < len(layer):
                steps.append(ProofStep(level, "right", layer[index + 1]))
            else:
                # Odd layer: last node paired with itself.
                steps.append(ProofStep(level, "self", layer[index]))
        else:
            steps.append(ProofStep(level, "left", layer[index - 1]))
        index //= 2
    return steps


def apply_proof(
    digest: bytes, leaf_index: int, steps: Sequence[ProofStep]
) -> bytes:
    """Recompute the root from one leaf digest and its proof steps.

    ``leaf_index`` must be the index the proof was generated for; each step's
    ``position`` gives the sibling's side relative to the running hash.
    """
    index = leaf_index
    for step in steps:
        if step.position == "left":
            assert index % 2 == 1, "proof step 'left' requires an odd node index"
            digest = node_digest(step.sibling, digest)
        elif step.position == "right":
            assert index % 2 == 0, "proof step 'right' requires an even node index"
            digest = node_digest(digest, step.sibling)
        elif step.position == "self":
            # The node is an even-indexed last node of an odd-sized layer.
            assert index % 2 == 0, "proof step 'self' requires an even node index"
            digest = node_digest(digest, digest)
        else:  # pragma: no cover - proofs only ever carry the three literals
            raise ValueError(f"unknown proof position: {step.position!r}")
        index //= 2
    return digest
