from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from ach_agent.boot.conversations import ConversationLocks
from tests.runner_client import RunnerClient


@pytest.mark.asyncio
async def test_waiters_are_reference_counted_and_evicted_after_cancellation() -> None:
    locks = ConversationLocks()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with locks.hold("opencode", "repo"):
            entered.set()
            await release.wait()

    first = asyncio.create_task(holder())
    await entered.wait()
    waiter = asyncio.create_task(_enter_and_set(locks, "opencode", "repo"))
    await asyncio.sleep(0)
    assert not waiter.done()
    assert len(locks) == 1

    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert len(locks) == 1

    release.set()
    await first
    assert len(locks) == 0


async def _enter_and_set(locks: ConversationLocks, engine: str, key: str) -> None:
    async with locks.hold(engine, key):
        raise AssertionError("cancelled waiter entered the lock")


@pytest.mark.asyncio
async def test_different_engine_types_do_not_share_conversation_lock() -> None:
    waiter_seen = asyncio.Event()

    class TrackingLocks(ConversationLocks):
        hold_count = 0

        def hold(self, engine_type, conversation_key):
            self.hold_count += 1
            if self.hold_count == 2:
                waiter_seen.set()
            return super().hold(engine_type, conversation_key)

    locks = TrackingLocks()
    entered = asyncio.Event()
    async with locks.hold("opencode", "repo"):

        async def other_engine() -> None:
            async with locks.hold("pi", "repo"):
                entered.set()

        task = asyncio.create_task(other_engine())
        await asyncio.wait_for(entered.wait(), timeout=1)
        await task


@pytest.mark.asyncio
async def test_lock_is_released_after_timeout_or_body_exception() -> None:
    locks = ConversationLocks()
    async with locks.hold("opencode", "repo"):
        with pytest.raises(TimeoutError):
            async with asyncio.timeout(0.01):
                async with locks.hold("opencode", "repo"):
                    raise AssertionError("the waiter should time out")
    assert len(locks) == 0

    with pytest.raises(RuntimeError, match="maintenance"):
        async with locks.hold("opencode", "repo"):
            raise RuntimeError("maintenance")
    assert len(locks) == 0

    async with locks.hold("opencode", "repo"):
        pass
    assert len(locks) == 0


@pytest.mark.asyncio
async def test_runner_holds_custom_conversation_through_release(tmp_path) -> None:
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.wire import PublicEngineConfig

    channel = ChannelConfig.model_validate(
        {
            "name": "chat",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "session": {"type": "custom", "key": "{{ payload.conversation }}"},
        }
    )
    pool_calls: list[str] = []
    release_started = asyncio.Event()
    allow_release = asyncio.Event()

    class Pool:
        sessions: dict[str, str] = {}

        async def acquire(self, _key, _cfg):
            pool_calls.append("acquire")
            return SimpleNamespace(proxy_token="token")

        async def release(self, _key, ttl_seconds):
            pool_calls.append("release")
            if _key == "one":
                release_started.set()
                await allow_release.wait()

    first_turn = asyncio.Event()
    finish_first = asyncio.Event()
    active: set[str] = set()
    overlap = False
    turn_count = 0

    async def run_turn(_server, **_kwargs):
        nonlocal overlap, turn_count
        turn_count += 1
        key = _kwargs["conv_key"]
        if key in active:
            overlap = True
        active.add(key)
        if turn_count == 1:
            first_turn.set()
            await finish_first.wait()
        active.remove(key)
        from ach_agent.engine.base.driver import TurnResult

        return TurnResult(text='{"action":"none","text":"ok"}', session_ref=key)

    driver = SimpleNamespace(
        engine_type="opencode",
        discard_session=AsyncMock(),
        compact_session=AsyncMock(),
        run_turn=run_turn,
    )

    def event(name: str, conversation: str) -> MessageEvent:
        return MessageEvent(
            idempotency_key=name,
            session_key=name,
            channel_name="chat",
            payload={"conversation": conversation},
        )

    runner = make_engine_runner(
        client=RunnerClient(Pool(), driver),
        engine_cfg=PublicEngineConfig(home=str(tmp_path / "home"), work_dir=str(tmp_path / "work")),
        max_invocation_seconds=30,
        channels_by_name={"chat": channel},
    )
    first = asyncio.create_task(runner(event("one", "shared"), lambda: None))
    await first_turn.wait()
    second = asyncio.create_task(runner(event("two", "shared"), lambda: None))
    third = asyncio.create_task(runner(event("three", "other"), lambda: None))
    await asyncio.sleep(0)
    assert not second.done()
    await asyncio.wait_for(third, timeout=1)
    finish_first.set()
    await release_started.wait()
    assert not second.done()
    allow_release.set()
    await asyncio.wait_for(first, timeout=1)
    await asyncio.wait_for(second, timeout=1)

    assert not overlap
    assert pool_calls == ["acquire", "acquire", "release", "release", "acquire", "release"]


