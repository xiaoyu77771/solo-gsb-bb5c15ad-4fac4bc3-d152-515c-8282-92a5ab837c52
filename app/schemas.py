from __future__ import annotations

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, Field, RootModel, model_validator

# SQLite INTEGER storage range (signed 64-bit). Pydantic's ``int`` in lax
# mode coerces bools, whole-number floats and numeric strings, and accepts
# arbitrary precision; values beyond this range then raise OverflowError at
# INSERT time (HTTP 500). A strict, bounded field rejects all of them with 422.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

#: Strict signed 64-bit integer input: only JSON integers in range are
#: accepted; ``true``/``false``, floats, numeric strings and out-of-range
#: values fail validation.
StrictInt64 = Annotated[int, Field(strict=True, ge=INT64_MIN, le=INT64_MAX)]

#: Strict integer input of any size: only JSON integers are accepted;
#: ``true``/``false``, floats (whole-number or not) and numeric strings
#: fail validation. Used where no SQLite INSERT bounds the value, so
#: out-of-range magnitudes keep their domain-level answer (e.g. 404).
StrictInt = Annotated[int, Field(strict=True)]


class EventIn(BaseModel):
    """Write model for a points event (immutable once accepted)."""

    event_id: str = Field(min_length=1, max_length=128)
    participant_id: str = Field(min_length=1, max_length=128)
    points: StrictInt64
    correction_of: Optional[str] = Field(
        default=None,
        description="event_id of an existing event this event corrects; "
        "corrections may themselves be corrected (chain).",
    )


class EventBatchIn(RootModel[List[EventIn]]):
    """Write model for ``POST /events/batch``: 1–100 events, unique event_ids."""

    root: List[EventIn] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "EventBatchIn":
        seen: set[str] = set()
        duplicates: list[str] = []
        for ev in self.root:
            if ev.event_id in seen and ev.event_id not in duplicates:
                duplicates.append(ev.event_id)
            seen.add(ev.event_id)
        if duplicates:
            raise ValueError(
                "event_id values must be unique within the batch; "
                f"duplicates: {duplicates}"
            )
        return self


class EventPreviewIn(BaseModel):
    """Request body for ``POST /events/preview`` (read-only scenario preview)."""

    base_revision: int = Field(
        ge=0,
        description="Baseline revision the drafts are applied on top of; "
        "0 means the empty ledger.",
    )
    events: List[EventIn] = Field(
        min_length=1,
        max_length=100,
        description="Draft events applied in array order on top of the "
        "baseline; fields as in POST /events.",
    )

    @model_validator(mode="after")
    def _unique_event_ids(self) -> "EventPreviewIn":
        seen: set[str] = set()
        duplicates: list[str] = []
        for ev in self.events:
            if ev.event_id in seen and ev.event_id not in duplicates:
                duplicates.append(ev.event_id)
            seen.add(ev.event_id)
        if duplicates:
            raise ValueError(
                "event_id values must be unique within the batch; "
                f"duplicates: {duplicates}"
            )
        return self


class EventOut(BaseModel):
    event_id: str
    participant_id: str
    points: int
    correction_of: Optional[str]
    revision: int
    created_at: str


class EffectiveEvent(BaseModel):
    event_id: str
    points: int
    revision: int
    correction_of: Optional[str]


class LeaderboardEntry(BaseModel):
    rank: int
    participant_id: str
    total_points: int
    effective_events: List[EffectiveEvent]


class LeaderboardOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]


class LeaderboardPageOut(BaseModel):
    revision: int
    entries: List[LeaderboardEntry]
    next_cursor: Optional[str] = Field(
        default=None,
        description="Signed cursor for the next page; null on the last page.",
    )


class MerkleProofStepOut(BaseModel):
    """One sibling node on a leaf-to-root Merkle path, bottom-up."""

    level: int = Field(
        description="Layer of the sibling node; 0 is the leaf layer."
    )
    position: Literal["left", "right", "self"] = Field(
        description=(
            "Where the sibling sits relative to the running hash: left means "
            "parent = SHA256(0x01 || sibling || running), right means "
            "parent = SHA256(0x01 || running || sibling); self marks the "
            "duplicated last node of an odd layer (running pairs with itself)."
        )
    )
    sibling: str = Field(description="Sibling node digest as lowercase hex.")


