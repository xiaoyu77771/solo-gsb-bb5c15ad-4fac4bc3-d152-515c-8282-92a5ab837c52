"""Core ledger logic: idempotent immutable writes + revisioned reads."""

from __future__ import annotations

import json
import sqlite3
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional, Sequence, Tuple, Union

from fastapi import HTTPException

from . import commitment
from .cursor import decode_cursor, encode_cursor
from .db import read_tx, write_tx
from .schemas import (
    CompareIntervalOut,
    CorrectionForkLeafOut,
    CorrectionForkOut,
    CorrectionForkParticipantPoints,
    CorrectionForksOut,
    EffectiveEvent,
    EventBatchIn,
    EventChangeOut,
    EventChangesOut,
    EventIn,
    EventOut,
    EventPreviewIn,
    EventPreviewOut,
    LeaderboardChangeOut,
    LeaderboardChangesOut,
    LeaderboardCommitmentOut,
    LeaderboardEntry,
    LeaderboardOut,
    LeaderboardPageOut,
    LeaderboardProofOut,
    MerkleProofStepOut,
    ParticipantCompareOut,
    RankPosition,
    RewardPreviewIn,
    RewardPreviewOut,
    RewardWinnerOut,
    ScoreBreakdownOut,
    ScoreContribution,
    ThresholdIntervalOut,
    ThresholdIntervalsOut,
)


def _canonical_payload(ev: EventIn) -> str:
    """Canonical JSON of the client-supplied payload (key order independent)."""
    return json.dumps(ev.model_dump(), sort_keys=True, separators=(",", ":"))


def _row_to_event(row: sqlite3.Row) -> EventOut:
    return EventOut(
        event_id=row["event_id"],
        participant_id=row["participant_id"],
        points=row["points"],
        correction_of=row["correction_of"],
        revision=row["revision"],
        created_at=row["created_at"],
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    )


def create_event(ev: EventIn) -> Tuple[EventOut, bool]:
    """Insert an immutable event.

    Returns (event, created). If ``event_id`` already exists:
      * identical payload  -> replay: return the stored event, created=False
      * different payload  -> 409 Conflict
    """
    if ev.correction_of is not None and ev.correction_of == ev.event_id:
        raise HTTPException(
            status_code=422, detail="correction_of must not equal event_id"
        )

    payload = _canonical_payload(ev)

    with write_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        if row is not None:
            if row["payload"] == payload:
                return _row_to_event(row), False
            raise HTTPException(
                status_code=409,
                detail=f"event_id '{ev.event_id}' already exists with a different payload",
            )

        if ev.correction_of is not None:
            target = conn.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (ev.correction_of,)
            ).fetchone()
            if target is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"correction_of references unknown event '{ev.correction_of}'",
                )

        revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM events"
        ).fetchone()[0]
        created_at = _utc_now()
        conn.execute(
            "INSERT INTO events (event_id, participant_id, points, correction_of, payload, revision, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                ev.event_id,
                ev.participant_id,
                ev.points,
                ev.correction_of,
                payload,
                revision,
                created_at,
            ),
        )
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (ev.event_id,)
        ).fetchone()
        return _row_to_event(row), True


def create_events(batch: EventBatchIn) -> Tuple[list[EventOut], bool]:
    """Atomically insert a batch of 1–100 immutable events.

    Revisions are assigned as one contiguous block, in request order:
    the first new event gets ``MAX(revision) + 1`` and so on. Validation
    and insertion all happen in a single ``BEGIN IMMEDIATE`` transaction,
    so any failure rolls the whole batch back — no event is stored and no
    revision number is consumed.

    ``correction_of`` may reference an event already stored in the ledger
    or an event appearing *earlier* in the same batch; referencing a later
    item (or an unknown id) fails with 422.

    Returns (events, created) with ``events`` in request order:
      * none of the ids existed -> insert all, created=True  (HTTP 201)
      * every id existed with an identical payload -> replay the stored
        events in request order, created=False                    (HTTP 200)
      * some but not all ids existed, or any existing id carries a
        different payload -> 409 Conflict
    """
    events = batch.root

    # Cheap self-checks; the schema normally rejects these before the call.
    if not events:
        raise HTTPException(status_code=422, detail="batch must contain 1–100 events")
    if len(events) > 100:
        raise HTTPException(status_code=422, detail="batch must contain 1–100 events")
    seen_ids: set[str] = set()
    for ev in events:
        if ev.event_id in seen_ids:
            raise HTTPException(
                status_code=422,
                detail=f"event_id '{ev.event_id}' appears more than once in the batch",
            )
        seen_ids.add(ev.event_id)
        if ev.correction_of is not None and ev.correction_of == ev.event_id:
            raise HTTPException(
                status_code=422, detail="correction_of must not equal event_id"
            )

    payloads = [_canonical_payload(ev) for ev in events]
    index_by_id = {ev.event_id: i for i, ev in enumerate(events)}
    created_at = _utc_now()

    with write_tx() as conn:
        rows = conn.execute(
            "SELECT event_id, payload FROM events"
            " WHERE event_id IN (%s)" % ",".join("?" for _ in events),
            [ev.event_id for ev in events],
        ).fetchall()
        stored = {row["event_id"]: row["payload"] for row in rows}

        if stored:
            # Conflict detection first: a same-id/different-payload clash is a
            # 409 even when an invalid reference is also present.
            for ev, payload in zip(events, payloads):
                if ev.event_id in stored and stored[ev.event_id] != payload:
                    raise HTTPException(
                        status_code=409,
                        detail=(
                            f"event_id '{ev.event_id}' already exists "
                            "with a different payload"
                        ),
                    )
            if len(stored) < len(events):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"batch is a partial replay: {len(stored)} of {len(events)} "
                        "events already exist; resend with the full original set "
                        "or only new event_ids"
                    ),
                )

        if len(stored) == len(events):
            # Full idempotent replay: return stored events in request order.
            rows_by_id = {
                row["event_id"]: row
                for row in conn.execute(
                    "SELECT * FROM events WHERE event_id IN (%s)"
                    % ",".join("?" for _ in events),
                    [ev.event_id for ev in events],
                ).fetchall()
            }
            return [_row_to_event(rows_by_id[ev.event_id]) for ev in events], False

        # All new: validate every reference first (against the ledger and
        # against earlier batch items), then insert as one contiguous
        # revision block in request order.
        for i, ev in enumerate(events):
            if ev.correction_of is None:
                continue
            target_idx = index_by_id.get(ev.correction_of)
            if target_idx is not None:
                if target_idx >= i:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"event '{ev.event_id}' has correction_of "
                            f"'{ev.correction_of}' referencing a later event "
                            "in the same batch; only earlier events may be "
                            "referenced"
                        ),
                    )
            else:
                target = conn.execute(
                    "SELECT 1 FROM events WHERE event_id = ?",
                    (ev.correction_of,),
                ).fetchone()
                if target is None:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            "correction_of references unknown event "
                            f"'{ev.correction_of}'"
                        ),
                    )

        next_revision = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) + 1 FROM events"
        ).fetchone()[0]
        results: list[EventOut] = []
        for i, ev in enumerate(events):
            revision = next_revision + i
            conn.execute(
                "INSERT INTO events (event_id, participant_id, points, correction_of, payload, revision, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ev.event_id,
                    ev.participant_id,
                    ev.points,
                    ev.correction_of,
                    payloads[i],
                    revision,
                    created_at,
                ),
            )
            results.append(
                EventOut(
                    event_id=ev.event_id,
                    participant_id=ev.participant_id,
                    points=ev.points,
                    correction_of=ev.correction_of,
                    revision=revision,
                    created_at=created_at,
                )
            )
        return results, True


