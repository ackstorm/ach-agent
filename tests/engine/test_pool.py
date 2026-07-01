# SPDX-License-Identifier: Apache-2.0
"""Tests for the keyed EnginePool (ENG-08 + keyed migration).

Each session_key maps to its own ManagedServer. acquire/release take a
session_key; releasing one key never affects another.
"""

from __future__ import annotations

import asyncio

import pytest

from ach_agent.engine.pool import EnginePool


def _make_fake_server(alive: bool = True):
    from unittest.mock import AsyncMock, MagicMock

    srv = MagicMock()
    srv.is_alive.return_value = alive
    srv.stop = AsyncMock()
    return srv


def _config():
    from unittest.mock import MagicMock

    return MagicMock(name="EngineConfig")


async def test_pool_reuse_same_key() -> None:
    """Second acquire with the same key reuses the alive server (no new start)."""
    pool = EnginePool()
    calls = {"n": 0}
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        calls["n"] += 1
        return fake

    pool._start_server = fake_start

    s1 = await pool.acquire("k1", _config())
    s2 = await pool.acquire("k1", _config())
    assert calls["n"] == 1, "Same key must reuse the server"
    assert s1 is fake and s2 is fake
    assert pool._ref_counts["k1"] == 2


async def test_pool_distinct_keys_get_distinct_servers() -> None:
    """Different keys start different servers and are tracked independently."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}

    async def fake_start(cfg):
        # index by call order via a mutable marker on the pool
        key = pool._pending_key  # set by test just before acquire
        return servers[key]

    pool._start_server = fake_start

    pool._pending_key = "k1"
    a = await pool.acquire("k1", _config())
    pool._pending_key = "k2"
    b = await pool.acquire("k2", _config())

    assert a is servers["k1"]
    assert b is servers["k2"]
    assert set(pool._servers.keys()) == {"k1", "k2"}


async def test_release_one_key_does_not_stop_another() -> None:
    """Stopping k1 (ttl=0) must leave k2's server alive and untracked-untouched."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}

    async def fake_start(cfg):
        return servers[pool._pending_key]

    pool._start_server = fake_start

    pool._pending_key = "k1"
    await pool.acquire("k1", _config())
    pool._pending_key = "k2"
    await pool.acquire("k2", _config())

    await pool.release("k1", ttl_seconds=0)

    servers["k1"].stop.assert_awaited_once()
    servers["k2"].stop.assert_not_awaited()
    assert "k1" not in pool._servers
    assert "k2" in pool._servers


async def test_ttl0_stops_immediately() -> None:
    """release(ttl=0) stops that key's server and removes it from the pool."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_ttl_expires_after_delay() -> None:
    """release(ttl>0) keeps the server until TTL elapses, then stops it."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.05)
    assert fake.stop.call_count == 0
    assert "k1" in pool._servers

    await asyncio.sleep(0.12)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_reacquire_cancels_pending_ttl() -> None:
    """A re-acquire before TTL fires cancels that key's expiry task."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0.05)
    await pool.acquire("k1", _config())  # cancels expiry
    await asyncio.sleep(0.12)
    fake.stop.assert_not_awaited()
    assert "k1" in pool._servers


async def test_ref_count_keeps_server_until_last_release() -> None:
    """Two acquires + one release keep the server; second release (ttl=0) stops it."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        return fake

    pool._start_server = fake_start

    await pool.acquire("k1", _config())
    await pool.acquire("k1", _config())
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_not_awaited()
    assert "k1" in pool._servers
    await pool.release("k1", ttl_seconds=0)
    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_dead_server_replaced_on_acquire() -> None:
    """If the tracked server is dead, acquire starts a fresh one for that key."""
    pool = EnginePool()
    dead = _make_fake_server(alive=False)
    live = _make_fake_server(alive=True)
    seq = [dead, live]

    async def fake_start(cfg):
        return seq.pop(0)

    pool._start_server = fake_start

    s1 = await pool.acquire("k1", _config())
    assert s1 is dead
    s2 = await pool.acquire("k1", _config())  # dead → replace
    assert s2 is live
    dead.stop.assert_awaited_once()


async def test_ready_latch_set_on_first_start() -> None:
    """engine_has_been_ready_once flips True on first successful start, stays True."""
    pool = EnginePool()
    fake = _make_fake_server(alive=True)

    async def fake_start(cfg):
        return fake

    pool._start_server = fake_start

    assert pool.engine_has_been_ready_once is False
    await pool.acquire("k1", _config())
    assert pool.engine_has_been_ready_once is True
    await pool.release("k1", ttl_seconds=0)
    assert pool.engine_has_been_ready_once is True


async def test_stop_all_stops_every_server() -> None:
    """stop_all() stops every live server and clears the pool."""
    pool = EnginePool()
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}

    async def fake_start(cfg):
        return servers[pool._pending_key]

    pool._start_server = fake_start

    pool._pending_key = "k1"
    await pool.acquire("k1", _config())
    pool._pending_key = "k2"
    await pool.acquire("k2", _config())

    await pool.stop_all()
    servers["k1"].stop.assert_awaited_once()
    servers["k2"].stop.assert_awaited_once()
    assert pool._servers == {}
