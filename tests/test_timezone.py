"""Unit tests for timezone conversion utilities."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from engine.core.timezone import local_to_monday, monday_to_local, now_local


FACTORY = "America/Denver"


def test_local_to_monday_8am_mdt_becomes_14_utc():
    """8 AM Denver in May (MDT, UTC-6) → 14:00 UTC."""
    local = datetime(2026, 5, 21, 8, 0, 0)  # naive — assumed factory-local
    payload = local_to_monday(local, FACTORY)
    assert payload == {"date": "2026-05-21", "time": "14:00:00"}


def test_local_to_monday_handles_aware_input():
    """Aware datetime in factory TZ — should produce same payload."""
    local = datetime(2026, 5, 21, 8, 0, 0, tzinfo=ZoneInfo(FACTORY))
    payload = local_to_monday(local, FACTORY)
    assert payload == {"date": "2026-05-21", "time": "14:00:00"}


def test_local_to_monday_handles_aware_input_from_different_tz():
    """Aware datetime in a different TZ — should convert correctly."""
    east = datetime(2026, 5, 21, 10, 0, 0, tzinfo=ZoneInfo("America/New_York"))  # 10 AM EDT = 14:00 UTC
    payload = local_to_monday(east, FACTORY)
    assert payload == {"date": "2026-05-21", "time": "14:00:00"}


def test_monday_to_local_14_utc_becomes_8am_mdt():
    """Round-trip: Monday's 14:00 UTC → 8 AM in factory TZ."""
    parsed = monday_to_local({"date": "2026-05-21", "time": "14:00:00"}, FACTORY)
    assert parsed is not None
    assert parsed.year == 2026
    assert parsed.month == 5
    assert parsed.day == 21
    assert parsed.hour == 8
    assert parsed.minute == 0


def test_monday_to_local_handles_hh_mm_format():
    """Monday sometimes returns time as HH:MM instead of HH:MM:SS."""
    parsed = monday_to_local({"date": "2026-05-21", "time": "14:00"}, FACTORY)
    assert parsed is not None
    assert parsed.hour == 8


def test_monday_to_local_empty_returns_none():
    assert monday_to_local(None, FACTORY) is None
    assert monday_to_local({}, FACTORY) is None
    assert monday_to_local({"date": ""}, FACTORY) is None


def test_round_trip_preserves_time():
    """Local → Monday → Local should be a no-op."""
    original = datetime(2026, 5, 21, 13, 30, 0)
    payload = local_to_monday(original, FACTORY)
    roundtripped = monday_to_local(payload, FACTORY)
    assert roundtripped is not None
    assert roundtripped.year == original.year
    assert roundtripped.month == original.month
    assert roundtripped.day == original.day
    assert roundtripped.hour == original.hour
    assert roundtripped.minute == original.minute


def test_dst_winter_offset():
    """November 30 — MST (UTC-7). 8 AM MST → 15:00 UTC."""
    local = datetime(2026, 11, 30, 8, 0, 0)
    payload = local_to_monday(local, FACTORY)
    assert payload == {"date": "2026-11-30", "time": "15:00:00"}


def test_now_local_returns_aware_datetime():
    n = now_local(FACTORY)
    assert n.tzinfo is not None
    assert str(n.tzinfo) == FACTORY
