"""IO shim for reading a Production Schedule item's Spec Sheet Payload.

Pure-core `engine.core.spec_sheet` does the parsing + translation. This
module does the Monday read. Separated so tests can mock the IO without
touching the pure-core logic.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.config import get_settings
from engine.io.monday import nexiuum_client

log = logging.getLogger(__name__)


class ProductionScheduleReadError(RuntimeError):
    """Raised when the engine can't fetch the requested Production Schedule
    item — board misconfigured, item deleted, or token can't see it.
    """


async def read_spec_sheet_payload_for_item(item_id: str) -> str:
    """Fetch a Production Schedule item's `Spec Sheet Payload` column text.

    Returns the raw JSON string (the long-text column's value). Caller
    parses with `engine.core.spec_sheet.parse_spec_sheet_payload`.

    Raises `ProductionScheduleReadError` when:
    - The item doesn't exist on the configured board.
    - The Nexiuum token isn't configured (engine wasn't set up for
      Phase 2 dual-instance).
    - The Spec Sheet Payload column is missing or blank — the form
      didn't write to this item (something else created it).
    """
    s = get_settings()
    if not s.nexiuum_monday_token:
        raise ProductionScheduleReadError(
            "NEXIUUM_MONDAY_TOKEN is not configured; can't read Production "
            "Schedule items. Set it before invoking the spec-sheet webhook."
        )

    async with nexiuum_client() as client:
        item = await client.fetch_item(
            item_id, column_ids=[s.col_ps_spec_sheet_payload],
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
    return payload_text


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
