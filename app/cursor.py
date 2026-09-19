"""HMAC-signed cursors for ``GET /leaderboard/page``.

A cursor is ``<payload>.<signature>`` where ``payload`` is
``base64url(JSON)`` carrying the cursor version, the pinned revision and
the last sort key ``(total_points, participant_id)``, and ``signature``
is ``base64url(HMAC-SHA256(payload, secret))``. The secret comes from
the ``LEADERBOARD_CURSOR_SECRET`` environment variable; a
development-only default is used when it is unset.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import NamedTuple

from fastapi import HTTPException

CURSOR_VERSION = 1
CURSOR_SECRET_ENV = "LEADERBOARD_CURSOR_SECRET"
_DEV_SECRET = "dev-only-insecure-cursor-secret"


class CursorPayload(NamedTuple):
    revision: int
    total_points: int
    participant_id: str


def _secret() -> bytes:
    return os.environ.get(CURSOR_SECRET_ENV, _DEV_SECRET).encode("utf-8")


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(data: str) -> bytes:
    padded = data + "=" * (-len(data) % 4)
    return base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)


def _signature(payload_b64: str) -> str:
    digest = hmac.new(_secret(), payload_b64.encode("ascii"), hashlib.sha256).digest()
    return _b64encode(digest)


def encode_cursor(revision: int, total_points: int, participant_id: str) -> str:
    payload = {
        "v": CURSOR_VERSION,
        "rev": revision,
        "pts": total_points,
        "pid": participant_id,
    }
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return f"{payload_b64}.{_signature(payload_b64)}"


def _invalid(detail: str) -> HTTPException:
    return HTTPException(status_code=422, detail=detail)


def decode_cursor(token: str) -> CursorPayload:
    """Verify and decode ``token``; any tampering or malformed input -> 422."""
    if not token.isascii():
        raise _invalid("malformed cursor")
    parts = token.split(".")
    if len(parts) != 2 or not all(parts):
        raise _invalid("malformed cursor")
    payload_b64, sig_b64 = parts
    if not hmac.compare_digest(_signature(payload_b64), sig_b64):
        raise _invalid("cursor signature mismatch")
    try:
        payload = json.loads(_b64decode(payload_b64))
    except ValueError:
        raise _invalid("malformed cursor payload")
    if not isinstance(payload, dict):
        raise _invalid("malformed cursor payload")
    if payload.get("v") != CURSOR_VERSION:
        raise _invalid(f"unsupported cursor version: {payload.get('v')!r}")
    rev, pts, pid = payload.get("rev"), payload.get("pts"), payload.get("pid")
    if not isinstance(rev, int) or isinstance(rev, bool) or rev < 1:
        raise _invalid("cursor payload field 'rev' must be an integer >= 1")
    if not isinstance(pts, int) or isinstance(pts, bool):
        raise _invalid("cursor payload field 'pts' must be an integer")
    if not isinstance(pid, str) or not pid:
        raise _invalid("cursor payload field 'pid' must be a non-empty string")
    return CursorPayload(revision=rev, total_points=pts, participant_id=pid)