def get_event(event_id: str) -> EventOut:
    with read_tx() as conn:
        row = conn.execute(
            "SELECT * FROM events WHERE event_id = ?", (event_id,)
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"event '{event_id}' not found")
    return _row_to_event(row)


def list_events() -> list[EventOut]:
    with read_tx() as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY revision ASC"
        ).fetchall()
    return [_row_to_event(r) for r in rows]


def current_revision() -> int:
    with read_tx() as conn:
        return conn.execute("SELECT COALESCE(MAX(revision), 0) FROM events").fetchone()[0]


def _resolve_revision(conn: sqlite3.Connection, revision: Optional[int]) -> int:
    max_rev = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM events"
    ).fetchone()[0]
    if revision is None:
        return max_rev
    if revision < 1 or revision > max_rev:
        raise HTTPException(status_code=404, detail=f"revision {revision} not found")
    return revision


def _effective_rows(conn: sqlite3.Connection, revision: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT e.event_id, e.participant_id, e.points, e.revision, e.correction_of
        FROM events e
        WHERE e.revision <= ?
          AND NOT EXISTS (
                SELECT 1 FROM events c
                WHERE c.correction_of = e.event_id
                  AND c.revision <= ?
          )
        ORDER BY e.participant_id ASC, e.revision ASC
        """,
        (revision, revision),
    ).fetchall()


def _aggregate(
    rows: list[Union[sqlite3.Row, Mapping]],
) -> Tuple[list[Tuple[str, int]], dict[str, list[EffectiveEvent]]]:
    """Per-participant totals ordered by (-total_points, participant_id).

    ``rows`` may be sqlite rows or plain mappings — anything supporting
    ``row["column"]`` for event_id/participant_id/points/revision/
    correction_of (the scenario preview passes projected draft dicts).
    """
    totals: dict[str, int] = {}
    details: dict[str, list[EffectiveEvent]] = {}
    for r in rows:
        pid = r["participant_id"]
        totals[pid] = totals.get(pid, 0) + r["points"]
        details.setdefault(pid, []).append(
            EffectiveEvent(
                event_id=r["event_id"],
                points=r["points"],
                revision=r["revision"],
                correction_of=r["correction_of"],
            )
        )
    ordered = sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))
    return ordered, details


def _leaderboard_out(
    revision: int,
    ordered: list[Tuple[str, int]],
    details: dict[str, list[EffectiveEvent]],
) -> LeaderboardOut:
    """Full leaderboard over an ordered aggregate, ranks starting at 1."""
    entries = [
        LeaderboardEntry(
            rank=i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(ordered)
    ]
    return LeaderboardOut(revision=revision, entries=entries)


def _rank_positions(
    ordered: list[Tuple[str, int]],
) -> dict[str, RankPosition]:
    """``{participant_id: (rank, total_points)}`` for an ordered aggregate."""
    return {
        pid: RankPosition(rank=i + 1, total_points=total)
        for i, (pid, total) in enumerate(ordered)
    }


def _diff_snapshots(
    before_map: dict[str, RankPosition],
    after_map: dict[str, RankPosition],
) -> list[LeaderboardChangeOut]:
    """Participants whose rank or total differs between two snapshots.

    Only changed participants are reported, ordered by participant_id; an
    absent side counts as 0 points for ``points_delta``.
    """
    changes: list[LeaderboardChangeOut] = []
    for pid in sorted(set(before_map) | set(after_map)):
        before = before_map.get(pid)
        after = after_map.get(pid)
        before_total = before.total_points if before is not None else 0
        after_total = after.total_points if after is not None else 0
        if before != after:  # rank or total_points changed (covers entry/exit)
            changes.append(
                LeaderboardChangeOut(
                    participant_id=pid,
                    before=before,
                    after=after,
                    points_delta=after_total - before_total,
                )
            )
    return changes


# --- SnapshotTimeline: one-pass per-revision replay engine -----------------


@dataclass(frozen=True)
class _ReplayEvent:
    """Minimal event record needed by the replay engine."""

    event_id: str
    participant_id: str
    points: int
    correction_of: Optional[str]
    revision: int


@dataclass(frozen=True)
class OwnershipTransfer:
    """An event re-attributed from one participant to another.

    Recorded at the revision of the event's *first* direct correction when
    that correction names a different participant. The old owner is debited
    and the new owner credited at the same revision; same-participant
    corrections (plain value corrections) produce no transfer.
    """

    revision: int
    event_id: str
    from_participant: str
    to_participant: str
    points: int


@dataclass(frozen=True)
class RevisionEffect:
    """Everything that becomes true at one revision during the replay.

    * ``added``: events written at this revision — effective immediately;
    * ``first_corrected``: events receiving their first direct correction,
      i.e. leaving the effective set exactly now (later fork branches
      correct the same ancestor again but never re-enter this tuple);
    * ``transfers``: cross-participant re-attributions taking effect now.
    """

    revision: int
    added: Tuple[str, ...]
    first_corrected: Tuple[str, ...]
    transfers: Tuple[OwnershipTransfer, ...]


class SnapshotTimeline:
    """Replay one event set in revision order, advancing one revision at a time.

    The whole engine is a single pass: the caller reads the relevant event
    set with *one* range query (``revision <= to_revision`` ordered by
    revision) and the engine consumes it incrementally — there is never a
    re-query per snapshot revision. Because every correction target already
    exists (targets are validated at write time) and carries a strictly
    smaller revision than the correction, no look-ahead is needed: processing
    events in revision order reproduces exactly what the ``NOT EXISTS``
    snapshot query computes for any revision R.

    State maintained during the replay:

    * an event becomes **effective** the revision it is written;
    * it stops being effective at the revision of its *first* direct
      correction (later corrections of the same event change nothing — the
      shared ancestor of a fork is superseded once, and every branch tip
      stays effective);
    * a correction that re-attributes an event to another participant moves
      the points in one step: the old owner is debited and the new owner
      credited at the correction's own revision;
    * the effective leaves of every correction chain are therefore the
      events with no in-snapshot direct corrector;
    * participants with at least one effective event are the snapshot
      members (an event superseded out of a participant removes its member
      slot, matching the ``NOT EXISTS`` aggregate: a participant whose
      events are all corrected away disappears, including from rank gaps).

    Revisions advance monotonically; totals may be read at the current
    revision at any time. ``revision == 0`` is the empty timeline.
    """

    def __init__(self, events: Iterable[Mapping] = ()) -> None:
        self._events_by_revision: dict[int, list[_ReplayEvent]] = {}
        for r in events:
            ev = _ReplayEvent(
                event_id=r["event_id"],
                participant_id=r["participant_id"],
                points=r["points"],
                correction_of=r["correction_of"],
                revision=r["revision"],
            )
            self._events_by_revision.setdefault(ev.revision, []).append(ev)

        self.revision = 0
        self._totals: dict[str, int] = {}
        self._effective_count: dict[str, int] = {}
        # event_id -> event for every ingested event (chain walks)
        self._by_id: dict[str, _ReplayEvent] = {}
        # event_id -> (owner participant_id, points) for every ingested event
        self._owner_by_id: dict[str, Tuple[str, int]] = {}
        # event_id -> direct-corrector event_ids seen so far, insertion order;
        # a key's presence also marks the first time the event was corrected.
        self._children: dict[str, list[str]] = {}
        # revision -> everything that became true at that revision
        self._effects: dict[int, RevisionEffect] = {}

    @classmethod
    def from_connection(cls, conn: sqlite3.Connection, to_revision: int) -> "SnapshotTimeline":
        """Build a timeline over all events with ``revision <= to_revision``.

        This is the single range read shared by the revision-window
        endpoints; the returned engine holds no database connection and
        performs no further queries while replaying.
        """
        rows = _timeline_rows(conn, to_revision)
        return cls(rows)

    def advance(self, revision: int) -> None:
        """Forward the replay to ``revision`` (must be >= current revision)."""
        while self.revision < revision:
            self.revision += 1
            added: list[str] = []
            first_corrected: list[str] = []
            transfers: list[OwnershipTransfer] = []
            for ev in self._events_by_revision.get(self.revision, ()):
                self._ingest(ev, added, first_corrected, transfers)
            self._effects[self.revision] = RevisionEffect(
                revision=self.revision,
                added=tuple(added),
                first_corrected=tuple(first_corrected),
                transfers=tuple(transfers),
            )

    def effect_at(self, revision: int) -> RevisionEffect:
        """What became true at ``revision`` after advancing through it.

        Returning an empty effect for revisions with no events (possible in
        theory if revisions ever gap) keeps callers branch-free.
        """
        return self._effects.get(
            revision, RevisionEffect(revision=revision, added=(), first_corrected=(), transfers=())
        )

    def all_transfers(self) -> list[OwnershipTransfer]:
        """Cross-participant re-attributions up to the current revision.

        Ordered by revision, then by the corrected event_id within a
        revision — the order events are ingested.
        """
        out: list[OwnershipTransfer] = []
        for rev in range(1, self.revision + 1):
            out.extend(self.effect_at(rev).transfers)
        return out

    def total_for(self, participant_id: str) -> int:
        """Effective total of one participant at the current revision (0 if absent)."""
        return self._totals.get(participant_id, 0)

    def rank_positions(self) -> dict[str, "RankPosition"]:
        """Full snapshot ``{participant_id: (rank, total_points)}``.

        Ordering matches ``/leaderboard``: total_points descending,
        participant_id ascending; ranks start at 1 and stay contiguous.
        Participants without an effective event are absent.
        """
        ordered = sorted(
            (pid for pid in self._effective_count if self._effective_count[pid] > 0),
            key=lambda pid: (-self._totals[pid], pid),
        )
        return {
            pid: RankPosition(rank=i + 1, total_points=self._totals[pid])
            for i, pid in enumerate(ordered)
        }

    def effective_leaves(self) -> Sequence[Tuple[_ReplayEvent, str]]:
        """Effective chain leaves at the current revision.

        Each item is ``(event, chain_root)`` — the leaf event itself and the
        uncorrected ancestor reached by walking ``correction_of`` links.
        Forked chains yield one leaf per branch; a plain linear chain yields
        its tip. Ordered by ``(revision, event_id)`` for deterministic use
        by auditors.
        """
        leaves: list[Tuple[_ReplayEvent, str]] = []
        for event_id in self._by_id:
            if event_id in self._children:
                continue  # has a direct corrector in the replay window
            leaves.append((self._by_id[event_id], self.chain_root(event_id)))
        leaves.sort(key=lambda item: (item[0].revision, item[0].event_id))
        return leaves

    def chain_root(self, event_id: str) -> str:
        """Chain root of an ingested event (walk ``correction_of`` to the top)."""
        root = event_id
        parent = self._by_id[event_id].correction_of
        while parent is not None:
            root = parent
            parent = self._by_id[root].correction_of
        return root

    def chain_path(self, event_id: str) -> list[str]:
        """Root-to-leaf event_ids for an ingested event (revision ascending)."""
        path = [event_id]
        parent = self._by_id[event_id].correction_of
        while parent is not None:
            path.append(parent)
            parent = self._by_id[parent].correction_of
        path.reverse()
        return path

    def fork_roots(self) -> list[str]:
        """Roots of chains containing a fork (an event with >=2 direct corrections).

        Sorted by chain_root, matching the audit endpoint's ordering.
        """
        return sorted(
            {
                self.chain_root(event_id)
                for event_id, kids in self._children.items()
                if len(kids) >= 2
            }
        )

    # -- internals ---------------------------------------------------------

    def _ingest(
        self,
        ev: _ReplayEvent,
        added: list[str],
        first_corrected: list[str],
        transfers: list[OwnershipTransfer],
    ) -> None:
        added.append(ev.event_id)
        self._by_id[ev.event_id] = ev
        self._owner_by_id[ev.event_id] = (ev.participant_id, ev.points)
        self._totals[ev.participant_id] = (
            self._totals.get(ev.participant_id, 0) + ev.points
        )
        self._effective_count[ev.participant_id] = (
            self._effective_count.get(ev.participant_id, 0) + 1
        )
        target = ev.correction_of
        if target is not None:
            kids = self._children.setdefault(target, [])
            kids.append(ev.event_id)
            if len(kids) == 1:
                # First direct correction of ``target``: it stops being
                # effective at this revision. Further corrections fork off
                # the same ancestor and must not debit it again.
                first_corrected.append(target)
                t_pid, t_points = self._owner_by_id[target]
                self._totals[t_pid] -= t_points
                self._effective_count[t_pid] -= 1
                if t_pid != ev.participant_id:
                    transfers.append(
                        OwnershipTransfer(
                            revision=ev.revision,
                            event_id=target,
                            from_participant=t_pid,
                            to_participant=ev.participant_id,
                            points=t_points,
                        )
                    )


def _timeline_rows(conn: sqlite3.Connection, to_revision: int) -> list[sqlite3.Row]:
    """The single range read for replay endpoints: events up to ``to_revision``."""
    return conn.execute(
        "SELECT event_id, participant_id, points, correction_of, revision"
        " FROM events WHERE revision <= ? ORDER BY revision ASC",
        (to_revision,),
    ).fetchall()


def leaderboard(revision: Optional[int] = None) -> LeaderboardOut:
    """Leaderboard as of ``revision`` (default: latest).

    An event is *effective* at revision R iff it exists at R (revision <= R)
    and no event with revision <= R corrects it. Hence, for every correction
    chain, only the chain tip that existed at R is counted.
    """
    with read_tx() as conn:
        revision = _resolve_revision(conn, revision)
        rows = _effective_rows(conn, revision)

    ordered, details = _aggregate(rows)
    return _leaderboard_out(revision, ordered, details)


def _resolve_snapshot_revision(
    conn: sqlite3.Connection, revision: Optional[int]
) -> int:
    """Resolve a snapshot revision that also accepts 0 (empty leaderboard).

    Mirrors :func:`_resolve_revision` for revisions >= 1; ``None`` defaults
    to the current maximum (0 on the empty ledger). A revision beyond the
    current maximum is 404; negatives are rejected at the API layer (422).
    """
    max_rev = conn.execute(
        "SELECT COALESCE(MAX(revision), 0) FROM events"
    ).fetchone()[0]
    if revision is None:
        return max_rev
    if revision < 0 or revision > max_rev:
        raise HTTPException(status_code=404, detail=f"revision {revision} not found")
    return revision


def _snapshot_entries(
    conn: sqlite3.Connection, revision: int
) -> list[LeaderboardEntry]:
    """Full ranked leaderboard entries as of ``revision`` (0 = empty)."""
    if revision == 0:
        return []
    ordered, details = _aggregate(_effective_rows(conn, revision))
    return [
        LeaderboardEntry(
            rank=i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(ordered)
    ]


def _commitment_leaves(entries: list[LeaderboardEntry]) -> list[bytes]:
    """Deterministic leaf digests in leaderboard (rank) order.

    Effective event_ids preserve ``_effective_rows`` order: per participant,
    revision ascending.
    """
    return [
        commitment.leaf_digest(
            rank=entry.rank,
            participant_id=entry.participant_id,
            total_points=entry.total_points,
            event_ids=[ev.event_id for ev in entry.effective_events],
        )
        for entry in entries
    ]


def leaderboard_commitment(revision: Optional[int]) -> LeaderboardCommitmentOut:
    """SHA-256 Merkle commitment over the leaderboard at ``revision``.

    Leaves follow the same order as ``GET /leaderboard`` (total_points
    descending, participant_id ascending) and encode each entry's rank,
    participant_id, total_points and effective event_ids, so the root pins
    the whole snapshot. The empty leaderboard (revision 0, or the default on
    an empty ledger) has the fixed root ``SHA-256(b"")``. Read-only; the
    result for a past revision never changes under later writes because all
    input rows are pinned by ``revision <= R``.
    """
    with read_tx() as conn:
        revision = _resolve_snapshot_revision(conn, revision)
        entries = _snapshot_entries(conn, revision)

    if not entries:
        return LeaderboardCommitmentOut(
            revision=revision, leaf_count=0, root_hash=commitment.EMPTY_ROOT_HEX
        )
    levels = commitment.build_levels(_commitment_leaves(entries))
    root = commitment.root_from_levels(levels)
    return LeaderboardCommitmentOut(
        revision=revision,
        leaf_count=len(entries),
        root_hash=root.hex(),
    )


def leaderboard_proof(
    participant_id: str, revision: Optional[int]
) -> LeaderboardProofOut:
    """Merkle inclusion proof for one participant's entry at ``revision``.

    Rebuilds exactly the same tree as :func:`leaderboard_commitment` for the
    same revision, so the returned proof recomputes to that endpoint's
    ``root_hash``. ``leaf_index`` is the zero-based rank (``rank - 1``);
    the proof lists sibling nodes from the leaf layer upward, including the
    duplicated-self pairing at every odd layer. A participant absent from
    the snapshot (or the empty leaderboard) is 404.
    """
    with read_tx() as conn:
        revision = _resolve_snapshot_revision(conn, revision)
        entries = _snapshot_entries(conn, revision)

    leaf_index = next(
        (i for i, e in enumerate(entries) if e.participant_id == participant_id),
        None,
    )
    if leaf_index is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"participant '{participant_id}' has no leaderboard entry "
                f"at revision {revision}"
            ),
        )

    levels = commitment.build_levels(_commitment_leaves(entries))
    steps = commitment.merkle_proof(levels, leaf_index)
    root = commitment.root_from_levels(levels)
    return LeaderboardProofOut(
        revision=revision,
        leaf_index=leaf_index,
        entry=entries[leaf_index],
        proof=[
            MerkleProofStepOut(
                level=step.level,
                position=step.position,
                sibling=step.sibling.hex(),
            )
            for step in steps
        ],
        root_hash=root.hex(),
        leaf_count=len(entries),
    )


def _snapshot(conn: sqlite3.Connection, revision: int) -> dict[str, RankPosition]:
    """``{participant_id: (rank, total_points)}`` as of ``revision``.

    ``revision == 0`` denotes the empty leaderboard. The effective-event
    rule is the same one used by ``/leaderboard``: at revision R an event
    counts iff it exists at R and no correction existing at R supersedes
    it, so a correction that moves an event from participant A to B makes
    A disappear and B appear in the same snapshot.
    """
    if revision == 0:
        return {}
    ordered, _details = _aggregate(_effective_rows(conn, revision))
    return _rank_positions(ordered)


def leaderboard_changes(
    from_revision: int, to_revision: int
) -> LeaderboardChangesOut:
    """Participants whose total points or rank differ between two revisions.

    Validation order: ordering errors (negative, ``from >= to``) are 422;
    revisions beyond the current maximum are 404. ``from_revision == 0``
    compares against the empty leaderboard.

    Both snapshots come from one :class:`SnapshotTimeline` replay built over
    a single ``revision <= to_revision`` range read: the engine is advanced
    once to ``from_revision`` for the before snapshot and on to
    ``to_revision`` for the after snapshot, never re-querying the database.
    """
    if from_revision < 0 or from_revision >= to_revision:
        raise HTTPException(
            status_code=422,
            detail=(
                "require 0 <= from_revision < to_revision, got "
                f"from_revision={from_revision}, to_revision={to_revision}"
            ),
        )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if to_revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {to_revision} not found"
            )
        timeline = SnapshotTimeline.from_connection(conn, to_revision)

    timeline.advance(from_revision)
    before_map = timeline.rank_positions()
    timeline.advance(to_revision)
    after_map = timeline.rank_positions()

    return LeaderboardChangesOut(
        from_revision=from_revision,
        to_revision=to_revision,
        changes=_diff_snapshots(before_map, after_map),
    )


def _event_snapshot(
    conn: sqlite3.Connection, revision: int
) -> dict[str, dict[str, EventOut]]:
    """Effective events at ``revision``, grouped as ``{chain_root: {event_id: event}}``.

    The effective rule matches ``/leaderboard``: at revision R an event counts
    iff it exists at R and no correction existing at R supersedes it. The chain
    root is found by walking ``correction_of`` links up to the uncorrected
    ancestor; targets always exist and carry smaller revisions, so the walk
    stays inside the snapshot and terminates.
    """
    if revision == 0:
        return {}
    rows = conn.execute(
        "SELECT * FROM events WHERE revision <= ? ORDER BY revision ASC",
        (revision,),
    ).fetchall()
    parents = {r["event_id"]: r["correction_of"] for r in rows}
    superseded = {p for p in parents.values() if p is not None}

    def root_of(event_id: str) -> str:
        while parents[event_id] is not None:
            event_id = parents[event_id]
        return event_id

    snapshot: dict[str, dict[str, EventOut]] = {}
    for r in rows:
        if r["event_id"] in superseded:
            continue
        snapshot.setdefault(root_of(r["event_id"]), {})[r["event_id"]] = (
            _row_to_event(r)
        )
    return snapshot


def event_changes(from_revision: int, to_revision: int) -> EventChangesOut:
    """Effective-event differences between two revisions, grouped by chain root.

    Within each correction chain, event_ids effective at both snapshots cancel
    out. A chain left with exactly one outgoing and one incoming event yields a
    single ``replaced`` change; any other remainder is reported as individual
    ``added``/``removed`` changes. Validation order matches
    ``/leaderboard/changes``: ordering errors are 422, revisions beyond the
    current maximum are 404. ``from_revision == 0`` compares against the empty
    ledger.
    """
    if from_revision < 0 or from_revision >= to_revision:
        raise HTTPException(
            status_code=422,
            detail=(
                "require 0 <= from_revision < to_revision, got "
                f"from_revision={from_revision}, to_revision={to_revision}"
            ),
        )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if to_revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {to_revision} not found"
            )
        before_map = _event_snapshot(conn, from_revision)
        after_map = _event_snapshot(conn, to_revision)

    changes: list[EventChangeOut] = []
    for root in set(before_map) | set(after_map):
        before_group = before_map.get(root, {})
        after_group = after_map.get(root, {})
        removed = [ev for eid, ev in before_group.items() if eid not in after_group]
        added = [ev for eid, ev in after_group.items() if eid not in before_group]
        if len(removed) == 1 and len(added) == 1:
            changes.append(
                EventChangeOut(
                    chain_root=root,
                    change_type="replaced",
                    before=removed[0],
                    after=added[0],
                )
            )
        else:
            changes.extend(
                EventChangeOut(
                    chain_root=root, change_type="removed", before=ev, after=None
                )
                for ev in removed
            )
            changes.extend(
                EventChangeOut(
                    chain_root=root, change_type="added", before=None, after=ev
                )
                for ev in added
            )
    changes.sort(
        key=lambda c: (
            c.chain_root,
            (c.after or c.before).revision,
            (c.after or c.before).event_id,
        )
    )
    return EventChangesOut(
        from_revision=from_revision,
        to_revision=to_revision,
        changes=changes,
    )


def correction_forks(revision: Optional[int]) -> CorrectionForksOut:
    """Correction chains that have forked as of ``revision``.

    Restores every correction relationship existing at the snapshot
    (``revision <= R``) and returns only chain roots whose tree contains a
    fork: an event with at least two direct corrections in the snapshot. A
    later chained correction still belongs to the branch it hangs off, so a
    plain linear chain (each node at most one direct correction) is never
    returned.

    For each forked chain the output lists every *effective leaf* — a
    snapshot event with no in-snapshot correction, matching the
    ``/leaderboard`` effective rule — with the complete root-to-leaf
    ``event_id`` path, the leaf's participant_id, points and revision, plus
    the chain's effective points summed per participant_id across its
    leaves. Read-only.

    ``revision`` defaults to the latest; 0 denotes the empty ledger; a
    revision beyond the current maximum is 404.

    The snapshot is restored by the shared :class:`SnapshotTimeline` engine
    (one ``revision <= R`` range read): its fork roots, effective leaves and
    root-to-leaf paths are exactly the relations the audit reports, so the
    fork view and the leaderboard effective rule can never drift apart.
    """
    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if revision is None:
            revision = max_rev
        elif revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {revision} not found"
            )
        if revision == 0:
            return CorrectionForksOut(revision=0, forks=[])
        timeline = SnapshotTimeline.from_connection(conn, revision)

    timeline.advance(revision)
    forked_roots = timeline.fork_roots()
    leaves_by_root: dict[str, list[tuple[_ReplayEvent, list[str]]]] = {}
    for leaf, root in timeline.effective_leaves():
        if root in forked_roots:
            leaves_by_root.setdefault(root, []).append(
                (leaf, timeline.chain_path(leaf.event_id))
            )

    forks: list[CorrectionForkOut] = []
    for root in forked_roots:
        leaves: list[CorrectionForkLeafOut] = []
        totals: dict[str, int] = {}
        for leaf, path_ids in leaves_by_root.get(root, []):
            leaves.append(
                CorrectionForkLeafOut(
                    path=path_ids,
                    participant_id=leaf.participant_id,
                    points=leaf.points,
                    revision=leaf.revision,
                )
            )
            totals[leaf.participant_id] = (
                totals.get(leaf.participant_id, 0) + leaf.points
            )
        leaves.sort(key=lambda leaf: (leaf.revision, leaf.path[-1]))
        participant_points = [
            CorrectionForkParticipantPoints(participant_id=pid, total_points=total)
            for pid, total in sorted(totals.items())
        ]
        forks.append(
            CorrectionForkOut(
                chain_root=root,
                leaves=leaves,
                participant_points=participant_points,
            )
        )
    return CorrectionForksOut(revision=revision, forks=forks)


def leaderboard_page(
    revision: Optional[int], limit: int, cursor: Optional[str]
) -> LeaderboardPageOut:
    """One page of the leaderboard, resumable via a signed cursor.

    The first request pins ``revision`` (default: latest). The returned
    ``next_cursor`` binds that revision and the last sort key
    ``(total_points, participant_id)``; follow-up requests continue from
    the cursor, so concurrent writers cannot shift or tear the snapshot.
    """
    after: Optional[Tuple[int, str]] = None
    if cursor is not None:
        payload = decode_cursor(cursor)
        if revision is not None and revision != payload.revision:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"explicit revision {revision} conflicts with "
                    f"cursor revision {payload.revision}"
                ),
            )
        revision = payload.revision
        after = (payload.total_points, payload.participant_id)

    with read_tx() as conn:
        revision = _resolve_revision(conn, revision)
        rows = _effective_rows(conn, revision)

    ordered, details = _aggregate(rows)

    start = 0
    if after is not None:
        keys = [(-total, pid) for pid, total in ordered]
        start = bisect_right(keys, (-after[0], after[1]))

    page = ordered[start : start + limit]
    entries = [
        LeaderboardEntry(
            rank=start + i + 1,
            participant_id=pid,
            total_points=total,
            effective_events=details[pid],
        )
        for i, (pid, total) in enumerate(page)
    ]
    has_more = start + limit < len(ordered)
    next_cursor = (
        encode_cursor(revision, page[-1][1], page[-1][0]) if has_more else None
    )
    return LeaderboardPageOut(revision=revision, entries=entries, next_cursor=next_cursor)


def score_breakdown(
    participant_id: str, revision: Optional[int]
) -> ScoreBreakdownOut:
    """Per-chain score attribution for one participant as of ``revision``.

    ``revision`` defaults to the latest; 0 denotes the empty ledger. The
    effective rule matches ``/leaderboard``: at revision R an event counts
    iff it exists at R and no correction existing at R supersedes it. A
    correction chain contributes iff its effective tip at the snapshot
    belongs to ``participant_id`` — a correction that re-attributes points
    to someone else removes the chain from the previous participant. Each
    contribution carries the chain root, the effective tip and the full
    root-to-tip history (revision ascending), which may include events
    attributed to other participants before a re-attributing correction.
    """
    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if revision is None:
            revision = max_rev
        elif revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {revision} not found"
            )
        rows = conn.execute(
            "SELECT * FROM events WHERE revision <= ? ORDER BY revision ASC",
            (revision,),
        ).fetchall()

    by_id = {r["event_id"]: r for r in rows}
    superseded = {
        r["correction_of"] for r in rows if r["correction_of"] is not None
    }

    contributions: list[ScoreContribution] = []
    for r in rows:
        if r["event_id"] in superseded or r["participant_id"] != participant_id:
            continue
        # Walk correction_of links up to the chain root; targets always
        # exist with smaller revisions, so the walk stays in the snapshot.
        path = [r]
        while path[-1]["correction_of"] is not None:
            path.append(by_id[path[-1]["correction_of"]])
        path.reverse()
        contributions.append(
            ScoreContribution(
                chain_root=path[0]["event_id"],
                effective_event=_row_to_event(r),
                history=[_row_to_event(x) for x in path],
            )
        )
    contributions.sort(
        key=lambda c: (
            c.chain_root,
            c.effective_event.revision,
            c.effective_event.event_id,
        )
    )
    total = sum(c.effective_event.points for c in contributions)
    return ScoreBreakdownOut(
        revision=revision, total_points=total, contributions=contributions
    )


def participant_compare(
    left_id: str, right_id: str, from_revision: int, to_revision: int
) -> ParticipantCompareOut:
    """Per-revision score race between two participants, merged into intervals.

    For every revision R in ``[from_revision, to_revision]`` both sides'
    effective totals are computed under the same rule as ``/leaderboard``:
    an event counts iff it exists at R and no correction existing at R
    supersedes it. Rather than re-running the snapshot query per revision,
    events are replayed once in revision order: an event becomes effective
    the moment it is written (corrections always carry larger revisions) and
    stops being effective at its *first* direct correction — so a correction
    that re-attributes an event from participant A to B subtracts from A and
    adds to B at the correction's own revision. Consecutive snapshots with
    identical totals on both sides merge into one closed interval.

    Validation order matches ``/leaderboard/changes``: ordering errors are
    422, a ``to_revision`` beyond the current maximum is 404.
    ``from_revision == 0`` denotes the empty ledger; unknown participants
    simply stay at 0, so two scoreless sides yield a single ``tie``
    interval covering the whole range.

    The per-revision totals come from one :class:`SnapshotTimeline` replay
    built over a single ``revision <= to_revision`` range read; the engine
    advances one revision at a time and both totals are read off the same
    engine state, so the two sides always reflect one consistent snapshot.
    """
    if from_revision < 0 or from_revision > to_revision:
        raise HTTPException(
            status_code=422,
            detail=(
                "require 0 <= from_revision <= to_revision, got "
                f"from_revision={from_revision}, to_revision={to_revision}"
            ),
        )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if to_revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {to_revision} not found"
            )
        timeline = SnapshotTimeline.from_connection(conn, to_revision)

    intervals: list[CompareIntervalOut] = []
    for rev in range(0, to_revision + 1):
        timeline.advance(rev)
        left_total = timeline.total_for(left_id)
        right_total = timeline.total_for(right_id)
        if rev < from_revision:
            continue
        if (
            intervals
            and intervals[-1].left_points == left_total
            and intervals[-1].right_points == right_total
        ):
            intervals[-1].end_revision = rev
            continue
        if left_total > right_total:
            leader = "left"
        elif right_total > left_total:
            leader = "right"
        else:
            leader = "tie"
        intervals.append(
            CompareIntervalOut(
                start_revision=rev,
                end_revision=rev,
                left_points=left_total,
                right_points=right_total,
                leader=leader,
            )
        )
    return ParticipantCompareOut(
        left_id=left_id,
        right_id=right_id,
        from_revision=from_revision,
        to_revision=to_revision,
        intervals=intervals,
    )


def threshold_intervals(
    participant_id: str,
    threshold: int,
    from_revision: int,
    to_revision: int,
) -> ThresholdIntervalsOut:
    """Consecutive-revision intervals where one participant meets a threshold.

    The participant's effective total is recomputed for *every* revision in
    ``[from_revision, to_revision]`` under the same rule as ``/leaderboard``:
    an event counts iff it exists at R and no correction existing at R
    supersedes it. Events are replayed once in revision order (same machinery
    as ``/participants/compare``): an event becomes effective the moment it is
    written and stops being effective at its *first* direct correction, so a
    correction that re-attributes an event from participant A to B subtracts
    from A and adds to B at the correction's own revision; a forked chain
    keeps every branch tip effective and supersedes the shared ancestor only
    once. Hence rises and falls caused by correction-chain forks and by
    cross-participant corrections are all reflected.

    Consecutive revisions whose recomputed total is ``>= threshold`` (border
    inclusive) merge into one closed interval; endpoints carry the totals at
    both ends, which may differ as long as they stay at/above the threshold.

    Validation order matches ``/participants/compare``: ordering errors
    (negative, ``from_revision > to_revision``) are 422, a ``to_revision``
    beyond the current maximum is 404. ``from_revision == 0`` denotes the
    empty ledger. A participant that never appears is treated as 0 points at
    every revision. Read-only.

    The per-revision totals come from one :class:`SnapshotTimeline` replay
    built over a single ``revision <= to_revision`` range read — the same
    engine ``/participants/compare`` and ``/leaderboard/changes`` use — so
    the threshold decision at every revision is exactly that revision's
    leaderboard aggregate.
    """
    if from_revision < 0 or from_revision > to_revision:
        raise HTTPException(
            status_code=422,
            detail=(
                "require 0 <= from_revision <= to_revision, got "
                f"from_revision={from_revision}, to_revision={to_revision}"
            ),
        )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if to_revision > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {to_revision} not found"
            )
        timeline = SnapshotTimeline.from_connection(conn, to_revision)

    intervals: list[ThresholdIntervalOut] = []
    for rev in range(0, to_revision + 1):
        timeline.advance(rev)
        total = timeline.total_for(participant_id)
        if rev < from_revision:
            continue
        if total >= threshold:
            if intervals and intervals[-1].end_revision == rev - 1:
                intervals[-1].end_revision = rev
                intervals[-1].end_points = total
            else:
                intervals.append(
                    ThresholdIntervalOut(
                        start_revision=rev,
                        end_revision=rev,
                        start_points=total,
                        end_points=total,
                    )
                )
    return ThresholdIntervalsOut(
        participant_id=participant_id,
        threshold=threshold,
        from_revision=from_revision,
        to_revision=to_revision,
        intervals=intervals,
    )


def rewards_preview(preview: RewardPreviewIn) -> RewardPreviewOut:
    """Dry-run prize settlement for the leaderboard at ``preview.revision``.

    Read-only: the preview never writes to the ledger. Participants sharing a
    total form one tie group covering a contiguous rank range
    ``[rank_start, rank_end]``. Ranks ``1..len(prize_cents)`` form the prize
    zone. If any rank covered by a group falls inside the zone, the prizes of
    the covered in-zone ranks are pooled and split equally across the whole
    group; the leftover cents go one each to the lowest ``participant_id``s,
    so the paid-out total always equals the pooled amount. Groups fully
    outside the zone produce no winners.
    """
    with read_tx() as conn:
        revision = _resolve_revision(conn, preview.revision)
        rows = _effective_rows(conn, revision)

    ordered, _details = _aggregate(rows)

    prizes = preview.prize_cents
    winners: list[RewardWinnerOut] = []
    i = 0
    while i < len(ordered):
        total = ordered[i][1]
        j = i
        while j < len(ordered) and ordered[j][1] == total:
            j += 1
        # Within a tie group `ordered` is participant_id-ascending already.
        group = [pid for pid, _ in ordered[i:j]]
        rank_start = i + 1
        rank_end = j  # 1-based inclusive
        covered = prizes[rank_start - 1 : rank_end]
        if covered:
            pool = sum(covered)
            share, remainder = divmod(pool, len(group))
            for k, pid in enumerate(group):
                winners.append(
                    RewardWinnerOut(
                        participant_id=pid,
                        rank_start=rank_start,
                        rank_end=rank_end,
                        total_points=total,
                        prize_cents=share + (1 if k < remainder else 0),
                    )
                )
        i = j
    return RewardPreviewOut(revision=revision, winners=winners)


def events_preview(preview: EventPreviewIn) -> EventPreviewOut:
    """Read-only what-if projection of draft events on top of a base revision.

    The baseline is the ledger as of ``base_revision`` (0 denotes the empty
    ledger); later ledger events are ignored by the projection. The drafts
    then take effect in array order, exactly as if they were appended at the
    virtual revisions ``base_revision + 1 .. base_revision + len(drafts)``:
    a draft correcting a baseline event or an earlier draft supersedes its
    target, and a forked chain keeps every effective leaf — the same
    effective rule, (-total_points, participant_id) ordering and sequential
    ranking as ``/leaderboard``.

    ``correction_of`` may reference an event visible in the baseline or an
    earlier draft of the same batch; anything else (unknown id, later draft,
    self-reference, or a ledger event beyond ``base_revision``) is a 422. A
    draft ``event_id`` already stored *anywhere* in the ledger is a 409 —
    previews have no idempotent replay. A ``base_revision`` beyond the
    current maximum is a 404. Pure dry run: nothing is written and no
    revision is consumed.
    """
    drafts = preview.events
    base = preview.base_revision

    # Cheap self-checks; the schema normally rejects these before the call.
    if not drafts or len(drafts) > 100:
        raise HTTPException(status_code=422, detail="events must contain 1–100 drafts")
    seen_ids: set[str] = set()
    for ev in drafts:
        if ev.event_id in seen_ids:
            raise HTTPException(
                status_code=422,
                detail=f"event_id '{ev.event_id}' appears more than once in the batch",
            )
        seen_ids.add(ev.event_id)
        if ev.correction_of is not None and ev.correction_of == ev.event_id:
            raise HTTPException(
                status_code=422, detail="correction_of must not equal event_id"
            )

    with read_tx() as conn:
        max_rev = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) FROM events"
        ).fetchone()[0]
        if base > max_rev:
            raise HTTPException(
                status_code=404, detail=f"revision {base} not found"
            )

        # Conflict check first (mirrors /events/batch): a draft id stored
        # anywhere in the ledger — at any revision — clashes, regardless of
        # the payload.
        clash = conn.execute(
            "SELECT event_id FROM events WHERE event_id IN (%s) LIMIT 1"
            % ",".join("?" for _ in drafts),
            [ev.event_id for ev in drafts],
        ).fetchone()
        if clash is not None:
            raise HTTPException(
                status_code=409,
                detail=f"event_id '{clash['event_id']}' already exists in the ledger",
            )

        baseline_rows = conn.execute(
            "SELECT event_id, participant_id, points, correction_of, revision"
            " FROM events WHERE revision <= ? ORDER BY revision ASC",
            (base,),
        ).fetchall()
        before_map = _snapshot(conn, base)

    # Every correction_of must resolve against the baseline or an earlier
    # draft; referencing a later draft (or an id unknown to both) is a 422.
    baseline_ids = {r["event_id"] for r in baseline_rows}
    index_by_id = {ev.event_id: i for i, ev in enumerate(drafts)}
    for i, ev in enumerate(drafts):
        if ev.correction_of is None:
            continue
        target_idx = index_by_id.get(ev.correction_of)
        if target_idx is not None:
            if target_idx >= i:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"event '{ev.event_id}' has correction_of "
                        f"'{ev.correction_of}' referencing a later event "
                        "in the same batch; only earlier events may be "
                        "referenced"
                    ),
                )
        elif ev.correction_of not in baseline_ids:
            raise HTTPException(
                status_code=422,
                detail=(
                    "correction_of references unknown event "
                    f"'{ev.correction_of}'"
                ),
            )

    # Project: baseline events plus the drafts at virtual revisions, then
    # apply the ledger's effective rule — an event counts iff nothing in the
    # combined set corrects it.
    combined: list[dict] = [
        {
            "event_id": r["event_id"],
            "participant_id": r["participant_id"],
            "points": r["points"],
            "correction_of": r["correction_of"],
            "revision": r["revision"],
        }
        for r in baseline_rows
    ]
    for i, ev in enumerate(drafts):
        combined.append(
            {
                "event_id": ev.event_id,
                "participant_id": ev.participant_id,
                "points": ev.points,
                "correction_of": ev.correction_of,
                "revision": base + i + 1,
            }
        )
    superseded = {
        e["correction_of"] for e in combined if e["correction_of"] is not None
    }
    effective = [e for e in combined if e["event_id"] not in superseded]
    effective.sort(key=lambda e: (e["participant_id"], e["revision"]))

    ordered, details = _aggregate(effective)
    projected = _leaderboard_out(base + len(drafts), ordered, details)
    after_map = _rank_positions(ordered)

    return EventPreviewOut(
        base_revision=base,
        projected_leaderboard=projected,
        changes=_diff_snapshots(before_map, after_map),
    )
