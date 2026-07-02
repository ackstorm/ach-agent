"""A2A end-to-end test (CHN-05) — Plan 04-04 GREEN.

Full-harness e2e: A2AAgentExecutorBridge → router → engine → completed EventQueue event.

Architecture (hermetic — no live A2A peer):
  - MockEventQueue (conftest.py): captures EventQueue.enqueue_event() calls
  - A2AAgentExecutorBridge.execute() called directly with MockEventQueue
  - fake_engine_runner: extracts on_complete from delivery_context, fires it
  - asyncio.Event + asyncio.timeout(5.0): no naked polling loops (CLAUDE.md)

Wiring pattern (mirrors main.py boot seam):
  - on_complete closure is created AFTER bridge is instantiated; captures bridge reference
  - Handler wrapper injects on_complete into event.delivery_context before routing
  - engine_runner extracts and calls on_complete(session_key, reply_text)
  - bridge.signal_completion schedules enqueue_event + sets completion asyncio.Event
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock

import pytest

from ach_agent.channels.a2a import A2AAgentExecutorBridge
from ach_agent.channels.message_event import MessageEvent
from ach_agent.router import Router
from ach_agent.router.dedup import InMemoryDedupStore
from ach_agent.router.router import RouterAdmitResult
from tests.e2e.conftest import MockEventQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeContext:
    """Minimal stand-in for a2a-sdk RequestContext in e2e tests."""

    def __init__(
        self,
        task_id: str = "task-e2e-1",
        context_id: str = "ctx-e2e-1",
        text: str = "review this",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self._text = text
        call_ctx = MagicMock()
        call_ctx.state = {"headers": headers or {}}
        self.call_context = call_ctx

    def get_user_input(self) -> str:
        return self._text


_E2E_TEST_SECRET = "e2e-test-secret"
_E2E_TEST_HEADER = "x-a2a-custom-api-key"
_E2E_TEST_ENV = "ACH_SECRET_A2A_E2E_TEST"


def _make_a2a_channel_cfg(name: str = "a2a-test", env_name: str = "") -> Any:
    """Build minimal A2A ChannelConfig with an optional secret env var for auth testing."""
    from ach_agent.config.schema import A2AAuthBlock, A2ABlock, ChannelConfig, SecretSource

    secret = SecretSource(env=env_name) if env_name else None
    a2a_block = A2ABlock(auth=A2AAuthBlock(secret=secret))
    return ChannelConfig(name=name, type="a2a", a2a=a2a_block)


def _make_a2a_channel_cfg_with_secret(
    monkeypatch: pytest.MonkeyPatch, name: str = "a2a-test"
) -> Any:
    """Build A2A ChannelConfig with a real secret env var for tests that exercise auth."""
    monkeypatch.setenv(_E2E_TEST_ENV, _E2E_TEST_SECRET)
    return _make_a2a_channel_cfg(name=name, env_name=_E2E_TEST_ENV)


def _authed_ctx(**kwargs: Any) -> FakeContext:
    """Return a FakeContext with the correct auth header for e2e tests."""
    kwargs.setdefault("headers", {})
    kwargs["headers"][_E2E_TEST_HEADER] = _E2E_TEST_SECRET
    return FakeContext(**kwargs)


def _build_bridge_with_router(
    router: Router,
    channel_cfg: Any,
) -> tuple[A2AAgentExecutorBridge, Any]:
    """Build a bridge + handler wrapper that injects on_complete into delivery_context.

    Returns (bridge, handler_wrapper).
    The engine_runner must call event.delivery_context['on_complete'](session_key, reply_text)
    to signal completion back to the bridge.
    """
    bridge: A2AAgentExecutorBridge | None = None

    class _HandlerWithOnComplete:
        async def handle(self, event: MessageEvent) -> RouterAdmitResult:
            # Inject on_complete closure (mirrors main.py boot seam: captures bridge)
            def on_complete(session_key: str, reply_text: str) -> None:
                if bridge is not None:
                    bridge.signal_completion(session_key, reply_text)

            event.delivery_context["on_complete"] = on_complete
            return await router.handle(event)

    handler = _HandlerWithOnComplete()
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)
    return bridge, handler


# ---------------------------------------------------------------------------
# E2E: happy path — A2A task → governed pipeline → completed EventQueue event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_task_routes_to_engine_and_enqueues_completed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHN-05 e2e: A2A inbound task → governed pipeline → TaskStatusUpdateEvent(completed)."""
    from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED

    _REPLY_TEXT = "LGTM from A2A engine"

    async def fake_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        """Fake engine: fires on_complete from delivery_context to complete bridge."""
        on_kill()
        on_complete = event.delivery_context.get("on_complete")
        if on_complete is not None:
            on_complete(event.session_key, _REPLY_TEXT)

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner,
        delivery_adapter=None,
    )

    channel_cfg = _make_a2a_channel_cfg_with_secret(monkeypatch)
    bridge, _ = _build_bridge_with_router(router, channel_cfg)

    ctx = _authed_ctx(task_id="task-e2e-1", context_id="ctx-e2e-1", text="review this")
    eq = MockEventQueue()

    # Execute with asyncio.timeout — no naked polling (CLAUDE.md)
    async with asyncio.timeout(5.0):
        await bridge.execute(ctx, eq)

    # Assert: completed event was enqueued
    completed_events = [e for e in eq.events if e.status.state == TASK_STATE_COMPLETED]
    assert len(completed_events) == 1, f"Expected completed event, got: {eq.events}"
    assert _REPLY_TEXT in completed_events[0].status.message.parts[0].text


