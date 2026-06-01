"""IO shim for reading a Production Schedule item's Spec Sheet Payload.

Pure-core `engine.core.spec_sheet` does the parsing + translation. This
module does the Monday read. Separated so tests can mock the IO without
touching the pure-core logic.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from engine.config import get_settings
from engine.core.labels import is_n_number
from engine.io.monday import nexiuum_client

log = logging.getLogger(__name__)


class ProductionScheduleReadError(RuntimeError):
    """Raised when the engine can't fetch the requested Production Schedule
    item — board misconfigured, item deleted, or token can't see it.
    """


@dataclass(frozen=True)
class PSItemIngest:
    """What the engine reads off a Production Schedule item to ingest an Order.

    `payload_text` is the raw Spec Sheet Payload JSON (required — a missing
    payload is a hard read error). `n_number` is the linked PO's N# from the
    "Nexiuum #" board_relation's display_value, or None when the item isn't
    linked to a PO yet (a label, not a key — None degrades gracefully).
    """

    payload_text: str
    n_number: str | None


async def read_ps_item_for_ingest(item_id: str) -> PSItemIngest:
    """Fetch a Production Schedule item's Spec Sheet Payload + N# in one read.

    Returns a `PSItemIngest`. The caller parses `payload_text` with
    `engine.core.spec_sheet.parse_spec_sheet_payload` and threads `n_number`
    into the ScheduleNewOrder.

    Raises `ProductionScheduleReadError` when:
    - The item doesn't exist on the configured board.
    - The Nexiuum token isn't configured (engine wasn't set up for
      Phase 2 dual-instance).
    - The Spec Sheet Payload column is missing or blank — the form
      didn't write to this item (something else created it).

    A missing/blank N# is NOT an error — it's a nullable label; the order
    schedules with `n_number=None` and the labels module falls back.
    """
    s = get_settings()
    if not s.nexiuum_monday_token:
        raise ProductionScheduleReadError(
            "NEXIUUM_MONDAY_TOKEN is not configured; can't read Production "
            "Schedule items. Set it before invoking the spec-sheet webhook."
        )

    async with nexiuum_client() as client:
        item = await client.fetch_item(
            item_id,
            column_ids=[s.col_ps_spec_sheet_payload, s.col_ps_n_number],
        )
    if item is None:
        raise ProductionScheduleReadError(
            f"Production Schedule item {item_id} not found (board "
            f"{s.nexiuum_production_schedule_board}). The webhook may "
            f"have fired against a deleted item, or the engine's token "
            f"can't see the board."
        )

    payload_text = _extract_long_text_value(item, s.col_ps_spec_sheet_payload)
    if not payload_text or not payload_text.strip():
        raise ProductionScheduleReadError(
            f"Production Schedule item {item_id} has no Spec Sheet "
            f"Payload value (column {s.col_ps_spec_sheet_payload}). "
            f"The form may not have written to this item, or the column "
            f"was cleared by an operator."
        )

    n_number = _extract_n_number(item, s.col_ps_n_number)
    return PSItemIngest(payload_text=payload_text, n_number=n_number)


def _extract_n_number(item: dict[str, Any], column_id: str) -> str | None:
    """Pull the N# from the "Nexiuum #" board_relation column.

    The N# is the linked PO item's name, surfaced by Monday as the
    board_relation's `display_value` (e.g. "N3629"). `text` is null for
    board_relation columns, so we read `display_value`.

    Returns None when the column is absent, unlinked, OR doesn't parse as a
    well-formed N#. A board_relation linked to multiple POs renders as
    "N1, N2" — `is_n_number` rejects that so a bogus value never gets stamped
    onto every downstream Slot. A label, never a hard requirement; rejection
    degrades gracefully to the engine's `#<last-6>` fallback.
    """
    for cv in item.get("column_values") or []:
        if cv.get("id") != column_id:
            continue
        dv = (cv.get("display_value") or "").strip()
        if not dv:
            return None
        if not is_n_number(dv):
            log.warning(
                "PS item %s: N# column %s display_value %r is not a "
                "well-formed N# (expected 'N<digits>'; possibly multi-linked "
                "or malformed) — treating as no N#.",
                item.get("id"), column_id, dv,
            )
            return None
        return dv
    return None


def _extract_long_text_value(item: dict[str, Any], column_id: str) -> str | None:
    """Pull the `text` field (or the JSON-encoded `value`) for a column.

    Monday's long-text column returns its content in the `text` field of
    the column_value. Falls back to parsing `value` (a JSON-encoded
    structure for non-trivial column types) if `text` is empty.
    """
    for cv in item.get("column_values") or []:
        if cv.get("id") != column_id:
            continue
        text = cv.get("text")
        if text:
            return text
        # Long-text columns occasionally come through as JSON in `value` —
        # `{"text": "..."}` shape. Defensive fallback.
        raw = cv.get("value")
        if raw:
            import json  # noqa: PLC0415
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict) and "text" in obj:
                    return obj["text"]
            except json.JSONDecodeError:
                # If `value` is the raw text (some columns do this), use it
                # directly.
                return raw
        return None
    return None