class LeaderboardCommitmentOut(BaseModel):
    """Leaf count and Merkle root committing one leaderboard snapshot."""

    revision: int = Field(
        description="Snapshot revision; 0 means the empty leaderboard."
    )
    leaf_count: int = Field(
        description="Number of committed leaves (participants); 0 for the empty leaderboard."
    )
    root_hash: str = Field(
        description=(
            "Merkle root digest as lowercase hex. The empty leaderboard root "
            "is fixed at SHA-256 of the empty byte string."
        )
    )


class LeaderboardProofOut(BaseModel):
    """Inclusion proof for one entry within a leaderboard commitment."""

    revision: int = Field(description="Snapshot revision the proof was built on.")
    leaf_index: int = Field(
        ge=0,
        description="Zero-based leaf index, equal to entry.rank - 1.",
    )
    entry: LeaderboardEntry = Field(
        description="The committed leaderboard entry (rank, participant_id, total_points, effective_events)."
    )
    proof: List[MerkleProofStepOut] = Field(
        description="Sibling nodes from the leaf layer up, in hash order; empty for a single-leaf tree."
    )
    root_hash: str = Field(
        description="Root digest the proof recomputes to (same value as GET /leaderboard/commitment)."
    )
    leaf_count: int = Field(description="Number of leaves in the committed tree.")


class RankPosition(BaseModel):
    """A participant's rank and total at one snapshot revision."""

    rank: int
    total_points: int


class LeaderboardChangeOut(BaseModel):
    participant_id: str
    before: Optional[RankPosition] = Field(
        default=None, description="Null when the participant is absent at from_revision."
    )
    after: Optional[RankPosition] = Field(
        default=None, description="Null when the participant is absent at to_revision."
    )
    points_delta: int = Field(
        description="after.total_points - before.total_points; an absent side counts as 0."
    )


class LeaderboardChangesOut(BaseModel):
    from_revision: int
    to_revision: int
    changes: List[LeaderboardChangeOut]


class EventChangeOut(BaseModel):
    """One effective-event difference between two revisions, per chain root."""

    chain_root: str = Field(
        description="event_id of the correction-chain root the change belongs to."
    )
    change_type: Literal["added", "removed", "replaced"] = Field(
        description=(
            "added: effective only at to_revision; removed: effective only at "
            "from_revision; replaced: the chain's single outgoing event was "
            "superseded by a single incoming one."
        )
    )
    before: Optional[EventOut] = Field(
        default=None,
        description="Full event effective at from_revision; null for added.",
    )
    after: Optional[EventOut] = Field(
        default=None,
        description="Full event effective at to_revision; null for removed.",
    )


class EventChangesOut(BaseModel):
    from_revision: int
    to_revision: int
    changes: List[EventChangeOut]


class CorrectionForkLeafOut(BaseModel):
    """One effective leaf of a forked correction chain."""

    path: List[str] = Field(
        description=(
            "Complete event_id path from the chain root to this effective "
            "leaf, ordered by revision ascending."
        )
    )
    participant_id: str
    points: int
    revision: int


class CorrectionForkParticipantPoints(BaseModel):
    """Effective points of one forked chain summed for one participant."""

    participant_id: str
    total_points: int = Field(
        description="Sum of the chain's effective-leaf points held by this participant."
    )


class CorrectionForkOut(BaseModel):
    """A correction chain whose tree contains a fork at the snapshot revision."""

    chain_root: str = Field(
        description="event_id of the correction-chain root; the root event itself carries no correction_of."
    )
    leaves: List[CorrectionForkLeafOut] = Field(
        description=(
            "Effective leaves of every branch, sorted by leaf revision then event_id."
        )
    )
    participant_points: List[CorrectionForkParticipantPoints] = Field(
        description="Effective points of this chain summed per participant_id, ordered by participant_id."
    )


