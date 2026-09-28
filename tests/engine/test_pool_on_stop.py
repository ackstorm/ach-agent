# SPDX-License-Identifier: Apache-2.0
"""EnginePool(on_stop=...) — hooks.sessionSuspend fires before the native stop."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.base.pool import EnginePool


def _make_fake_server(alive: bool = True):
    srv = MagicMock()
    srv.is_alive.return_value = alive
    srv.stop = AsyncMock()
    return srv


def _real_config() -> EngineConfig:
    return EngineConfig(model_base_url="http://127.0.0.1:9/v1", engine_type="opencode")


async def test_on_stop_fires_before_native_stop_on_expire() -> None:
    calls: list[str] = []
    fake = _make_fake_server()

    async def on_stop(session_key: str) -> None:
        calls.append(session_key)
        assert not fake.stop.await_args_list, "on_stop must run before the native stop"

    pool = EnginePool(on_stop=on_stop)

    async def fake_start(cfg, session_key: str):
        return fake

    pool._start_server = fake_start
    await pool.acquire("k1", _real_config())
    await pool.release("k1", ttl_seconds=0.01)
    await pool._ttl_tasks["k1"]

    assert calls == ["k1"]
    fake.stop.assert_awaited_once()


async def test_on_stop_fires_before_native_stop_on_discard() -> None:
    calls: list[str] = []
    fake = _make_fake_server()

    async def on_stop(session_key: str) -> None:
        calls.append(session_key)
        assert not fake.stop.await_args_list

    pool = EnginePool(on_stop=on_stop)

    async def fake_start(cfg, session_key: str):
        return fake

    pool._start_server = fake_start
    await pool.acquire("k1", _real_config())
    await pool.discard("k1")

    assert calls == ["k1"]
    fake.stop.assert_awaited_once()


async def test_on_stop_fires_before_native_stop_on_stop_all() -> None:
    calls: list[str] = []
    servers = {"k1": _make_fake_server(), "k2": _make_fake_server()}
    seq = iter([servers["k1"], servers["k2"]])

    async def on_stop(session_key: str) -> None:
        calls.append(session_key)
        assert not servers[session_key].stop.await_args_list

    pool = EnginePool(on_stop=on_stop)

    async def fake_start(cfg, session_key: str):
        return next(seq)

    pool._start_server = fake_start
    await pool.acquire("k1", _real_config())
    await pool.acquire("k2", _real_config())
    await pool.release("k1", ttl_seconds=0)
    await pool.release("k2", ttl_seconds=0)

    assert sorted(calls) == ["k1", "k2"]
    servers["k1"].stop.assert_awaited_once()
    servers["k2"].stop.assert_awaited_once()


async def test_on_stop_not_called_for_a_key_with_no_server() -> None:
    calls: list[str] = []

    async def on_stop(session_key: str) -> None:
        calls.append(session_key)

    pool = EnginePool(on_stop=on_stop)
    await pool.discard("never-acquired")

    assert calls == []


async def test_raising_on_stop_does_not_break_the_native_stop() -> None:
    fake = _make_fake_server()

    async def on_stop(session_key: str) -> None:
        raise RuntimeError("sessionSuspend boom")

    pool = EnginePool(on_stop=on_stop)

    async def fake_start(cfg, session_key: str):
        return fake

    pool._start_server = fake_start
    await pool.acquire("k1", _real_config())
    await pool.discard("k1")

    fake.stop.assert_awaited_once()
    assert "k1" not in pool._servers


async def test_raising_on_stop_is_not_fatal_under_strict_cleanup() -> None:
    """strict_cleanup governs the native stop/cleanup callback, not on_stop's own errors —
    on_stop is best-effort by contract (hooks.sessionSuspend), regardless of that flag."""
    fake = _make_fake_server()

    async def on_stop(session_key: str) -> None:
        raise RuntimeError("sessionSuspend boom")

    pool = EnginePool(on_stop=on_stop, strict_cleanup=True)

    async def fake_start(cfg, session_key: str):
        return fake

    pool._start_server = fake_start
    await pool.acquire("k1", _real_config())
    await pool.discard("k1")

    fake.stop.assert_awaited_once()
