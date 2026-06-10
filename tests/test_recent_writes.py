"""engine/io/recent_writes — write-origin echo registry (Codex E4 B1).

The registry must suppress ONLY events matching a recorded engine write
inside the TTL, and must never suppress events on boards/items the engine
didn't touch — that's the whole fix over the user-id guard, which ate any
event from the shared engine user (live failure 2026-06-10: an operator
Blend Status flip suppressed as an "echo").
"""
from __future__ import annotations

import pytest

from engine.io import recent_writes


@pytest.fixture(autouse=True)
def _clean_registry():
    recent_writes.reset()
    yield
    recent_writes.reset()


def test_no_record_never_suppresses():
    """The B1 regression: an event the engine did not cause passes, no
    matter who triggered it."""
    assert not recent_writes.is_engine_echo(18404836849, "123", "color_mm1mb9cm")


def test_column_scoped_record_suppresses_matching_column():
    recent_writes.record_write(111, "42", {"status", "date4"}, ttl_seconds=60, now=100.0)
    assert recent_writes.is_engine_echo(111, "42", "status", now=101.0)
    assert recent_writes.is_engine_echo(111, "42", "date4", now=101.0)


def test_column_scoped_record_does_not_suppress_other_columns():
    recent_writes.record_write(111, "42", {"status"}, ttl_seconds=60, now=100.0)
    assert not recent_writes.is_engine_echo(111, "42", "priority", now=101.0)


def test_column_scoped_record_fails_open_without_column_id():
    """An event with no columnId does NOT match a column-scoped entry —
    failing open costs a redundant reflow; failing closed eats an
    operator change."""
    recent_writes.record_write(111, "42", {"status"}, ttl_seconds=60, now=100.0)
    assert not recent_writes.is_engine_echo(111, "42", None, now=101.0)


def test_creation_record_suppresses_any_event_on_pulse():
    recent_writes.record_write(111, "42", None, ttl_seconds=60, now=100.0)
    assert recent_writes.is_engine_echo(111, "42", None, now=101.0)
    assert recent_writes.is_engine_echo(111, "42", "anything", now=101.0)


def test_ttl_expiry():
    recent_writes.record_write(111, "42", {"status"}, ttl_seconds=60, now=100.0)
    assert recent_writes.is_engine_echo(111, "42", "status", now=159.9)
    assert not recent_writes.is_engine_echo(111, "42", "status", now=160.1)


def test_different_board_or_pulse_not_suppressed():
    recent_writes.record_write(111, "42", None, ttl_seconds=60, now=100.0)
    assert not recent_writes.is_engine_echo(222, "42", None, now=101.0)
    assert not recent_writes.is_engine_echo(111, "43", None, now=101.0)


def test_records_merge_columns_and_extend_deadline():
    recent_writes.record_write(111, "42", {"status"}, ttl_seconds=60, now=100.0)
    recent_writes.record_write(111, "42", {"date4"}, ttl_seconds=60, now=130.0)
    # Both columns now suppress, and the deadline follows the later write.
    assert recent_writes.is_engine_echo(111, "42", "status", now=170.0)
    assert recent_writes.is_engine_echo(111, "42", "date4", now=170.0)
    assert not recent_writes.is_engine_echo(111, "42", "status", now=190.1)


def test_creation_record_subsumes_column_scope_on_merge():
    recent_writes.record_write(111, "42", {"status"}, ttl_seconds=60, now=100.0)
    recent_writes.record_write(111, "42", None, ttl_seconds=60, now=101.0)
    assert recent_writes.is_engine_echo(111, "42", "anything", now=102.0)


def test_board_and_pulse_ids_compare_as_strings():
    """Monday payloads mix int and str ids — the registry must not care."""
    recent_writes.record_write("111", 42, {"status"}, ttl_seconds=60, now=100.0)
    assert recent_writes.is_engine_echo(111, "42", "status", now=101.0)


def test_entry_cap_drops_oldest_first():
    for i in range(recent_writes._MAX_ENTRIES + 10):
        recent_writes.record_write(1, str(i), None, ttl_seconds=10_000 + i, now=100.0)
    # Oldest-expiring entries (lowest i) were evicted; newest survive.
    assert not recent_writes.is_engine_echo(1, "0", None, now=101.0)
    assert recent_writes.is_engine_echo(
        1, str(recent_writes._MAX_ENTRIES + 9), None, now=101.0
    )
