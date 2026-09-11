# SPDX-License-Identifier: Apache-2.0
"""Lane timeout ownership (B2, B3).

The lane owns the single authoritative maxInvocationSeconds bound (RTR-04). When it
fires it must:
  - increment ENGINE_WATCHDOG_KILLS (metric moved off run_invocation), and
  - (via the cancelled engine_runner) always finish the ID-keyed completion with a
    timeout error and force-kill the runaway server (release ttl=0, never the warm TTL).
"""
from __future__ import annotations

import asyncio
import weakref

from ach_agent.router.lane import Lane
from tests.router.conftest import make_event


class _FakeRouter:
    """Minimal router stand-in for direct Lane construction."""

    def on_lane_idle(self, session_key: str) -> None:
        pass

    def release_queued_slot(self) -> None:
        pass


def _make_lane(engine_runner, max_invocation_seconds: float, router: _FakeRouter) -> Lane:
    pool_sem = asyncio.Semaphore(4)
    channel_sem = asyncio.Semaphore(4)
    return Lane(
        session_key="k",
        router_ref=weakref.ref(router),
        invocation_semaphores=lambda _channel_name: (pool_sem, channel_sem),
        engine_runner=engine_runner,
        max_invocation_seconds=max_invocation_seconds,
    )


async def test_lane_timeout_increments_watchdog_metric() -> None:
    """The lane's own deadline increments ENGINE_WATCHDOG_KILLS (B2)."""
    from ach_agent.engine.metrics import ENGINE_WATCHDOG_KILLS

    router = _FakeRouter()

    async def slow_runner(event, on_kill) -> None:  # noqa: ANN001
        await asyncio.sleep(5)

    lane = _make_lane(slow_runner, 0.05, router)
    before = ENGINE_WATCHDOG_KILLS._value.get()
    await lane.put(make_event())

    # Let the lane hit its 0.05s deadline and run the except TimeoutError branch.
    deadline = asyncio.get_event_loop().time() + 2.0
    while ENGINE_WATCHDOG_KILLS._value.get() - before < 1.0:
        if asyncio.get_event_loop().time() > deadline:
            break
        await asyncio.sleep(0.02)

    after = ENGINE_WATCHDOG_KILLS._value.get()
    lane.cancel()
    await lane.wait_closed()
    assert after - before == 1.0, f"expected watchdog +1 at lane, got {after - before}"


async def _build_runner(fake_pool, channel_ttl: dict[str, float]):
    return await _build_runner_with_registry(fake_pool, channel_ttl, None)


async def _build_runner_with_registry(fake_pool, channel_ttl, registry):
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver

    return make_engine_runner(
        pool=fake_pool,
        driver=OpencodeDriver(),
        engine_cfg=EngineConfig(),
        max_invocation_seconds=1,
        channel_ttl=channel_ttl,
        channels_by_name={},
        memory_cfg=None,
        completion_registry=registry,
    )


async def test_completion_resolved_on_timeout() -> None:
    """A lane timeout finishes the ID-keyed completion (B3, no hang)."""
    from unittest.mock import AsyncMock, MagicMock

    from ach_agent.boot.completions import CompletionRegistry
    from ach_agent.router.router import RouterAdmitResult

    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value=MagicMock())
    fake_pool.release = AsyncMock()
    router = _FakeRouter()

    async def slow_run_contract_turn(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(5)
        return {"action": "none", "text": ""}

    from unittest.mock import patch

    registry = CompletionRegistry(lambda _event: _accepted())

    async def _accepted():
        return RouterAdmitResult.ACCEPTED

    with patch("ach_agent.engine.base.terminal.run_contract_turn", slow_run_contract_turn):
        runner = await _build_runner_with_registry(fake_pool, {"test-channel": 60.0}, registry)
        lane = _make_lane(runner, 0.05, router)
        event = make_event(channel_name="test-channel")
        submission = await registry.submit(event)
        await lane.put(event)
        assert submission.completion is not None
        completion = await asyncio.wait_for(registry.wait(submission.completion.ref), 1.0)
        assert completion.state == "failed"
        assert "timed out" in (completion.error or "")

    lane.cancel()
    await lane.wait_closed()


async def test_timeout_force_kills_regardless_of_ttl() -> None:
    """On a lane timeout the pooled server is released with ttl=0, not the channel warm TTL (B3)."""
    from unittest.mock import AsyncMock, MagicMock, patch

    recorded: list[float] = []
    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(return_value=MagicMock())

    async def record_release(session_key: str, ttl_seconds: float) -> None:
        recorded.append(ttl_seconds)

    fake_pool.release = record_release
    router = _FakeRouter()

    async def slow_run_contract_turn(*args: object, **kwargs: object) -> dict[str, object]:
        await asyncio.sleep(5)
        return {"action": "none", "text": ""}

    with patch("ach_agent.engine.base.terminal.run_contract_turn", slow_run_contract_turn):
        runner = await _build_runner(fake_pool, {"test-channel": 60.0})
        lane = _make_lane(runner, 0.05, router)
        await lane.put(make_event(channel_name="test-channel"))

        deadline = asyncio.get_event_loop().time() + 2.0
        while not recorded:
            if asyncio.get_event_loop().time() > deadline:
                break
            await asyncio.sleep(0.02)

    lane.cancel()
    await lane.wait_closed()
    assert recorded == [0.0], f"timeout release must force-kill (ttl=0), got {recorded}"


async def test_engine_launch_failure_increments_metric_and_finishes_completion() -> None:
    """pool.acquire raising is an explicit launch failure (Step 5, decoupled acceptance):
    ENGINE_LAUNCH_FAILURES.inc() + WARN, and the awaiting reply_future receives the
    completion — no hang, no silent drop. server stays None, so release is never called.
    """
    from unittest.mock import AsyncMock, MagicMock

    from ach_agent.boot.completions import CompletionRegistry
    from ach_agent.engine.metrics import ENGINE_LAUNCH_FAILURES
    from ach_agent.router.router import RouterAdmitResult

    class _LaunchError(RuntimeError):
        pass

    fake_pool = MagicMock()
    fake_pool.acquire = AsyncMock(side_effect=_LaunchError("opencode failed to start"))
    fake_pool.release = AsyncMock()
    router = _FakeRouter()

    before = ENGINE_LAUNCH_FAILURES._value.get()

    async def _accepted(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(_accepted)
    runner = await _build_runner_with_registry(fake_pool, {"test-channel": 60.0}, registry)
    lane = _make_lane(runner, 5.0, router)
    event = make_event(channel_name="test-channel")
    submission = await registry.submit(event)
    await lane.put(event)
    assert submission.completion is not None
    completion = await asyncio.wait_for(registry.wait(submission.completion.ref), 1.0)
    assert completion.state == "failed"
    assert "opencode failed" in (completion.error or "")

    after = ENGINE_LAUNCH_FAILURES._value.get()
    lane.cancel()
    await lane.wait_closed()

    assert after - before == 1.0, f"expected ENGINE_LAUNCH_FAILURES +1, got {after - before}"
    fake_pool.release.assert_not_called()
