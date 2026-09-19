from __future__ import annotations

from contextlib import asynccontextmanager
from typing import List, Optional

from fastapi import APIRouter, FastAPI, Path, Query, Response, status

from . import ledger
from .db import init_db
from .routing import EncodedPathRoute
from .schemas import (
    CorrectionForksOut,
    EventBatchIn,
    EventChangesOut,
    EventIn,
    EventOut,
    EventPreviewIn,
    EventPreviewOut,
    LeaderboardChangesOut,
    LeaderboardCommitmentOut,
    LeaderboardOut,
    LeaderboardPageOut,
    LeaderboardProofOut,
    ParticipantCompareOut,
    RewardPreviewIn,
    RewardPreviewOut,
    ScoreBreakdownOut,
    ThresholdIntervalsOut,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="League Points Ledger",
    version="1.0.0",
    description=(
        "Immutable points-event ledger with idempotent writes, "
        "correction chains and revisioned leaderboards."
    ),
    lifespan=lifespan,
)


# Routes that carry an opaque ID in the path — plus the static routes those
# IDs could be confused with — match the raw, still percent-encoded request
# path (see app/routing.py). Any writable ID (containing "/", or equal to a
# static route name such as "changes"/"compare") then round-trips through
# URL encoding, while the static routes keep matching literally. Within this
# router, declaration order matters: static literals come before the dynamic
# patterns they share a prefix with.
id_router = APIRouter(route_class=EncodedPathRoute)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post(
    "/events",
    response_model=EventOut,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {"description": "Idempotent replay: event_id already stored with identical payload"},
        409: {"description": "event_id already exists with a different payload"},
        422: {"description": "Invalid correction reference"},
    },
)
def post_event(ev: EventIn, response: Response) -> EventOut:
    event, created = ledger.create_event(ev)
    if not created:
        response.status_code = status.HTTP_200_OK
    return event


@app.post(
    "/events/batch",
    response_model=List[EventOut],
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "description": (
                "Idempotent replay: every event_id already stored with an "
                "identical payload; original results returned in request order"
            )
        },
        409: {
            "description": (
                "Partial replay (some ids exist, some do not) or an existing "
                "event_id carries a different payload; nothing is written"
            )
        },
        422: {
            "description": (
                "Batch size outside 1–100, duplicate event_id within the "
                "batch, self-reference, or a correction_of pointing at an "
                "unknown event or a later event of the same batch"
            )
        },
    },
)
def post_events_batch(batch: EventBatchIn, response: Response) -> List[EventOut]:
    events, created = ledger.create_events(batch)
    if not created:
        response.status_code = status.HTTP_200_OK
    return events


# NOTE: no POST /events/{event_id} route exists, so this literal path is
# never shadowed.
@app.post(
    "/events/preview",
    response_model=EventPreviewOut,
    responses={
        404: {"description": "base_revision beyond the current maximum"},
        409: {"description": "A draft event_id already exists in the ledger"},
        422: {
            "description": (
                "Batch size outside 1–100, duplicate event_id within the "
                "batch, self-reference, or a correction_of pointing at an "
                "unknown event or a later draft of the same batch"
            )
        },
    },
)
def post_events_preview(preview: EventPreviewIn) -> EventPreviewOut:
    return ledger.events_preview(preview)


@app.get("/events", response_model=List[EventOut])
def get_events() -> List[EventOut]:
    return ledger.list_events()


# NOTE: declared before /events/{event_id} so the literal path wins. The
# event literally named "changes" stays readable via URL encoding, e.g.
# GET /events/%63hanges (see app/routing.py).
@id_router.get(
    "/events/changes",
    response_model=EventChangesOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer params, or invalid revision ordering"},
    },
)
def get_event_changes(
    from_revision: int = Query(
        description="Earlier revision; 0 means the empty ledger.",
        ge=0,
    ),
    to_revision: int = Query(
        description="Later revision; must be greater than from_revision.",
        ge=0,
    ),
) -> EventChangesOut:
    return ledger.event_changes(from_revision, to_revision)


@id_router.get(
    "/events/{event_id}",
    response_model=EventOut,
    responses={404: {"description": "event_id not found"}},
)
def get_event(
    event_id: str = Path(
        description=(
            "Opaque event ID. URL-encode reserved characters: '/' as %2F, "
            "'%' as %25, non-ASCII as UTF-8 percent-encoding. An ID equal "
            "to a static route name (e.g. 'changes') is read back by "
            "encoding at least one character, e.g. /events/%63hanges."
        )
    ),
) -> EventOut:
    return ledger.get_event(event_id)


@app.get(
    "/audit/correction-forks",
    response_model=CorrectionForksOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer or negative revision"},
    },
)
def get_correction_forks(
    revision: Optional[int] = Query(
        default=None,
        ge=0,
        description="Snapshot revision; defaults to the latest. 0 means the empty ledger.",
    ),
) -> CorrectionForksOut:
    return ledger.correction_forks(revision)


@app.get("/leaderboard", response_model=LeaderboardOut)
def get_leaderboard(
    revision: Optional[int] = Query(
        default=None, description="Revision to query; defaults to the latest."
    )
) -> LeaderboardOut:
    return ledger.leaderboard(revision)


