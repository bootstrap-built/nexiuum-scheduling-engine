"""Tests for engine.io.blend_records_io — the Blend Record → PS item
correlation read shim (#23).

The Blending trigger and the actuals handlers arrive keyed by Blend Record
id; the engine resolves them to the originating Production Schedule item id
through the `source item` text column the blend-intake workflow stamps.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import get_settings
from engine.io.blend_records_io import (
    BlendRecordReadError,
    _extract_text_value,
    read_blend_record_ps_item_id,
)

S = get_settings()


# ─── _extract_text_value ───────────────────────────────────────────────────


def test_extract_text_value_reads_text():
    item = {"column_values": [{"id": S.col_blend_source_item, "text": "ps-9001"}]}
    assert _extract_text_value(item, S.col_blend_source_item) == "ps-9001"


def test_extract_text_value_strips_whitespace():
    item = {"column_values": [{"id": S.col_blend_source_item, "text": "  ps-9001 "}]}
    assert _extract_text_value(item, S.col_blend_source_item) == "ps-9001"


def test_extract_text_value_none_when_blank():
    item = {"column_values": [{"id": S.col_blend_source_item, "text": ""}]}
    assert _extract_text_value(item, S.col_blend_source_item) is None


def test_extract_text_value_none_when_column_absent():
    item = {"column_values": [{"id": "other", "text": "x"}]}
    assert _extract_text_value(item, S.col_blend_source_item) is None


# ─── read_blend_record_ps_item_id ──────────────────────────────────────────


def _client(fake_item):
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.fetch_item = AsyncMock(return_value=fake_item)
    return client


@pytest.mark.asyncio
async def test_read_resolves_ps_item_id():
    fake_item = {
        "id": "blend-1",
        "column_values": [{"id": S.col_blend_source_item, "text": "ps-9001"}],
    }
    client = _client(fake_item)
    with patch("engine.io.blend_records_io.gray_space_client", return_value=client):
        result = await read_blend_record_ps_item_id("blend-1")

    assert result == "ps-9001"
    # Reads only the correlation column.
    _, kwargs = client.fetch_item.call_args
    assert kwargs["column_ids"] == [S.col_blend_source_item]


@pytest.mark.asyncio
async def test_read_returns_none_for_unlinked_legacy_record():
    """A legacy Gray-Space-origin Blend Record carries no link → None (caller
    falls back to the Blend Record id itself)."""
    fake_item = {
        "id": "blend-2",
        "column_values": [{"id": S.col_blend_source_item, "text": ""}],
    }
    with patch(
        "engine.io.blend_records_io.gray_space_client", return_value=_client(fake_item)
    ):
        result = await read_blend_record_ps_item_id("blend-2")
    assert result is None


@pytest.mark.asyncio
async def test_read_raises_when_item_missing():
    with patch(
        "engine.io.blend_records_io.gray_space_client", return_value=_client(None)
    ):
        with pytest.raises(BlendRecordReadError):
            await read_blend_record_ps_item_id("blend-404")
