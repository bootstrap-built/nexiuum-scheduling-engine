"""Unit tests for EchoGuard — LRU-style hash set."""

from __future__ import annotations

from engine.io.echo_guard import EchoGuard


def test_empty_guard_recognizes_nothing():
    g = EchoGuard()
    assert g.is_engine_origin("anything") is False
    assert g.is_engine_origin(None) is False
    assert g.is_engine_origin("") is False


def test_remembered_hash_is_recognized():
    g = EchoGuard()
    g.remember("abc123")
    assert g.is_engine_origin("abc123") is True
    assert g.is_engine_origin("other") is False


def test_remember_ignores_empty_input():
    g = EchoGuard()
    g.remember("")
    g.remember(None)  # type: ignore[arg-type]
    assert len(g) == 0


def test_remember_dedupes_repeated_hash():
    g = EchoGuard()
    g.remember("h1")
    g.remember("h1")
    g.remember("h1")
    assert len(g) == 1


def test_eviction_when_over_capacity():
    g = EchoGuard(capacity=3)
    g.remember("h1")
    g.remember("h2")
    g.remember("h3")
    g.remember("h4")  # should evict h1
    assert g.is_engine_origin("h1") is False
    assert g.is_engine_origin("h2") is True
    assert g.is_engine_origin("h3") is True
    assert g.is_engine_origin("h4") is True


def test_remember_existing_hash_moves_it_to_recent():
    """Re-remembering an existing hash should not evict it later."""
    g = EchoGuard(capacity=3)
    g.remember("h1")
    g.remember("h2")
    g.remember("h3")
    g.remember("h1")  # bumps h1 to most-recent
    g.remember("h4")  # evicts h2 (oldest), not h1
    assert g.is_engine_origin("h1") is True
    assert g.is_engine_origin("h2") is False


def test_clear_wipes_cache():
    g = EchoGuard()
    g.remember("h1")
    g.clear()
    assert len(g) == 0
    assert g.is_engine_origin("h1") is False
