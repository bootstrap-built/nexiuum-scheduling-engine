"""Engine identity — the Monday user id the engine writes as.

Used by the webhook handler to filter out our own writes from operator
changes. All engine writes happen via MONDAY_GRAYSPACE_TOKEN, which is
bound to a specific Monday user (e.g. "Gray Space Force"). Monday's
webhook payloads include `event.userId` — comparing that against the
engine's user id is the echo filter.

Detection happens once (lazy) and is cached for the lifetime of the
process. The cache survives across requests but is module-level so tests
can reset it via `reset_engine_user_id()`.

Replaces the v1 `last_reflow_hash`-column-based echo guard, which was
architecturally broken: operator edits don't touch `last_reflow_hash`,
so the column kept the stale engine-written hash and subsequent operator
webhooks were silently suppressed as "echoes."
"""

from __future__ import annotations

import logging

from engine.config import get_settings
from engine.io.monday import gray_space_client

log = logging.getLogger(__name__)

_engine_user_id: str | None = None


async def get_engine_user_id() -> str:
    """Return the Monday user id the engine writes as.

    Order of resolution:
      1. Cached value from a previous call.
      2. `ENGINE_MONDAY_USER_ID` env var (lets tests/operators override).
      3. `{ me { id } }` GraphQL query against Gray Space.

    Raises if neither is available — the engine cannot safely process
    Schedule webhooks without knowing its own user id (the alternative
    is a write loop).
    """
    global _engine_user_id
    if _engine_user_id is not None:
        return _engine_user_id

    s = get_settings()
    if s.engine_monday_user_id:
        _engine_user_id = str(s.engine_monday_user_id)
        return _engine_user_id

    async with gray_space_client() as c:
        data = await c.query("{ me { id } }", {})
    me = (data or {}).get("me") or {}
    user_id = me.get("id")
    if not user_id:
        raise RuntimeError(
            "Failed to detect engine Monday user id via { me { id } }. "
            "Set ENGINE_MONDAY_USER_ID to bypass."
        )
    _engine_user_id = str(user_id)
    log.info("engine Monday user id detected: %s", _engine_user_id)
    return _engine_user_id


def reset_engine_user_id() -> None:
    """Test helper — clear the cached user id."""
    global _engine_user_id
    _engine_user_id = None
