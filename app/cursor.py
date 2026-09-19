"""HMAC-signed cursors for ``GET /leaderboard/page``.

A cursor is ``<payload>.<signature>`` where ``payload`` is
``base64url(JSON)`` carrying the cursor version, the pinned revision and
the last sort key ``(total_points, participant_id)``, and ``signature``
is ``base64url(HMAC-SHA256(payload, secret))``.

Secret resolution (once, at startup — see :func:`init_cursor_secret`):

1. An explicit ``LEADERBOARD_CURSOR_SECRET`` environment variable always
   wins and is never written to disk.
2. Otherwise a 256-bit random secret is generated on first startup and
   stored as ``leaderboard-cursor-secret`` (mode 0600) next to the
   database file. Creation is atomic (write temp file, then hard-link),
   so concurrently starting instances converge on the same value, and
   the persisted file keeps previously issued cursors verifiable across
   restarts.

There is no built-in fallback secret: any failure to read or create the
secret file (permissions, empty file, unwritable directory, ...) raises
:class:`CursorSecretError` and aborts startup rather than letting
clients forge cursors against a publicly known key.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import tempfile
import threading
from typing import NamedTuple, Optional

from fastapi import HTTPException

from . import db

CURSOR_VERSION = 1
CURSOR_SECRET_ENV = "LEADERBOARD_CURSOR_SECRET"
SECRET_FILE_NAME = "leaderboard-cursor-secret"

# Bounded retries for the create/read race: we lose the atomic-create race
# to a peer, then its file vanishes before we can read it (e.g. a wiped
# volume). Practically unreachable, but never loop forever.
_CREATE_RACE_ATTEMPTS = 5

_secret_lock = threading.Lock()
_cached_secret: Optional[bytes] = None


class CursorSecretError(RuntimeError):
    """The cursor secret could not be resolved; the service must not start."""


class CursorPayload(NamedTuple):
    revision: int
    total_points: int
    participant_id: str


def init_cursor_secret() -> bytes:
    """Resolve and cache the HMAC secret; called once at app startup.

    Re-resolving (after changing the environment or the database path)
    is how a restart with a new configuration is expressed. Raises
    :class:`CursorSecretError` on any failure — there is no fallback.
    """
    global _cached_secret
    with _secret_lock:
        _cached_secret = _resolve_secret()
        return _cached_secret


def _resolve_secret() -> bytes:
    env_secret = os.environ.get(CURSOR_SECRET_ENV)
    if env_secret:
        return env_secret.encode("utf-8")
    return _load_or_create_file_secret(_secret_file_path())


def _secret_file_path() -> str:
    directory = os.path.dirname(os.path.abspath(db.DB_PATH))
    return os.path.join(directory, SECRET_FILE_NAME)


def _read_secret_file(path: str) -> bytes:
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        raise
    except OSError as exc:
        raise CursorSecretError(
            f"cannot read cursor secret file {path!r}: {exc}"
        ) from exc
    secret = data.strip()
    if not secret:
        raise CursorSecretError(f"cursor secret file {path!r} is empty")
    return secret


def _create_secret_file(path: str, secret_hex: str) -> None:
    """Create ``path`` with ``secret_hex`` exactly once, atomically.

    The content is written to a temp file (mode 0600, fsynced) and then
    hard-linked into place, so the destination never exists in a partial
    state. Raises :class:`FileExistsError` if a concurrent startup won
    the race, :class:`CursorSecretError` on any other I/O failure.
    """
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=directory, prefix=".cursor-secret-", suffix=".tmp"
        )
    except OSError as exc:
        raise CursorSecretError(
            f"cannot create cursor secret file in {directory!r}: {exc}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="ascii") as fh:
            fh.write(secret_hex + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp_path, path)
    except FileExistsError:
        raise
    except OSError as exc:
        raise CursorSecretError(
            f"cannot create cursor secret file {path!r}: {exc}"
        ) from exc
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _load_or_create_file_secret(path: str) -> bytes:
    for _ in range(_CREATE_RACE_ATTEMPTS):
        try:
            return _read_secret_file(path)
        except FileNotFoundError:
            pass  # first boot: generate below
        secret_hex = secrets.token_hex(32)  # 256 bits, ASCII-safe on disk
        try:
            _create_secret_file(path, secret_hex)
        except FileExistsError:
            continue  # a concurrent startup won; read its value
        return secret_hex.encode("ascii")
    raise CursorSecretError(
        f"cursor secret file {path!r} disappeared while being initialized"
    )


def _secret() -> bytes:
    if _cached_secret is None:
        return init_cursor_secret()
    return _cached_secret


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
