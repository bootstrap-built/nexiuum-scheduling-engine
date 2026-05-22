"""Pytest configuration.

Sets sentinel env vars before any test module imports engine.config
(which validates env at first instantiation). Tests that need REAL
credentials are gated via the helpers — they skip when only sentinels
are set, so integration tests don't attempt to authenticate with
garbage.

Sentinels are set via `setdefault` so a real value sourced from the
shell wins (e.g. via ~/.monday_tokens).
"""

from __future__ import annotations

import os

PLACEHOLDER_TOKEN = "__test_placeholder_no_real_calls__"
TEST_WEBHOOK_SECRET = "test-webhook-secret-deadbeef"
TEST_ENGINE_USER_ID = "test-engine-user-123"

os.environ.setdefault("MONDAY_GRAYSPACE_TOKEN", PLACEHOLDER_TOKEN)
os.environ.setdefault("MONDAY_WEBHOOK_SECRET", TEST_WEBHOOK_SECRET)
# Pre-set the engine user id so webhook tests don't need to mock a
# Monday `{ me { id } }` round-trip for echo filtering.
os.environ.setdefault("ENGINE_MONDAY_USER_ID", TEST_ENGINE_USER_ID)


def has_real_monday_token() -> bool:
    token = os.environ.get("MONDAY_GRAYSPACE_TOKEN", "")
    return bool(token) and token != PLACEHOLDER_TOKEN


# ─── Autouse: reset module-level singletons between tests ────────────────
# The worker queue is asyncio-loop-bound, and pytest-asyncio creates a
# fresh loop per test by default. Without this, a queue created in test
# A's loop would be reused by test B running in a different loop and
# fail with "Queue is bound to a different event loop".

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_engine_singletons():
    """Clear module-level state before and after every test."""
    from engine.io import engine_identity
    from engine.io.worker import reset_state_for_tests

    reset_state_for_tests()
    engine_identity.reset_engine_user_id()
    yield
    reset_state_for_tests()
    engine_identity.reset_engine_user_id()
