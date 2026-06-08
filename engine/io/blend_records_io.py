"""IO shim for reading a Blend Records item's correlation link (#23).

The Blend Records board (Gray Space) carries a text column pointing back at
the originating Production Schedule item — written by the blend-intake
workflow as `source_item_id` when it creates the Blend Record. The engine
resolves Blend Record → PS Order through this column on every Blend Records
status event: the Blending trigger (ADR-0004) and the actuals (Pressing /
Done) handlers all arrive keyed by Blend Record id, but Orders and Slots are
keyed by the PS item id.

Pure read shim, separated from pure-core so tests can mock the Monday read.
"""
from __future__ import annotations

import logging
from typing import Any

from engine.config import get_settings
from engine.io.monday import gray_space_client

log = logging.getLogger(__name__)


class BlendRecordReadError(RuntimeError):
    """Raised when the engine can't fetch the requested Blend Records item —
    board misconfigured, item deleted, or token can't see it.
    """


async def read_blend_record_ps_item_id(blend_record_id: str) -> str | None:
    """Resolve a Blend Records item to its linked Production Schedule item id.

    Reads the Blend Records "source item" text column — the PS item id the
    blend-intake workflow stamped at creation. Returns that PS item id, or
    `None` when the column is absent/blank: a legacy Gray-Space-origin Blend
    Record (created before the spec-form intake) carries no link, and its
    Order is keyed by the Blend Record id itself, so callers fall back to
    `blend_record_id` on None.

    Raises `BlendRecordReadError` when the item can't be fetched at all
    (deleted, or the Gray Space token can't see the board).
    """
    s = get_settings()
    async with gray_space_client() as client:
        item = await client.fetch_item(
            blend_record_id, column_ids=[s.col_blend_source_item],
        )
    if item is None:
        raise BlendRecordReadError(
            f"Blend Records item {blend_record_id} not found (board "
            f"{s.gray_space_blend_records_board}). The webhook may have fired "
            f"against a deleted item, or the engine's token can't see it."
        )
    return _extract_text_value(item, s.col_blend_source_item)


def _extract_text_value(item: dict[str, Any], column_id: str) -> str | None:
    """Pull a plain text column's value; None when absent or blank."""
    for cv in item.get("column_values") or []:
        if cv.get("id") != column_id:
            continue
        text = (cv.get("text") or "").strip()
        return text or None
    return None
