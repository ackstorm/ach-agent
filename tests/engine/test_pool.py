"""Pool tests: ENG-08 shared-server pool behavior.

Tests owned by this plan (00-03): implements the stubs marked in 00-01a.

Per-Task Verification Map (00-VALIDATION.md):
  ENG-08: test_pool_reuse         — implemented by 00-03
  ENG-08: test_pool_ttl_expires   — implemented by 00-03
  ENG-08: test_pool_ttl0_stops_immediately — implemented by 00-03
  ENG-08: test_pool_reacquire_cancels_ttl — implemented by 00-03
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fake ManagedServer helper
# ---------------------------------------------------------------------------


def _make_fake_server(alive: bool = True) -> MagicMock:
    """Return a fake ManagedServer with controllable is_alive() and a recordable stop()."""
    server = MagicMock()
    server.is_alive.return_value = alive
    server.stop = AsyncMock()
    return server


# ---------------------------------------------------------------------------
# ENG-08: Shared mode — second acquire reuses alive server
# ---------------------------------------------------------------------------


async def test_pool_reuse() -> None:
    """ENG-08: EnginePool.acquire() reuses the alive server on second call.

    Verifies that the ref-count increments and the same ManagedServer object
    is returned without starting a new subprocess.
    """
    from ach_agent.engine.pool import EnginePool
    from ach_agent.engine.lifecycle import EngineConfig

    config = EngineConfig()
    pool = EnginePool()

    fake_server = _make_fake_server(alive=True)
    start_call_count = 0

    async def fake_start_server(cfg: EngineConfig) -> MagicMock:
        nonlocal start_call_count
        start_call_count += 1
        return fake_server

    pool._start_server = fake_start_server

    # First acquire — creates a new server
    s1 = await pool.acquire(config)
    assert start_call_count == 1, "First acquire must start a new server"
    assert s1 is fake_server

    # Second acquire — server is alive, must reuse it (no new subprocess)
    s2 = await pool.acquire(config)
    assert start_call_count == 1, "Second acquire must NOT start a new server (reuse)"
    assert s2 is fake_server, "Second acquire must return the same ManagedServer"

    # ref_count must be 2 after two acquires
    assert pool._ref_count == 2, f"Expected ref_count=2, got {pool._ref_count}"


# ---------------------------------------------------------------------------
# ENG-08: TTL=0 stops server immediately on release
# ---------------------------------------------------------------------------


async def test_pool_ttl0_stops_immediately() -> None:
    """ENG-08: release(ttl_seconds=0) stops the server immediately (spawn-per-invocation)."""
    from ach_agent.engine.pool import EnginePool
    from ach_agent.engine.lifecycle import EngineConfig

    config = EngineConfig()
    pool = EnginePool()

    fake_server = _make_fake_server(alive=True)

    async def fake_start_server(cfg: EngineConfig) -> MagicMock:
        return fake_server

    pool._start_server = fake_start_server

    await pool.acquire(config)
    assert pool._ref_count == 1

    await pool.release(ttl_seconds=0)

    # Server must be stopped immediately
    fake_server.stop.assert_awaited_once()
    # Pool must no longer hold the server
    assert pool._server is None
    assert pool._ref_count == 0


# ---------------------------------------------------------------------------
# ENG-08: Shared mode — TTL triggers stop after idle
# ---------------------------------------------------------------------------


async def test_pool_ttl_expires() -> None:
    """ENG-08: EnginePool.release() with ttl_seconds>0 stops server after TTL elapses."""
    from ach_agent.engine.pool import EnginePool
    from ach_agent.engine.lifecycle import EngineConfig

    config = EngineConfig()
    pool = EnginePool()

    fake_server = _make_fake_server(alive=True)

    async def fake_start_server(cfg: EngineConfig) -> MagicMock:
        return fake_server

    pool._start_server = fake_start_server

    await pool.acquire(config)
    assert pool._ref_count == 1

    # Release with a tiny TTL (0.05s) — server should NOT be stopped immediately
    await pool.release(ttl_seconds=0.05)
    assert fake_server.stop.call_count == 0, "Server must not be stopped before TTL elapses"
    assert pool._server is not None, "Pool must still hold the server before TTL"

    # Wait for TTL to elapse
    await asyncio.sleep(0.15)

    # Server must now be stopped and pool cleared
    fake_server.stop.assert_awaited_once()
    assert pool._server is None, "Pool must clear server after TTL expiry"


# ---------------------------------------------------------------------------
# ENG-08: Re-acquire before TTL elapses cancels the pending TTL task
# ---------------------------------------------------------------------------


async def test_pool_reacquire_cancels_ttl() -> None:
    """ENG-08: A new acquire before the TTL fires cancels the pending expiry task."""
    from ach_agent.engine.pool import EnginePool
    from ach_agent.engine.lifecycle import EngineConfig

    config = EngineConfig()
    pool = EnginePool()

    fake_server = _make_fake_server(alive=True)

    async def fake_start_server(cfg: EngineConfig) -> MagicMock:
        return fake_server

    pool._start_server = fake_start_server

    await pool.acquire(config)
    await pool.release(ttl_seconds=0.1)  # TTL not yet elapsed

    # Re-acquire while TTL is pending — must cancel the TTL and reuse the server
    s = await pool.acquire(config)
    assert s is fake_server, "Re-acquire must reuse the server still in TTL window"

    # Wait past the original TTL deadline
    await asyncio.sleep(0.2)

    # Server must NOT have been stopped (TTL was cancelled)
    assert fake_server.stop.call_count == 0, (
        "Server must not be stopped — TTL was cancelled by re-acquire"
    )
    assert pool._server is not None, "Pool must still hold the server after re-acquire"
    assert pool._ref_count == 1


# ---------------------------------------------------------------------------
# DUR-02: engine_has_been_ready_once latch (Plan 03-02)
# ---------------------------------------------------------------------------


async def test_ready_once_latch() -> None:
    """DUR-02: engine_has_been_ready_once starts False, set True after first acquire, never resets.

    D-07: first-warmup-only gate — latch never re-gated after ready-once.
    D-08-latch: set on first successful poll_ready return (via acquire).
    """
    from ach_agent.engine.pool import EnginePool
    from ach_agent.engine.lifecycle import EngineConfig

    config = EngineConfig()
    pool = EnginePool()

    # Latch must start as False (DUR-02)
    assert pool.engine_has_been_ready_once is False, (
        "DUR-02: engine_has_been_ready_once must start False"
    )

    fake_server = _make_fake_server(alive=True)

    async def fake_start_server(cfg: EngineConfig) -> MagicMock:
        return fake_server

    pool._start_server = fake_start_server

    # First acquire — latch must flip to True after server starts
    await pool.acquire(config)

    assert pool.engine_has_been_ready_once is True, (
        "DUR-02: engine_has_been_ready_once must be True after first acquire"
    )

    # Second acquire — latch must remain True (D-07: never reset)
    await pool.acquire(config)
    assert pool.engine_has_been_ready_once is True, (
        "DUR-02: latch must remain True after second acquire (D-07: first-warmup-only)"
    )
