"""POST /webhook/monday/{secret} — receives Monday webhook events.

The secret is a shared token embedded in the URL path. The webhook is
registered with Monday's `create_webhook` mutation using a URL that
already contains the secret, so any inbound request lacking the secret
returns 404 (the FastAPI router never matches). The path-secret value
is constant-time-compared against the configured secret.

(Webhooks created via `create_webhook` are NOT JWT-signed by Monday —
JWT signing only applies to Monday Apps Framework integration recipes.
If we migrate to Apps Framework later, add an Authorization-header JWT
verification branch alongside the path-secret check.)

Two patterns:
1. Challenge handshake on first registration: Monday POSTs
   `{"challenge": "..."}` and expects the same value echoed back.
2. Event payloads: Monday POSTs `{"event": {...}}` with details about
   what changed.

The handler does the minimum work synchronously (validate secret,
classify, echo-filter, enqueue) and returns 200 fast. The async worker
does the actual scheduling work.

Phase 1 dispatch:
- Capacity Engine column changes → CapacityChanged event (handler stub)
- Schedule column changes → echo-filter by userId; if not engine, log
  (handlers for Priority→Expedite, drag, Status→Done are E5/E6 work)
- Blend Records column changes → log (E5 actual_start handler pending)
- Anything else → log and 200

Echo filtering: webhook payload includes `event.userId`. All engine
writes go through MONDAY_GRAYSPACE_TOKEN, which is bound to a specific
Monday user. If userId matches the engine's user id, the webhook is our
own echo and is dropped.
"""

from __future__ import annotations

import hmac
import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from datetime import datetime, timezone

from engine.config import get_settings
from engine.core.timezone import now_local
from engine.io.engine_identity import get_engine_user_id
from engine.io.worker import WorkerNotRunning, enqueue_event
from engine.models import ActualStartReported, CapacityChanged

router = APIRouter(tags=["webhook"])
log = logging.getLogger(__name__)


def _check_secret(provided: str) -> None:
    """Constant-time compare provided URL-path secret against configured."""
    s = get_settings()
    configured = s.monday_webhook_secret or ""
    if not configured:
        # Fail closed: never accept webhooks if we have no secret to verify.
        raise HTTPException(status_code=503, detail="webhook secret not configured")
    if not hmac.compare_digest(provided, configured):
        raise HTTPException(status_code=401, detail="invalid webhook secret")


@router.post("/webhook/monday/{secret}")
async def webhook_monday(secret: str, request: Request) -> dict[str, Any]:
    """Receive a Monday webhook event. Always returns 200 quickly on auth pass."""
    _check_secret(secret)
    payload = await request.json()

    # 1. Challenge handshake — Monday tests the URL during webhook setup.
    if "challenge" in payload:
        log.info("Monday webhook challenge received")
        return {"challenge": payload["challenge"]}

    # 2. Real event.
    event = payload.get("event") or {}
    board_id = event.get("boardId")
    pulse_id = str(event.get("pulseId")) if event.get("pulseId") is not None else None
    event_type = event.get("type")
    user_id = str(event.get("userId")) if event.get("userId") is not None else None

    log.info(
        "webhook received: board=%s pulse=%s type=%s user=%s",
        board_id, pulse_id, event_type, user_id,
    )

    # Echo filter — drop our own writes before any dispatch logic.
    if user_id is not None:
        try:
            engine_user = await get_engine_user_id()
        except Exception:
            # If we can't determine engine identity, fail open (process the
            # event) rather than silently swallow real operator changes.
            log.exception("could not resolve engine user id; processing event without echo filter")
            engine_user = None
        if engine_user is not None and user_id == engine_user:
            log.info(
                "webhook from engine user (id=%s) — suppressing as echo (board=%s pulse=%s)",
                user_id, board_id, pulse_id,
            )
            return {"status": "ignored", "kind": "echo"}

    s = get_settings()
    if board_id == s.gray_space_capacity_engine_board and pulse_id is not None:
        # Capacity change on a machine — enqueue a CapacityChanged event.
        try:
            await enqueue_event(CapacityChanged(machine_id=pulse_id))
        except WorkerNotRunning:
            # Still ack to Monday so it doesn't retry; the operator will
            # see the engine as unhealthy via /health.
            log.error("worker not running; dropping CapacityChanged for machine=%s", pulse_id)
            return {"status": "dropped", "kind": "worker_unavailable"}
        return {"status": "enqueued", "kind": "capacity_changed"}

    if board_id == s.gray_space_schedule_board and pulse_id is not None:
        # Real (operator-driven) change — handlers for Status/Priority/drag
        # are E5/E6 work. For now, log and acknowledge.
        log.info(
            "schedule event for pulse=%s type=%s (handler not yet implemented)",
            pulse_id, event_type,
        )
        return {"status": "received", "kind": "schedule_change_unhandled"}

    if board_id == s.gray_space_blend_records_board and pulse_id is not None:
        # E5: only react to Blend Status changes — every other column edit
        # on Blend Records is operator metadata we don't track.
        if event.get("columnId") != s.col_blend_status:
            return {"status": "received", "kind": "blend_records_ignored_column"}

        new_label = _extract_status_label(event.get("value"))
        if new_label != s.blend_status_pressing_label:
            log.info(
                "blend-records status changed to %r (pulse=%s); only %r triggers actual_start",
                new_label, pulse_id, s.blend_status_pressing_label,
            )
            return {"status": "received", "kind": "blend_records_status_not_actionable"}

        # Use Monday's changedAt if available (more accurate than webhook
        # receipt time); fall back to now() in factory tz.
        actual_at = _resolve_actual_at(event, s.factory_tz)
        try:
            await enqueue_event(
                ActualStartReported(
                    job_reference_id=pulse_id,
                    stage_id=s.blend_status_pressing_stage_id,
                    actual_at=actual_at,
                )
            )
        except WorkerNotRunning:
            log.error("worker not running; dropping ActualStartReported for blend=%s", pulse_id)
            return {"status": "dropped", "kind": "worker_unavailable"}
        return {"status": "enqueued", "kind": "actual_start_reported"}

    return {"status": "received", "kind": "unrecognized_source"}


def _extract_status_label(value: Any) -> str | None:
    """Pull `label.text` out of a Monday status (color) column webhook value.

    Webhook payload shape: `value` is a dict like
    {"label": {"text": "Pressing", "index": 5, "style": {...}}, ...}
    or None when the column was cleared. Returns None on any malformed shape.
    """
    if not isinstance(value, dict):
        return None
    label = value.get("label")
    if not isinstance(label, dict):
        return None
    text = label.get("text")
    return text if isinstance(text, str) else None


def _resolve_actual_at(event: dict[str, Any], factory_tz: str):
    """Prefer Monday's `changedAt` Unix timestamp; fall back to now()."""
    changed_at = event.get("changedAt")
    if isinstance(changed_at, (int, float)) and changed_at > 0:
        # Monday sends Unix epoch seconds. Convert to local (factory) time
        # for consistency with the rest of the engine's local-time convention.
        from zoneinfo import ZoneInfo
        return datetime.fromtimestamp(float(changed_at), tz=timezone.utc).astimezone(ZoneInfo(factory_tz))
    return now_local(factory_tz)
