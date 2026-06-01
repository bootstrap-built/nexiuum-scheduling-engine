"""Tests for engine.io.spec_sheet_io — the Production Schedule read shim.

Covers the new N# extraction off the "Nexiuum #" board_relation column
(display_value, not text) and the combined payload+N# ingest read. The
pure parse/translate logic lives in engine.core.spec_sheet (tested in
test_spec_sheet.py); this file covers the IO-side column plucking.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from engine.config import get_settings
from engine.io.spec_sheet_io import (
    PSItemIngest,
    ProductionScheduleReadError,
    SpecSheetPayloadAbsent,
    _extract_n_number,
    read_ps_item_for_ingest,
)

S = get_settings()


# ─── _extract_n_number — board_relation display_value parsing ──────────────


def test_extract_n_number_reads_display_value():
    """board_relation N# comes through `display_value`, not `text`."""
    item = {
        "column_values": [
            {"id": S.col_ps_n_number, "text": None, "display_value": "N3629"},
        ]
    }
    assert _extract_n_number(item, S.col_ps_n_number) == "N3629"


def test_extract_n_number_strips_whitespace():
    item = {"column_values": [{"id": S.col_ps_n_number, "display_value": "  N42 "}]}
    assert _extract_n_number(item, S.col_ps_n_number) == "N42"


def test_extract_n_number_none_when_unlinked():
    """Empty display_value (no PO linked yet) → None, not ''."""
    item = {"column_values": [{"id": S.col_ps_n_number, "display_value": ""}]}
    assert _extract_n_number(item, S.col_ps_n_number) is None


def test_extract_n_number_none_when_column_absent():
    item = {"column_values": [{"id": "some_other_col", "text": "x"}]}
    assert _extract_n_number(item, S.col_ps_n_number) is None


@pytest.mark.parametrize(
    "display_value",
    [
        "N1, N2",      # board_relation linked to multiple POs
        "ROAR LLC",    # arbitrary linked-item name, not an N#
        "n3629",       # lowercase
        "N3629-A",     # unexpected suffix
        "3629",        # missing N prefix
    ],
)
def test_extract_n_number_rejects_malformed_or_multilink(display_value):
    """A display_value that isn't a clean N# degrades to None (→ #<last-6>
    fallback) rather than being stamped onto every Slot."""
    item = {"column_values": [{"id": S.col_ps_n_number, "display_value": display_value}]}
    assert _extract_n_number(item, S.col_ps_n_number) is None


# ─── read_ps_item_for_ingest — combined payload + N# read ──────────────────


@pytest.mark.asyncio
async def test_read_ps_item_for_ingest_returns_payload_and_n_number(monkeypatch):
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "tok")

    fake_item = {
        "id": "ps-1",
        "name": "Viper PO N3629 (D) Blue",
        "column_values": [
            {"id": S.col_ps_spec_sheet_payload, "text": '{"x": 1}'},
            {"id": S.col_ps_n_number, "display_value": "N3629"},
        ],
    }
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.fetch_item = AsyncMock(return_value=fake_item)

    with patch("engine.io.spec_sheet_io.nexiuum_client", return_value=client):
        result = await read_ps_item_for_ingest("ps-1")

    assert isinstance(result, PSItemIngest)
    assert result.payload_text == '{"x": 1}'
    assert result.n_number == "N3629"
    # The reader must request BOTH columns in one fetch.
    _, kwargs = client.fetch_item.call_args
    assert set(kwargs["column_ids"]) == {
        S.col_ps_spec_sheet_payload, S.col_ps_n_number,
    }


@pytest.mark.asyncio
async def test_read_ps_item_for_ingest_n_number_none_is_not_an_error(monkeypatch):
    """A missing N# is a nullable label — payload still ingests fine."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "tok")
    fake_item = {
        "id": "ps-2", "name": "x",
        "column_values": [
            {"id": S.col_ps_spec_sheet_payload, "text": '{"x": 1}'},
            {"id": S.col_ps_n_number, "display_value": ""},
        ],
    }
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.fetch_item = AsyncMock(return_value=fake_item)

    with patch("engine.io.spec_sheet_io.nexiuum_client", return_value=client):
        result = await read_ps_item_for_ingest("ps-2")
    assert result.n_number is None
    assert result.payload_text == '{"x": 1}'


@pytest.mark.asyncio
async def test_read_ps_item_for_ingest_blank_payload_raises(monkeypatch):
    """A missing/blank Spec Sheet Payload raises SpecSheetPayloadAbsent — the
    benign "not a Nexiuum form order" case the worker skips quietly. Still a
    ProductionScheduleReadError subclass so broad handlers keep catching it."""
    monkeypatch.setenv("MONDAY_NEXIUUM_TOKEN", "tok")
    fake_item = {
        "id": "ps-3", "name": "x",
        "column_values": [
            {"id": S.col_ps_spec_sheet_payload, "text": ""},
            {"id": S.col_ps_n_number, "display_value": "N1"},
        ],
    }
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.fetch_item = AsyncMock(return_value=fake_item)

    with patch("engine.io.spec_sheet_io.nexiuum_client", return_value=client):
        with pytest.raises(SpecSheetPayloadAbsent):
            await read_ps_item_for_ingest("ps-3")
        # Subclass relationship holds — broad handlers still catch it.
        with pytest.raises(ProductionScheduleReadError):
            await read_ps_item_for_ingest("ps-3")