@pytest.mark.asyncio
async def test_supplied_empty_registry_is_used_and_evicted(tmp_path) -> None:
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.wire import PublicEngineConfig

    channel = ChannelConfig.model_validate(
        {
            "name": "chat",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "session": {"type": "custom", "key": "{{ payload.conversation }}"},
        }
    )
    waiter_seen = asyncio.Event()

    class TrackingLocks(ConversationLocks):
        hold_count = 0

        def hold(self, engine_type, conversation_key):
            self.hold_count += 1
            if self.hold_count == 2:
                waiter_seen.set()
            return super().hold(engine_type, conversation_key)

    locks = TrackingLocks()
    entered = asyncio.Event()
    allow = asyncio.Event()
    acquire_count = 0

    class Pool:
        sessions: dict[str, str] = {}

        async def acquire(self, _key, _cfg):
            nonlocal acquire_count
            acquire_count += 1
            return SimpleNamespace(proxy_token="token")

        async def release(self, _key, ttl_seconds):
            return None

    driver = SimpleNamespace(engine_type="opencode")

    async def run_turn(*_args, **kwargs):
        if acquire_count == 1:
            entered.set()
            await allow.wait()
        return {"action": "none", "text": "ok"}

    def event(name: str) -> MessageEvent:
        return MessageEvent(
            idempotency_key=name,
            session_key=name,
            channel_name="chat",
            payload={"conversation": "shared"},
        )

    with patch("ach_agent.engine.base.terminal.run_contract_turn", new=run_turn):
        runner = make_engine_runner(
            client=RunnerClient(Pool(), driver),
            engine_cfg=PublicEngineConfig(
                home=str(tmp_path / "home"), work_dir=str(tmp_path / "work")
            ),
            max_invocation_seconds=30,
            channels_by_name={"chat": channel},
            conversation_locks=locks,
        )
        first = asyncio.create_task(runner(event("one"), lambda: None))
        await entered.wait()
        second = asyncio.create_task(runner(event("two"), lambda: None))
        await asyncio.wait_for(waiter_seen.wait(), timeout=1)
        assert not second.done()
        assert len(locks) == 1
        assert locks._entries[("opencode", "shared")].references == 2
        allow.set()
        await first
        await second

    assert acquire_count == 2
    assert len(locks) == 0


@pytest.mark.asyncio
async def test_runner_cancellation_during_release_does_not_poison_lock(tmp_path) -> None:
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.wire import PublicEngineConfig

    channel = ChannelConfig.model_validate(
        {
            "name": "chat",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "session": {"type": "custom", "key": "{{ payload.conversation }}"},
        }
    )
    release_started = asyncio.Event()
    allow_release = asyncio.Event()
    release_count = 0

    class Pool:
        sessions: dict[str, str] = {}

        async def acquire(self, _key, _cfg):
            return SimpleNamespace(proxy_token="token")

        async def release(self, _key, ttl_seconds):
            nonlocal release_count
            release_count += 1
            if release_count == 1:
                release_started.set()
                await allow_release.wait()

    driver = SimpleNamespace(engine_type="opencode")

    async def run_turn(*_args, **_kwargs):
        return {"action": "none", "text": "ok"}

    def event(name: str) -> MessageEvent:
        return MessageEvent(
            idempotency_key=name,
            session_key=name,
            channel_name="chat",
            payload={"conversation": "shared"},
        )

    with patch("ach_agent.engine.base.terminal.run_contract_turn", new=run_turn):
        runner = make_engine_runner(
            client=RunnerClient(Pool(), driver),
            engine_cfg=PublicEngineConfig(
                home=str(tmp_path / "home"), work_dir=str(tmp_path / "work")
            ),
            max_invocation_seconds=30,
            channels_by_name={"chat": channel},
        )
        cancelled = asyncio.create_task(runner(event("one"), lambda: None))
        await release_started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        allow_release.set()
        await asyncio.wait_for(runner(event("two"), lambda: None), timeout=1)
