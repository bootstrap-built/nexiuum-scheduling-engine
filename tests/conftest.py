"""Pytest configuration.

Sets a sentinel MONDAY_GRAYSPACE_TOKEN before any test module imports
engine.config (which validates env at first instantiation). Tests that
need a REAL token are gated explicitly via PLACEHOLDER_TOKEN check —
they skip when only the sentinel is set, so integration tests don't
attempt to authenticate with garbage credentials.

The sentinel is set via `setdefault` so a real token sourced from the
shell (via ~/.monday_tokens) wins.
"""

from __future__ import annotations

import os

PLACEHOLDER_TOKEN = "__test_placeholder_no_real_calls__"
os.environ.setdefault("MONDAY_GRAYSPACE_TOKEN", PLACEHOLDER_TOKEN)


def has_real_monday_token() -> bool:
    token = os.environ.get("MONDAY_GRAYSPACE_TOKEN", "")
    return bool(token) and token != PLACEHOLDER_TOKEN
