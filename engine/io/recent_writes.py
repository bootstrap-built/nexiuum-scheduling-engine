"""Write-origin echo registry — fixes Codex E4 Blocker #1 (B1).

The engine shares a Monday user with real humans: it authenticates as
Josh's Gray Space account (101255258) until a dedicated service account
exists, and the same token drives CLI/agent test writes. So "event.userId
== engine user" is NOT a valid discriminator for "did the engine cause
this webhook?" — the user-id guard it replaces suppressed legitimate
operator and API changes wholesale (live failure 2026-06-10: a Blend
Status → Blending flip was eaten as an "echo" on a board the engine never
writes, killing the ADR-0004 press-scheduling trigger).

This registry suppresses by ACTUAL write origin instead, per the Codex
prescription (item + column + short TTL): `apply_plan` records every
Monday write the engine performs — (board, pulse, column ids) for updates,
(board, pulse) for creations — and the webhook route drops an inbound
event only when it matches a recorded write inside the TTL window. No
recorded write → the event is operator-originated and is processed, no
matter which user made it. Boards the engine never writes (Blend Records,
Production Schedule) can therefore never be suppressed.

Fail-open bias: a column-scoped entry does not suppress an event that
carries no columnId; an expired or missing entry never suppresses. The
worst case of failing open is one redundant reflow; the worst case of
failing closed is a silently dropped operator change.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

# Hard cap on tracked entries — a runaway recorder degrades to forgetting
# oldest writes (fail-open) rather than growing without bound.
_MAX_ENTRIES = 2048


@dataclass
class _Entry:
    expires_at: float
    # None = creation (any event on the pulse is an echo while fresh).
    # Otherwise the set of column ids the engine wrote.
    column_ids: set[str] | None


_lock = threading.Lock()
_entries: dict[tuple[str, str], _Entry] = {}


def _key(board_id: object, pulse_id: object) -> tuple[str, str]:
    return (str(board_id), str(pulse_id))


def _prune_locked(now: float) -> None:
    expired = [k for k, e in _entries.items() if e.expires_at <= now]
    for k in expired:
        del _entries[k]
    if len(_entries) > _MAX_ENTRIES:
        # Drop oldest-expiring first.
        for k in sorted(_entries, key=lambda k: _entries[k].expires_at)[
            : len(_entries) - _MAX_ENTRIES
        ]:
            del _entries[k]


def record_write(
    board_id: object,
    pulse_id: object,
    column_ids: set[str] | None,
    *,
    ttl_seconds: float,
    now: float | None = None,
) -> None:
    """Record an engine write. `column_ids=None` marks an item creation.

    Repeated records for the same (board, pulse) merge: column sets union,
    a creation record subsumes column scoping, and the deadline extends.
    """
    t = time.monotonic() if now is None else now
    key = _key(board_id, pulse_id)
    with _lock:
        _prune_locked(t)
        existing = _entries.get(key)
        if existing is not None and existing.expires_at > t:
            if existing.column_ids is None or column_ids is None:
                merged: set[str] | None = None
            else:
                merged = existing.column_ids | column_ids
            _entries[key] = _Entry(
                expires_at=max(existing.expires_at, t + ttl_seconds),
                column_ids=merged,
            )
        else:
            _entries[key] = _Entry(
                expires_at=t + ttl_seconds,
                column_ids=set(column_ids) if column_ids is not None else None,
            )


def is_engine_echo(
    board_id: object,
    pulse_id: object,
    column_id: str | None,
    *,
    now: float | None = None,
) -> bool:
    """True only when the event matches a fresh recorded engine write.

    Creation entries match any event on the pulse. Column-scoped entries
    match only events carrying one of the recorded column ids — an event
    without a columnId does NOT match a column-scoped entry (fail open).
    """
    t = time.monotonic() if now is None else now
    key = _key(board_id, pulse_id)
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry.expires_at <= t:
            return False
        if entry.column_ids is None:
            return True
        return column_id is not None and column_id in entry.column_ids


def reset() -> None:
    """Test hook — clear all recorded writes."""
    with _lock:
        _entries.clear()