class CorrectionForksOut(BaseModel):
    revision: int
    forks: List[CorrectionForkOut] = Field(
        description="Forked correction chains, sorted by chain_root."
    )


class ScoreContribution(BaseModel):
    """One correction chain's contribution to a participant's total."""

    chain_root: str = Field(
        description="event_id of the correction-chain root."
    )
    effective_event: EventOut = Field(
        description="The chain tip effective at the snapshot revision."
    )
    history: List[EventOut] = Field(
        description=(
            "Full chain from the root to the effective event, ordered by "
            "revision ascending; may include events attributed to other "
            "participants before a re-attributing correction."
        )
    )


class ScoreBreakdownOut(BaseModel):
    revision: int
    total_points: int
    contributions: List[ScoreContribution]


class CompareIntervalOut(BaseModel):
    """One closed revision interval where both sides' totals stay constant."""

    start_revision: int
    end_revision: int
    left_points: int = Field(description="Effective total of ``left_id`` on this interval.")
    right_points: int = Field(description="Effective total of ``right_id`` on this interval.")
    leader: Literal["left", "right", "tie"] = Field(
        description=(
            "left: left_points > right_points; right: right_points > left_points; "
            "tie: equal totals (including both zero)."
        )
    )


class ParticipantCompareOut(BaseModel):
    left_id: str
    right_id: str
    from_revision: int
    to_revision: int
    intervals: List[CompareIntervalOut]


class ThresholdIntervalOut(BaseModel):
    """One closed, maximal run of consecutive revisions at/above a threshold."""

    start_revision: int
    end_revision: int
    start_points: int = Field(
        description="The participant's effective total at start_revision."
    )
    end_points: int = Field(
        description="The participant's effective total at end_revision."
    )


class ThresholdIntervalsOut(BaseModel):
    participant_id: str
    threshold: int
    from_revision: int
    to_revision: int
    intervals: List[ThresholdIntervalOut] = Field(
        description="Closed revision intervals where total_points >= threshold, "
        "sorted by start_revision ascending."
    )


class RewardPreviewIn(BaseModel):
    """Request body for ``POST /rewards/preview`` (read-only dry run)."""

    revision: Optional[StrictInt] = Field(
        default=None,
        description="Snapshot revision; defaults to the latest. Only a JSON "
        "integer or null is accepted — booleans, numeric strings and floats "
        "(whole-number or not) fail validation.",
    )
    prize_cents: List[Annotated[StrictInt, Field(ge=0)]] = Field(
        min_length=1,
        max_length=100,
        description="Prize per rank in cents, ordered by rank (1st place first); "
        "ranks 1..len(prize_cents) form the prize zone. Every item must be a "
        "non-negative JSON integer — booleans, numeric strings and floats "
        "(whole-number or not) fail validation.",
    )


class RewardWinnerOut(BaseModel):
    participant_id: str
    rank_start: int = Field(
        description="First rank covered by the winner's tie group."
    )
    rank_end: int = Field(
        description="Last rank covered by the winner's tie group."
    )
    total_points: int
    prize_cents: int


class RewardPreviewOut(BaseModel):
    revision: int
    winners: List[RewardWinnerOut]


class EventPreviewOut(BaseModel):
    """Response of ``POST /events/preview``: projection plus baseline diff."""

    base_revision: int = Field(
        description="Baseline revision the projection was built on; "
        "0 means the empty ledger."
    )
    projected_leaderboard: LeaderboardOut = Field(
        description=(
            "Leaderboard as if the drafts were appended after base_revision. "
            "Its revision is virtual (base_revision + number of drafts): no "
            "real revision is consumed. Draft events carry their virtual "
            "revisions in effective_events."
        )
    )
    changes: List[LeaderboardChangeOut] = Field(
        description=(
            "Rank/points diffs between the baseline snapshot and the "
            "projection; same item shape and participant_id ordering as "
            "GET /leaderboard/changes."
        )
    )
