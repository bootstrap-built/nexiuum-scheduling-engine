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
async def _reset_engine_singletons():
    """Clear module-level state before and after every test.

    Async so that any background tasks the test started (worker, sweep)
    can be cleanly cancelled with `await stop_*` before the singleton
    handle is dropped. A sync fixture that only nulled the handles would
    leave the underlying asyncio.Task running until loop close, producing
    pending-task warnings and cross-test interference.
    """
    from engine.config import reset_settings_for_tests as reset_settings
    from engine.io import engine_identity, recent_writes
    from engine.io.sweep import reset_state_for_tests as reset_sweep
    from engine.io.sweep import stop_sweep
    from engine.io.worker import reset_state_for_tests as reset_worker
    from engine.io.worker import stop_worker

    # Pre-test: tear down any leftover state from a previous test that
    # exited abnormally, then null the handles.
    await stop_sweep()
    await stop_worker()
    reset_sweep()
    reset_worker()
    reset_settings()
    engine_identity.reset_engine_user_id()
    recent_writes.reset()

    yield

    # Post-test: same dance. Cancel tasks first so we don't drop a live
    # task reference, then wipe the singletons for the next test's loop.
    await stop_sweep()
    await stop_worker()
    reset_sweep()
    reset_worker()
    reset_settings()
    engine_identity.reset_engine_user_id()
    recent_writes.reset()