@pytest.mark.asyncio
async def test_engine_runner_signals_on_fail_on_engine_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """F1 regression: when the engine raises, engine_runner MUST call
    delivery_context['on_fail'] (and still re-raise). Otherwise the a2a bridge — whose
    execute() awaits completion.wait() with NO timeout — hangs forever.
    """
    from ach_agent.engine import lifecycle
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.main import _make_engine_runner

    class _FakeServer:
        def is_alive(self) -> bool:
            return True

    class _FakePool:
        oc_sessions: dict[str, str] = {}

        async def acquire(self, session_key: str, config: Any) -> _FakeServer:
            return _FakeServer()

        async def release(self, session_key: str, ttl_seconds: float) -> None:
            return None

    async def _boom(**_kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("engine exploded")

    # _make_engine_runner imports run_invocation from lifecycle at call time → patch the source.
    monkeypatch.setattr(lifecycle, "run_invocation", _boom)

    runner = _make_engine_runner(
        pool=_FakePool(),
        engine_cfg=EngineConfig(),
        max_invocation_seconds=30,
        memory_cfg=None,
    )

    failures: list[tuple[str, str]] = []
    event = MessageEvent(
        idempotency_key="i-1",
        session_key="ctx-fail-1",
        channel_name="a2a",
        payload={"text": "review this"},
        delivery_context={"on_fail": lambda k, r: failures.append((k, r))},
    )

    with pytest.raises(RuntimeError, match="engine exploded"):
        await runner(event, lambda: None)

    assert len(failures) == 1 and failures[0][0] == "ctx-fail-1", (
        "engine_runner must signal on_fail on an engine error — else the a2a executor hangs"
    )


# ---------------------------------------------------------------------------
# E2E: decouple — engine not ready (cold pool) still routes to the engine
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_engine_not_ready_still_routes_to_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decouple: engine-not-ready (cold pool) no longer blocks dispatch — the task
    routes through to the engine like any other request (lazy engine start).
    """
    from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED

    _REPLY_TEXT = "LGTM despite cold start"
    engine_invocations: list[Any] = []

    async def fake_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        engine_invocations.append(event)
        on_kill()
        on_complete = event.delivery_context.get("on_complete")
        if on_complete is not None:
            on_complete(event.session_key, _REPLY_TEXT)

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner,
        delivery_adapter=None,
    )

    channel_cfg = _make_a2a_channel_cfg_with_secret(monkeypatch)
    # Cold pool — engine not ready yet; must NOT block dispatch (decoupled)
    bridge, _ = _build_bridge_with_router(router, channel_cfg)

    ctx = _authed_ctx(task_id="task-cold-1")
    eq = MockEventQueue()

    # Execute with asyncio.timeout — no naked polling (CLAUDE.md)
    async with asyncio.timeout(5.0):
        await bridge.execute(ctx, eq)

    completed_events = [e for e in eq.events if e.status.state == TASK_STATE_COMPLETED]
    assert len(completed_events) == 1, f"Expected completed event, got: {eq.events}"
    assert len(engine_invocations) == 1, "engine must be invoked despite cold pool (lazy start)"


# ---------------------------------------------------------------------------
# E2E: dedup rejects repeated task_id
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_dedup_rejects_repeated_task_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """CHN-05/IDM-01: duplicate task_id → deduplicated (router drops second task)."""
    from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED

    _REPLY_TEXT = "done"
    engine_invocations: list[Any] = []

    async def fake_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        engine_invocations.append(event)
        on_kill()
        on_complete = event.delivery_context.get("on_complete")
        if on_complete is not None:
            on_complete(event.session_key, _REPLY_TEXT)

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner,
        delivery_adapter=None,
    )

    channel_cfg = _make_a2a_channel_cfg_with_secret(monkeypatch)

    # First task — should succeed
    bridge1, _ = _build_bridge_with_router(router, channel_cfg)
    ctx1 = _authed_ctx(task_id="task-dedup", context_id="ctx-dedup", text="hello")
    eq1 = MockEventQueue()
    async with asyncio.timeout(5.0):
        await bridge1.execute(ctx1, eq1)

    assert any(e.status.state == TASK_STATE_COMPLETED for e in eq1.events)
    assert len(engine_invocations) == 1

    # Second task with SAME task_id — should be deduplicated (DUPLICATE result, no completed event)
    bridge2, _ = _build_bridge_with_router(router, channel_cfg)
    ctx2 = _authed_ctx(task_id="task-dedup", context_id="ctx-dedup", text="hello again")
    eq2 = MockEventQueue()

    # Dedup path: execute() returns quickly without enqueuing any event
    await bridge2.execute(ctx2, eq2)

    # No events emitted on duplicate
    assert len(eq2.events) == 0
    # Engine still only invoked once total
    assert len(engine_invocations) == 1
