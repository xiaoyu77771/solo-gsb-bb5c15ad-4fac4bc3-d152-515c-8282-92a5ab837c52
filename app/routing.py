"""Routing support for reading back opaque IDs through percent-encoded paths.

The ledger accepts any non-empty string as ``event_id`` / ``participant_id``
(see ``schemas.EventIn``), including values that contain ``/`` or equal a
static route name such as ``changes`` or ``compare``. Starlette matches
routes against the *decoded* request path, which breaks read-back of those
IDs:

* ``GET /events/a%2Fb`` is decoded to ``/events/a/b`` before routing, so the
  single-segment ``{event_id}`` pattern no longer matches (404);
* ``GET /events/%63hanges`` is decoded to ``/events/changes``, so the static
  changes feed shadows the event literally named ``changes``.

``EncodedPathRoute`` instead matches the *raw* (still percent-encoded)
request path and decodes the captured parameters itself:

* static routes keep matching literally — ``/events/changes`` stays the
  changes feed, exactly as before;
* an encoded ID never collides with a static route — ``/events/%63hanges``
  reaches ``GET /events/{event_id}`` with ``event_id == "changes"``;
* ``%2F`` stays inside a single raw segment, so ``/events/a%2Fb`` yields
  ``event_id == "a/b"``.

An unencoded ``/`` remains a path separator: IDs containing it must be sent
percent-encoded, per RFC 3986.
"""

from __future__ import annotations

from typing import Optional, Tuple
from urllib.parse import unquote

from fastapi.routing import APIRoute
from starlette.routing import Match
from starlette.types import Scope


def _consistent_raw_path(scope: Scope) -> Optional[str]:
    """Percent-encoded request path, or ``None`` when it cannot be trusted.

    Returns ``None`` — meaning "fall back to the default decoded-path
    matching" — when the server did not supply ``raw_path``, or when it no
    longer corresponds to ``scope["path"]`` (e.g. the router's
    ``redirect_slashes`` re-match runs with a modified ``path`` but the
    original ``raw_path``).
    """
    raw_path = scope.get("raw_path")
    if not raw_path:
        return None
    if isinstance(raw_path, bytes):
        raw_path = raw_path.decode("ascii", errors="replace")
    # Some ASGI clients append the query string to raw_path.
    raw_path = raw_path.partition("?")[0]
    if unquote(raw_path) != scope["path"]:
        return None
    return raw_path


class EncodedPathRoute(APIRoute):
    """APIRoute that matches the raw (percent-encoded) request path.

    Path parameters captured by this route are percent-decoded after the
    match, so opaque IDs round-trip through URL encoding while static
    routes keep their literal matching behaviour.
    """

    def matches(self, scope: Scope) -> Tuple[Match, Scope]:
        raw_path = _consistent_raw_path(scope)
        if raw_path is None:
            return super().matches(scope)
        inherited = scope.get("path_params") or {}
        match, child_scope = super().matches({**scope, "path": raw_path})
        if match != Match.NONE:
            path_params = child_scope.get("path_params")
            if path_params:
                child_scope["path_params"] = {
                    key: (
                        unquote(value)
                        if key not in inherited and isinstance(value, str)
                        else value
                    )
                    for key, value in path_params.items()
                }
        return match, child_scope