@app.get(
    "/leaderboard/changes",
    response_model=LeaderboardChangesOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer params, or invalid revision ordering"},
    },
)
def get_leaderboard_changes(
    from_revision: int = Query(
        description="Earlier revision; 0 means the empty leaderboard.",
        ge=0,
    ),
    to_revision: int = Query(
        description="Later revision; must be greater than from_revision.",
        ge=0,
    ),
) -> LeaderboardChangesOut:
    return ledger.leaderboard_changes(from_revision, to_revision)


@app.get(
    "/leaderboard/page",
    response_model=LeaderboardPageOut,
    responses={
        404: {"description": "Revision not found"},
        422: {"description": "Invalid limit, or malformed/tampered/conflicting cursor"},
    },
)
def get_leaderboard_page(
    revision: Optional[int] = Query(
        default=None,
        description="Revision to query; defaults to the latest. When a cursor is "
        "supplied, an explicit revision must match the cursor's revision.",
    ),
    limit: int = Query(default=50, ge=1, le=500, description="Maximum entries per page."),
    cursor: Optional[str] = Query(
        default=None,
        description="Signed cursor from a previous page; pins the snapshot revision.",
    ),
) -> LeaderboardPageOut:
    return ledger.leaderboard_page(revision, limit, cursor)


@app.get(
    "/leaderboard/commitment",
    response_model=LeaderboardCommitmentOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer or negative revision"},
    },
)
def get_leaderboard_commitment(
    revision: Optional[int] = Query(
        default=None,
        ge=0,
        description="Snapshot revision; defaults to the latest. 0 means the empty leaderboard.",
    ),
) -> LeaderboardCommitmentOut:
    return ledger.leaderboard_commitment(revision)


@id_router.get(
    "/leaderboard/proof/{participant_id}",
    response_model=LeaderboardProofOut,
    responses={
        404: {
            "description": (
                "Revision beyond the current maximum, or participant absent "
                "from the leaderboard at that revision"
            )
        },
        422: {"description": "Missing/non-integer or negative revision"},
    },
)
def get_leaderboard_proof(
    participant_id: str = Path(
        description=(
            "Opaque participant ID. URL-encode reserved characters: '/' as "
            "%2F, '%' as %25, non-ASCII as UTF-8 percent-encoding."
        )
    ),
    revision: Optional[int] = Query(
        default=None,
        ge=0,
        description="Snapshot revision; defaults to the latest. 0 means the empty leaderboard.",
    ),
) -> LeaderboardProofOut:
    return ledger.leaderboard_proof(participant_id, revision)


# NOTE: declared before /participants/{participant_id}/... so the literal
# path keeps winning even if a /participants/{participant_id} route appears.
@id_router.get(
    "/participants/compare",
    response_model=ParticipantCompareOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Missing/non-integer params, or invalid revision ordering"},
    },
)
def get_participant_compare(
    left_id: str = Query(min_length=1, description="Left-hand participant."),
    right_id: str = Query(min_length=1, description="Right-hand participant."),
    from_revision: int = Query(
        description="First snapshot revision; 0 means the empty ledger.",
        ge=0,
    ),
    to_revision: int = Query(
        description="Last snapshot revision; must be >= from_revision.",
        ge=0,
    ),
) -> ParticipantCompareOut:
    return ledger.participant_compare(left_id, right_id, from_revision, to_revision)


@id_router.get(
    "/participants/{participant_id}/score-breakdown",
    response_model=ScoreBreakdownOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {"description": "Negative revision"},
    },
)
def get_score_breakdown(
    participant_id: str = Path(
        description=(
            "Opaque participant ID. URL-encode reserved characters: '/' as "
            "%2F, '%' as %25, non-ASCII as UTF-8 percent-encoding."
        )
    ),
    revision: Optional[int] = Query(
        default=None,
        ge=0,
        description="Snapshot revision; defaults to the latest. "
        "0 means the empty ledger.",
    ),
) -> ScoreBreakdownOut:
    return ledger.score_breakdown(participant_id, revision)


@id_router.get(
    "/participants/{participant_id}/threshold-intervals",
    response_model=ThresholdIntervalsOut,
    responses={
        404: {"description": "to_revision beyond the current maximum"},
        422: {
            "description": "Missing/non-integer params, negative revision, or from_revision > to_revision"
        },
    },
)
def get_threshold_intervals(
    participant_id: str = Path(
        description=(
            "Opaque participant ID. URL-encode reserved characters: '/' as "
            "%2F, '%' as %25, non-ASCII as UTF-8 percent-encoding."
        )
    ),
    threshold: int = Query(description="Inclusive total-points threshold (any integer)."),
    from_revision: int = Query(
        description="Earlier revision; 0 means the empty ledger.",
        ge=0,
    ),
    to_revision: int = Query(
        description="Later revision; must be >= from_revision.",
        ge=0,
    ),
) -> ThresholdIntervalsOut:
    return ledger.threshold_intervals(
        participant_id, threshold, from_revision, to_revision
    )


@app.post(
    "/rewards/preview",
    response_model=RewardPreviewOut,
    responses={
        404: {"description": "Revision beyond the current maximum"},
        422: {
            "description": (
                "revision is not a JSON integer, or prize_cents is not "
                "1–100 non-negative JSON integers"
            )
        },
    },
)
def post_rewards_preview(preview: RewardPreviewIn) -> RewardPreviewOut:
    return ledger.rewards_preview(preview)


# Mounted last: none of its paths overlap the app-level routes above, and
# its internal static-before-dynamic order is preserved on inclusion.
app.include_router(id_router)
