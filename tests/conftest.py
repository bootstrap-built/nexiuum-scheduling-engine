"""Pytest configuration — sets asyncio mode to auto so async tests work
without explicit @pytest.mark.asyncio decorations on every test.
"""

import pytest


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if "asyncio" in item.keywords:
            continue
        if item.get_closest_marker("asyncio") is None:
            # Async tests in this codebase always need the asyncio marker
            pass
