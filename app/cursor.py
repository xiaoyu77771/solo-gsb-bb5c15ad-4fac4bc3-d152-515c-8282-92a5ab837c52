"""HMAC-signed cursors for ``GET /leaderboard/page``.

A cursor is ``<payload>.<signature>`` where ``payload`` is
``base64url(JSON)`` carrying the cursor version, the pinned revision and
the last sort key ``(total_points, participant_id)``, and ``signature``
is ``base64url(HMAC-SHA256(payload, secret))``.

Secret resolution
-----------------
#. The ``LEADERBOARD_CURSOR_SECRET`` environment variable wins when set
   to a non-empty value (explicit deployment configuration).
#. Otherwise a secret is generated once with :mod:`secrets` and stored
   in ``<data-dir>/cursor_secret`` next to the SQLite database. Creation
   is atomic (temp file + ``os.link``), so processes starting at the
   same time share one value; the persisted value keeps validating
   already-issued cursors across restarts.
#. There is deliberately no compiled-in fallback secret: a missing file
   that cannot be created, an unreadable/empty secret file, or any
   permission problem is fatal at startup rather than silently falling
   back to a publicly known default that any client could forge.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
from typing import NamedTuple, Optional

from fastapi import HTTPException

from . import db

CURSOR_VERSION = 1
CURSOR_SECRET_ENV = "LEADERBOARD_CURSOR_SECRET"
#: Secret file living in the same directory as the SQLite database.
CURSOR_SECRET_FILENAME = "cursor_secret"
_SECRET_BYTES = 32  # 256 random bits, hex-encoded on disk
_LOAD_RETRIES = 8

_lock = threading.Lock()
_cached_secret: Optional[bytes] = None


class CursorPayload(NamedTuple):
    revision: int
    total_points: int
    participant_id: str


def _secret_path() -> str:
    db_path = os.path.abspath(db.DB_PATH)
    return os.path.join(os.path.dirname(db_path), CURSOR_SECRET_FILENAME)


def _read_secret_file(path: str) -> Optional[bytes]:
    """Return the stored secret, or None when the file is absent.

    Raises RuntimeError on any read/permission problem or on an empty
    file: such states must never silently degrade to a known default.
    """
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise RuntimeError(f"cannot read cursor secret file {path}: {exc}") from exc
    secret_value = data.strip()
    if not secret_value:
        raise RuntimeError(f"cursor secret file {path} is empty")
    return secret_value


def _create_secret_file(path: str) -> None:
    """Create ``path`` atomically with a fresh random secret.

    Writes to a mode-0600 temp file in the same directory and then uses
    ``os.link`` for the final name. Hard links cannot replace an
    existing file, so concurrent initializers share exactly one secret
    (losers remove only their own temp file) and nothing can observe a
    half-written file.
    """
    directory = os.path.dirname(path)
    token = secrets.token_hex(8)
    candidate = os.path.join(
        directory, f".{CURSOR_SECRET_FILENAME}.{os.getpid()}.{token}"
    )
    try:
        fd = os.open(candidate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            os.write(fd, secrets.token_hex(_SECRET_BYTES).encode("ascii"))
            os.fsync(fd)
            os.close(fd)
            fd = -1
            os.link(candidate, path)
        finally:
            if fd >= 0:
                os.close(fd)
    except FileExistsError:
        # Another process/thread won the race: keep its secret.
        return
    except OSError as exc:
        raise RuntimeError(f"cannot persist cursor secret {path}: {exc}") from exc
    finally:
        try:
            os.unlink(candidate)
        except FileNotFoundError:
            pass
        except OSError:
            # The secret (if linked) is intact; stale temp cleanup is best-effort.
            pass


def _load_file_secret() -> bytes:
    """Load the persisted secret, generating one once if none exists."""
    path = _secret_path()
    directory = os.path.dirname(path)
    for _ in range(_LOAD_RETRIES):
        value = _read_secret_file(path)
        if value is not None:
            return value
        try:
            os.makedirs(directory, exist_ok=True)
        except OSError as exc:
            raise RuntimeError(
                f"cannot create data directory {directory} for cursor secret: {exc}"
            ) from exc
        _create_secret_file(path)
        # Re-read: in a race another process may have published its own
        # file first; the loop converges on the persisted value.
    value = _read_secret_file(path)
    if value is None:
        raise RuntimeError(f"cursor secret file {path} never appeared after initialization")
    return value


def _secret() -> bytes:
    """Resolve the effective HMAC secret (env override or persisted file)."""
    global _cached_secret
    explicit = os.environ.get(CURSOR_SECRET_ENV)
    if explicit:
        return explicit.encode("utf-8")
    if _cached_secret is not None:
        return _cached_secret
    with _lock:
        if _cached_secret is None:
            _cached_secret = _load_file_secret()
        return _cached_secret


def init_cursor_secret() -> None:
    """Resolve the cursor secret at process startup; fail hard if unusable.

    Called from the application lifespan so that permission problems and
    an unwritable data directory abort startup instead of surfacing as
    per-request errors or falling back to a public default.
    """
    _secret()


def _reset_cached_secret() -> None:
    """Test-only hook: forget the cached file secret."""
    global _cached_secret
    with _lock:
        _cached_secret = None


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
