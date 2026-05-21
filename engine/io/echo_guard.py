"""Echo guard — prevents the engine from reacting to its own writes.

When the engine applies a Plan, it stamps every Schedule item with a
`last_reflow_hash` (UUID). Monday then fires `Schedule item modified`
webhooks back to us — one per column changed. Without a guard, those
webhooks would trigger more reflows, more writes, more webhooks → loop.

The guard maintains a small recent-hashes set. On webhook receipt, the
caller reads the affected Schedule item's `last_reflow_hash` column. If
that hash is in the guard's set, the webhook is an engine echo and is
ignored. If it's empty or unknown, the change was operator-driven and
deserves processing.

Capacity: 256 hashes by default. Each apply_plan stamps one hash that
covers all its writes (often 1-5 items), so this holds roughly the last
~250 reflows. At Phase 1 scale (tens of reflows per day) this is days
of headroom.
"""

from __future__ import annotations

from collections import OrderedDict
from threading import Lock


class EchoGuard:
    """In-memory LRU-style set of recently-applied reflow hashes.

    Thread-safe via internal lock. Module-level singleton via `get_echo_guard()`.
    """

    def __init__(self, capacity: int = 256):
        self._cache: OrderedDict[str, None] = OrderedDict()
        self._capacity = capacity
        self._lock = Lock()

    def remember(self, reflow_hash: str) -> None:
        """Record a hash the engine just wrote.

        No-op for empty strings (defensive — apply_plan generates a fresh
        UUID per call, but unit tests sometimes pass empty hashes).
        """
        if not reflow_hash:
            return
        with self._lock:
            if reflow_hash in self._cache:
                self._cache.move_to_end(reflow_hash)
                return
            self._cache[reflow_hash] = None
            if len(self._cache) > self._capacity:
                self._cache.popitem(last=False)

    def is_engine_origin(self, reflow_hash: str | None) -> bool:
        """True if the hash matches a recently-applied write (= our own echo)."""
        if not reflow_hash:
            return False
        with self._lock:
            return reflow_hash in self._cache

    def __len__(self) -> int:
        return len(self._cache)

    def clear(self) -> None:
        """Test helper — wipe the cache."""
        with self._lock:
            self._cache.clear()


_singleton: EchoGuard | None = None


def get_echo_guard() -> EchoGuard:
    """Module-level singleton instance."""
    global _singleton
    if _singleton is None:
        _singleton = EchoGuard()
    return _singleton
