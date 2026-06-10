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
from engine.io import recent_writes
from engine.io.worker import WorkerNotRunning, enqueue_event
from engine.models import (
    ActualEndReported,
    ActualStartReported,
    BlendingStarted,
    CapacityChanged,
    SpecSheetItemReady,
)

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

    # Echo filter (Codex E4 B1) — suppress by ACTUAL write origin, not user.
    # The engine shares its Monday user with humans and CLI/agent tokens, so
    # userId can't discriminate engine writes; instead apply_plan records
    # every write it performs and only a matching (board, pulse, column)
    # inside the TTL window is an echo. Boards the engine never writes
    # (Blend Records, Production Schedule) can never be suppressed.
    column_id = (event.get("columnId") or None) if isinstance(event, dict) else None
    if pulse_id is not None and recent_writes.is_engine_echo(
        board_id, pulse_id, column_id
    ):
        log.info(
            "suppressing engine-write echo (board=%s pulse=%s column=%s user=%s)",
            board_id, pulse_id, column_id, user_id,
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
        # Use Monday's changedAt if available (more accurate than webhook
        # receipt time); fall back to now() in factory tz.
        actual_at = _resolve_actual_at(event, s.factory_tz)

        if new_label == s.blend_status_blending_label:
            # ADR-0004 — the press-scheduling trigger. A pressing order deferred
            # at create_item is released onto the schedule now. Keyed by the
            # Blend Record id; the worker resolves it to the PS item (#23).
            try:
                await enqueue_event(BlendingStarted(blend_record_id=pulse_id))
            except WorkerNotRunning:
                log.error("worker not running; dropping BlendingStarted for blend=%s", pulse_id)
                return {"status": "dropped", "kind": "worker_unavailable"}
            return {"status": "enqueued", "kind": "blending_started"}

        if new_label == s.blend_status_pressing_label:
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

        if new_label == s.blend_status_done_label:
            # Phase 2C: Blend Status → "Done" closes the press stage AND
            # triggers the baton-pass to any dependent (packaging) slots.
            try:
                await enqueue_event(
                    ActualEndReported(
                        job_reference_id=pulse_id,
                        stage_id=s.blend_status_pressing_stage_id,
                        actual_at=actual_at,
                    )
                )
            except WorkerNotRunning:
                log.error("worker not running; dropping ActualEndReported for blend=%s", pulse_id)
                return {"status": "dropped", "kind": "worker_unavailable"}
            return {"status": "enqueued", "kind": "actual_end_reported"}

        log.info(
            "blend-records status changed to %r (pulse=%s); "
            "actionable labels: %r→start, %r→end",
            new_label, pulse_id,
            s.blend_status_pressing_label, s.blend_status_done_label,
        )
        return {"status": "received", "kind": "blend_records_status_not_actionable"}

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


# ─────────────────────────────────────────────────────────────────────────
# Phase 2D — Spec Sheet item ready trigger
# ─────────────────────────────────────────────────────────────────────────


@router.post("/webhook/monday/spec-sheet/{secret}")
async def webhook_monday_spec_sheet(secret: str, request: Request) -> dict[str, Any]:
    """Phase 2D — receive a "schedule this Production Schedule item" trigger.

    Wired up as a Monday `create_webhook` on the Production Schedule
    board (8196668916) with event `create_pulse` — it fires once per new
    item. The Nexiuum spec-sheet form creates fully-populated items
    (Spec Sheet Payload + Nexiuum # set atomically at creation), so
    everything the engine reads is present at create time; a column-change
    trigger would never fire because Monday emits no column-change events
    for creation-time values (verified against order N3851, Issue #8).

    The board is shared with the regular production flow (Gray Space POs,
    samples) whose items have no Spec Sheet Payload. create_pulse fires for
    those too; the worker reads, finds no payload, and skips them quietly
    (SpecSheetPayloadAbsent → info, not an ingest failure). The handler
    stays event-agnostic — it just needs boardId + pulseId.

    Engine:
    1. Verifies the URL secret (constant-time compare, same pattern as
       the Phase 1 Blend Records webhook).
    2. Echo-filters via the write-origin registry (Codex E4 B1) — the
       engine never writes to Production Schedule, so nothing here can
       ever match; this stays purely as defense in depth.
    3. Extracts the pulseId (Production Schedule item id) from the
       payload.
    4. Enqueues a SpecSheetItemReady event for the worker.

    The worker does the actual read from Monday + payload parsing +
    plan + apply. Webhook always returns 200 fast on auth pass — Monday
    retries on non-2xx and we don't want operator delays from misformed
    payloads to look like webhook outages.
    """
    _check_secret(secret)
    payload = await request.json()

    if "challenge" in payload:
        log.info("Monday spec-sheet webhook challenge received")
        return {"challenge": payload["challenge"]}

    event = payload.get("event") or {}
    board_id = event.get("boardId")
    pulse_id = str(event.get("pulseId")) if event.get("pulseId") is not None else None
    user_id = str(event.get("userId")) if event.get("userId") is not None else None

    log.info(
        "spec-sheet webhook received: board=%s pulse=%s user=%s",
        board_id, pulse_id, user_id,
    )

    if pulse_id is None:
        log.warning("spec-sheet webhook had no pulseId; ignoring")
        return {"status": "received", "kind": "no_pulse_id"}

    # Belt-and-suspenders: only accept triggers from the configured
    # Production Schedule board. Prevents stray automations on other
    # boards from accidentally scheduling against item ids the engine
    # can't actually read.
    s = get_settings()
    if board_id is not None and board_id != s.nexiuum_production_schedule_board:
        log.warning(
            "spec-sheet webhook from unexpected board=%s (expected %s); ignoring",
            board_id, s.nexiuum_production_schedule_board,
        )
        return {"status": "ignored", "kind": "wrong_board"}

    # Echo filter (Codex E4 B1) — write-origin registry, kept for symmetry
    # with the generic handler. The engine never writes to Production
    # Schedule (read-only board), so the registry never holds entries for
    # it and this can never suppress — unlike the user-id guard it
    # replaces, which ate any event from the shared engine user.
    column_id = (event.get("columnId") or None) if isinstance(event, dict) else None
    if recent_writes.is_engine_echo(board_id, pulse_id, column_id):
        log.info(
            "suppressing engine-write echo on spec-sheet route "
            "(board=%s pulse=%s column=%s user=%s)",
            board_id, pulse_id, column_id, user_id,
        )
        return {"status": "ignored", "kind": "echo"}

    try:
        await enqueue_event(SpecSheetItemReady(item_id=pulse_id))
    except WorkerNotRunning:
        log.error("worker not running; dropping SpecSheetItemReady for item=%s", pulse_id)
        return {"status": "dropped", "kind": "worker_unavailable"}
    return {"status": "enqueued", "kind": "spec_sheet_item_ready"}
