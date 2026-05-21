"""POST /webhook/monday — receives Monday webhook events.

Two patterns:
1. Challenge handshake on first registration: Monday POSTs
   `{"challenge": "..."}` and expects the same value echoed back.
2. Event payloads: Monday POSTs `{"event": {...}}` with details about
   what changed.

The handler does the minimum work synchronously (validate, classify,
echo-guard check, enqueue) and returns 200 fast. The async worker does
the actual scheduling work.

Phase 1 dispatch:
- Capacity Engine column changes → CapacityChanged event (handler stub)
- Schedule column changes → echo-guard check; if engine-origin, drop;
  otherwise log (handlers for Priority→Expedite, drag, Status→Done are
  E5/E6 work)
- Anything else → log and 200

Source-board events (Blend Records status flips for actuals) are E5.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from engine.config import get_settings
from engine.io.echo_guard import get_echo_guard
from engine.io.monday import gray_space_client
from engine.io.worker import submit_event
from engine.models import CapacityChanged

router = APIRouter(tags=["webhook"])
log = logging.getLogger(__name__)


@router.post("/webhook/monday")
async def webhook_monday(request: Request) -> dict[str, Any]:
    """Receive a Monday webhook event. Always returns 200 quickly."""
    payload = await request.json()

    # 1. Challenge handshake — Monday tests the URL during webhook setup
    if "challenge" in payload:
        log.info("Monday webhook challenge received")
        return {"challenge": payload["challenge"]}

    # 2. Real event
    event = payload.get("event") or {}
    board_id = event.get("boardId")
    pulse_id = str(event.get("pulseId")) if event.get("pulseId") is not None else None
    event_type = event.get("type")

    log.info(
        "webhook received: board=%s pulse=%s type=%s",
        board_id, pulse_id, event_type,
    )

    s = get_settings()
    if board_id == s.gray_space_capacity_engine_board and pulse_id is not None:
        # Capacity change on a machine — enqueue a CapacityChanged event.
        await submit_event(CapacityChanged(machine_id=pulse_id))
        return {"status": "enqueued", "kind": "capacity_changed"}

    if board_id == s.gray_space_schedule_board and pulse_id is not None:
        # Schedule item change — check echo guard before enqueueing anything.
        is_echo = await _is_engine_echo(pulse_id)
        if is_echo:
            log.info("schedule event for pulse=%s suppressed by echo guard", pulse_id)
            return {"status": "ignored", "kind": "echo"}
        # Real (operator-driven) change — handlers for Status/Priority/drag
        # are E5/E6 work. For now, log and acknowledge.
        log.info(
            "schedule event for pulse=%s type=%s (handler not yet implemented)",
            pulse_id, event_type,
        )
        return {"status": "received", "kind": "schedule_change_unhandled"}

    if board_id == s.gray_space_blend_records_board and pulse_id is not None:
        # Source-board event — actuals processing is E5. Log and ack.
        log.info(
            "blend-records event for pulse=%s type=%s (E5 handler pending)",
            pulse_id, event_type,
        )
        return {"status": "received", "kind": "blend_records_unhandled"}

    return {"status": "received", "kind": "unrecognized_source"}


async def _is_engine_echo(schedule_item_id: str) -> bool:
    """Read the Schedule item's current `last_reflow_hash` and check the guard.

    Returns True only if the hash is non-empty AND in the echo guard's recent
    set. Returns False on any read error (don't swallow real operator changes).
    """
    s = get_settings()
    query = """
    query($id: [ID!], $col: String!) {
      items(ids: $id) {
        column_values(ids: [$col]) {
          id
          text
        }
      }
    }
    """
    try:
        async with gray_space_client() as c:
            data = await c.query(
                query, {"id": [schedule_item_id], "col": s.col_schedule_last_reflow_hash}
            )
        items = data.get("items") or []
        if not items:
            return False
        col_values = items[0].get("column_values") or []
        if not col_values:
            return False
        hash_value = col_values[0].get("text") or ""
        return get_echo_guard().is_engine_origin(hash_value)
    except Exception:
        log.exception("echo-guard lookup failed for pulse=%s", schedule_item_id)
        return False
