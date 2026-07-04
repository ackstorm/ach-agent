"""A2A channel adapter unit tests (CHN-05, D-03, D-06, §14.6) — Plan 04-04 GREEN.

Covers:
  - A2AAgentExecutorBridge.execute() builds canonical MessageEvent
  - derive_a2a_idempotency_key: task_id → a2a:{task_id}, empty → ms-timestamp
  - session_key = context_id (fallback to task_id)
  - Header auth (spec §14.6 / T-04-13): missing/wrong header → failed event, handler never called
  - Decouple: engine not ready no longer gates dispatch — routes normally (lazy engine start)
  - FULL_QUEUE → enqueue failed event (never silent)
  - cancel() → enqueue canceled event
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from ach_agent.channels.a2a import A2AAgentExecutorBridge
from ach_agent.router.router import RouterAdmitResult

# ---------------------------------------------------------------------------
# Idempotency derivation (already GREEN from Plan 04-01, kept here for
# completeness / regression guard)
# ---------------------------------------------------------------------------


def test_derive_a2a_idempotency_key_non_empty_task_id() -> None:
    """CHN-05/IDM-01: non-empty task_id → a2a:{task_id}."""
    from ach_agent.router.dedup import derive_a2a_idempotency_key

    assert derive_a2a_idempotency_key("task-123") == "a2a:task-123"


def test_derive_a2a_idempotency_key_empty_task_id_is_non_empty_ms_timestamp() -> None:
    """IDM-01: empty task_id → non-empty ms-timestamp (unique-per-arrival, never shared)."""
    from ach_agent.router.dedup import derive_a2a_idempotency_key

    key = derive_a2a_idempotency_key("")
    assert key
    assert key != "a2a:"
    assert key.isdigit(), f"Expected ms-timestamp digits, got: {key!r}"


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class MockEventQueue:
    """Captures enqueue_event calls for assertions."""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def enqueue_event(self, event: Any) -> None:
        self.events.append(event)


class FakeContext:
    """Minimal stand-in for a2a.server.agent_execution.context.RequestContext."""

    def __init__(
        self,
        task_id: str = "task-1",
        context_id: str = "",
        text: str = "hello",
        headers: dict[str, str] | None = None,
    ) -> None:
        self.task_id = task_id
        self.context_id = context_id
        self._text = text
        # Build call_context.state['headers'] mirroring DefaultServerCallContextBuilder
        call_ctx = MagicMock()
        call_ctx.state = {"headers": headers or {}}
        self.call_context = call_ctx

    def get_user_input(self) -> str:
        return self._text


def _make_channel_cfg(
    name: str = "test-a2a",
    env_name: str = "",
    header: str = "x-a2a-custom-api-key",
) -> Any:
    """Build a minimal ChannelConfig-like object for unit tests."""
    from ach_agent.config.schema import A2AAuthBlock, A2ABlock, ChannelConfig, SecretSource

    a2a_auth = A2AAuthBlock(header=header, secret=SecretSource(env=env_name))
    a2a_block = A2ABlock(auth=a2a_auth)
    return ChannelConfig(name=name, type="a2a", a2a=a2a_block)


_UNIT_TEST_SECRET = "unit-test-secret"
_UNIT_TEST_HEADER = "x-a2a-custom-api-key"
_UNIT_TEST_ENV = "ACH_SECRET_A2A_TEST"


def _make_authed_channel_cfg(monkeypatch: pytest.MonkeyPatch, name: str = "test-a2a") -> Any:
    """Build a ChannelConfig with a real secret env var for tests that need auth to pass.

    After CR-01/CR-02 fix, tests that want to exercise post-auth logic (session keys,
    A′ gate, full queue, etc.) must provide a valid secret+header so auth passes.
    """
    monkeypatch.setenv(_UNIT_TEST_ENV, _UNIT_TEST_SECRET)
    return _make_channel_cfg(name=name, env_name=_UNIT_TEST_ENV)


def _authed_ctx(**kwargs: Any) -> FakeContext:
    """Return a FakeContext pre-populated with the correct auth header for unit tests."""
    kwargs.setdefault("headers", {})
    kwargs["headers"][_UNIT_TEST_HEADER] = _UNIT_TEST_SECRET
    return FakeContext(**kwargs)


def _make_accepted_handler() -> AsyncMock:
    handler = AsyncMock()
    handler.handle.return_value = RouterAdmitResult.ACCEPTED
    return handler


def _make_full_queue_handler() -> AsyncMock:
    handler = AsyncMock()
    handler.handle.return_value = RouterAdmitResult.FULL_QUEUE
    return handler


# ---------------------------------------------------------------------------
# Header auth tests (spec §14.6 / T-04-13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_header_auth_missing_header_enqueues_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.6/T-04-13: missing x-a2a-custom-api-key header → failed event, handler NOT called."""
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    monkeypatch.setenv(_UNIT_TEST_ENV, "correct-secret")

    handler = _make_accepted_handler()
    channel_cfg = _make_channel_cfg(env_name=_UNIT_TEST_ENV)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    # Context with NO auth header
    ctx = FakeContext(headers={})
    eq = MockEventQueue()

    await bridge.execute(ctx, eq)

    # Must emit exactly one failed event
    assert len(eq.events) == 1
    evt = eq.events[0]
    assert evt.status.state == TASK_STATE_FAILED
    # handler.handle must NEVER be called (T-04-13)
    handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_a2a_header_auth_wrong_header_enqueues_failed_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.6/T-04-13: wrong x-a2a-custom-api-key header → failed event, handler NOT called."""
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    monkeypatch.setenv(_UNIT_TEST_ENV, "correct-secret")

    handler = _make_accepted_handler()
    channel_cfg = _make_channel_cfg(env_name=_UNIT_TEST_ENV)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    # Context with WRONG auth header
    ctx = FakeContext(headers={"x-a2a-custom-api-key": "wrong-secret"})
    eq = MockEventQueue()

    await bridge.execute(ctx, eq)

    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_FAILED
    handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_a2a_header_auth_correct_header_proceeds_to_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """§14.6: correct x-a2a-custom-api-key header → handler.handle IS called."""
    monkeypatch.setenv(_UNIT_TEST_ENV, "correct-secret")

    handler = _make_accepted_handler()
    channel_cfg = _make_channel_cfg(env_name=_UNIT_TEST_ENV)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = FakeContext(headers={"x-a2a-custom-api-key": "correct-secret"})
    eq = MockEventQueue()

    # We DON'T await completion here (no signal_completion called in this unit test),
    # so we drive execute() until it blocks on completion.wait() using a task + cancel.
    task = asyncio.create_task(bridge.execute(ctx, eq))
    # Give the coroutine a chance to run past handler.handle and reach completion.wait()
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # handler.handle should have been called (auth passed, A′ passed, ACCEPTED)
    handler.handle.assert_called_once()

    # Cancel the task (it is blocked on completion.wait — expected in unit test context)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Session key derivation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_executor_bridge_builds_correct_message_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHN-05: executor bridge builds MessageEvent with correct idempotency_key."""
    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-abc", context_id="ctx-1", text="hi there")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    handler.handle.assert_called_once()
    captured_event = handler.handle.call_args[0][0]
    assert captured_event.idempotency_key == "a2a:task-abc"
    assert captured_event.channel_name == channel_cfg.name
    assert captured_event.source_trait == "async_no_retry"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a2a_session_key_uses_context_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-03: session_key = context_id when present."""
    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-1", context_id="ctx-999")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    captured_event = handler.handle.call_args[0][0]
    # session_key must be context_id (priority over task_id)
    assert captured_event.session_key == "ctx-999"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a2a_session_key_fallback_to_task_id_when_no_context_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-03: session_key = task_id when context_id is absent."""
    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-xyz", context_id="")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    captured_event = handler.handle.call_args[0][0]
    assert captured_event.session_key == "task-xyz"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ---------------------------------------------------------------------------
# Decouple acceptance from engine readiness + FULL_QUEUE
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_execute_emits_interim_working_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-blocking support: execute() emits one interim WORKING event after a successful
    admit and BEFORE completion.wait(), so the SDK's consume_and_break_on_interrupt
    (blocking=False) has a task-creating event to break on and can return task_id now.
    The WORKING event must be non-terminal and carry ids matching the TaskManager."""
    from a2a.types.a2a_pb2 import TASK_STATE_WORKING

    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-w", context_id="ctx-w")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Dispatched, now blocked on completion.wait() — the interim WORKING event is present.
    handler.handle.assert_called_once()
    assert len(eq.events) == 1
    evt = eq.events[0]
    assert evt.status.state == TASK_STATE_WORKING
    assert evt.task_id == "task-w"
    assert evt.context_id == "ctx-w"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a2a_engine_not_ready_routes_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decouple: engine-not-ready (cold pool) no longer emits a "Service warming up"
    failed event — the bridge routes to the handler like any other request. The engine
    starts lazily inside pool.acquire() (main.py engine_runner), not at the channel layer.
    """
    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-1")
    eq = MockEventQueue()

    # We DON'T await completion here (no signal_completion called in this unit test),
    # so we drive execute() until it blocks on completion.wait() using a task + cancel.
    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    handler.handle.assert_called_once()
    # After dispatch the bridge emits one interim WORKING event (non-blocking support),
    # not a failed "service warming up" event. The engine still starts lazily in the lane.
    from a2a.types.a2a_pb2 import TASK_STATE_WORKING

    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_WORKING

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_a2a_full_queue_enqueues_failed_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-05/RTR-05: FULL_QUEUE → enqueue failed event (not silent drop — there is a caller)."""
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    handler = _make_full_queue_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-1")
    eq = MockEventQueue()

    await bridge.execute(ctx, eq)

    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_FAILED


# ---------------------------------------------------------------------------
# Regression tests for gap-closure fixes (CR-01 / CR-02 / CR-04)
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="v3 schema rejects a2a channel with a2a=None at config-load "
    "(model_validator), so this runtime-layer case is unconstructable — Plan 3"
)
@pytest.mark.asyncio
async def test_cr01_no_auth_block_rejects_request() -> None:
    """CR-01: A2A channel with no a2a sub-block (a2a=None) must REJECT, not admit.

    Regression: old code silently skips auth and calls handler.handle() when a2a=None,
    then hangs on completion.wait(). The test wraps execute() with a short timeout;
    before the fix it times out (handler.handle was called); after the fix it completes
    immediately with a failed event and handler.handle is never called.
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    handler = _make_accepted_handler()

    # ChannelConfig with NO a2a sub-block — simulates operator omitting `a2a:` from config
    from ach_agent.config.schema import ChannelConfig

    channel_cfg = ChannelConfig(name="test-a2a-no-auth", type="a2a", a2a=None)

    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)
    ctx = FakeContext(headers={"x-a2a-custom-api-key": "any-value"})
    eq = MockEventQueue()

    # Bounded: must complete quickly (reject path); would hang on buggy code
    await asyncio.wait_for(bridge.execute(ctx, eq), timeout=2.0)

    # Must reject with a failed event and never dispatch to handler
    assert len(eq.events) == 1, f"Expected 1 failed event, got {len(eq.events)}"
    assert eq.events[0].status.state == TASK_STATE_FAILED
    handler.handle.assert_not_called()


@pytest.mark.skip(
    reason="v3 schema (SecretSource requires env) rejects an empty secret at config-load, "
    "so 'no auth secret configured' is unconstructable here — mirrors "
    "test_cr01_no_auth_block_rejects_request above"
)
@pytest.mark.asyncio
async def test_cr01_empty_secret_path_rejects_request() -> None:
    """CR-01/CR-02: A2A channel with empty secret env must REJECT, not admit.

    Empty secret.env means no auth secret is configured — fail-closed.
    Before the fix: auth is skipped, handler.handle() called, hangs on completion.wait().
    After the fix: execute() returns immediately with a failed event.
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    handler = _make_accepted_handler()
    # env_name="" — no auth configured
    channel_cfg = _make_channel_cfg(env_name="")

    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)
    ctx = FakeContext(headers={})  # no auth header presented either
    eq = MockEventQueue()

    # Bounded: must complete quickly; would hang on buggy code
    await asyncio.wait_for(bridge.execute(ctx, eq), timeout=2.0)

    # Must reject — empty-vs-empty hmac.compare_digest("","") must NOT pass
    assert len(eq.events) == 1, f"Expected 1 failed event, got {len(eq.events)}"
    assert eq.events[0].status.state == TASK_STATE_FAILED
    handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_cr02_unset_env_secret_rejects_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-01/CR-02: schema-valid secret={env: NAME} whose env var is UNSET must REJECT.

    Still-reachable runtime path the empty-secret migration left uncovered: unlike
    test_cr01_empty_secret_path_rejects_request above (unconstructable — SecretSource
    requires exactly one of {env, file}), this builds a perfectly schema-valid
    SecretSource(env=...) and only leaves the *value* missing at request time.
    resolve_secret() returns None for an unset env var, so auth must still fail-closed
    (not admit, not hang on completion.wait()).
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    from ach_agent.config.schema import A2AAuthBlock, A2ABlock, ChannelConfig, SecretSource

    env_name = "ACH_SECRET_UNSET_XYZ"
    monkeypatch.delenv(env_name, raising=False)

    handler = _make_accepted_handler()
    a2a_auth = A2AAuthBlock(header=_UNIT_TEST_HEADER, secret=SecretSource(env=env_name))
    channel_cfg = ChannelConfig(name="test-a2a", type="a2a", a2a=A2ABlock(auth=a2a_auth))

    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)
    ctx = FakeContext(headers={})  # no auth header presented either
    eq = MockEventQueue()

    # Bounded: must complete quickly; would hang on buggy (fail-open) code
    await asyncio.wait_for(bridge.execute(ctx, eq), timeout=2.0)

    assert len(eq.events) == 1, f"Expected 1 failed event, got {len(eq.events)}"
    assert eq.events[0].status.state == TASK_STATE_FAILED
    handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_cr02_unresolvable_env_secret_rejects_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02: Unresolvable secret (env var unset) must REJECT, not pass auth (empty-vs-empty).

    resolve_secret returns None when the env var is unset; old code did
    hmac.compare_digest("","")=True when presented header is also empty, then hangs on
    completion.wait(). Fix: treat unresolvable/empty secret as auth failure (reject
    immediately).
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    env_name = "ACH_SECRET_A2A_UNRESOLVABLE"
    monkeypatch.delenv(env_name, raising=False)

    handler = _make_accepted_handler()
    channel_cfg = _make_channel_cfg(env_name=env_name)

    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)
    # Caller sends no header — "" vs "" was previously True → auth "passed"
    ctx = FakeContext(headers={})
    eq = MockEventQueue()

    # Bounded: must complete quickly; would hang on buggy code (auth pass → dispatch → hang)
    await asyncio.wait_for(bridge.execute(ctx, eq), timeout=2.0)

    assert len(eq.events) == 1, f"Expected 1 failed event, got {len(eq.events)}"
    assert eq.events[0].status.state == TASK_STATE_FAILED
    handler.handle.assert_not_called()


@pytest.mark.asyncio
async def test_cr04_concurrent_empty_key_calls_complete_independently(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-04: Two concurrent execute() with empty context_id+task_id must not collide/hang.

    Old code: both write session_key="" to _pending; second overwrites first,
    first coroutine's completion.wait() hangs forever.

    After fix: empty-key calls are rejected immediately (failed event — CR-04), so both
    complete without hanging. Uses a valid secret+header so auth passes and the session_key
    path is actually exercised. asyncio.wait_for provides the bounded timeout.
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    monkeypatch.setenv(_UNIT_TEST_ENV, "test-secret")

    handler = _make_accepted_handler()
    channel_cfg = _make_channel_cfg(env_name=_UNIT_TEST_ENV)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    # Provide correct auth header so requests pass auth and reach the session_key check
    auth_headers = {"x-a2a-custom-api-key": "test-secret"}
    ctx1 = FakeContext(task_id="", context_id="", headers=auth_headers)
    ctx2 = FakeContext(task_id="", context_id="", headers=auth_headers)
    eq1 = MockEventQueue()
    eq2 = MockEventQueue()

    # Both must finish within the timeout — if they hang, we get TimeoutError → test fails
    task1 = asyncio.create_task(asyncio.wait_for(bridge.execute(ctx1, eq1), timeout=2.0))
    task2 = asyncio.create_task(asyncio.wait_for(bridge.execute(ctx2, eq2), timeout=2.0))

    results = await asyncio.gather(task1, task2, return_exceptions=True)

    for i, result in enumerate(results, 1):
        assert not isinstance(result, asyncio.TimeoutError), (
            f"execute() call {i} timed out — empty session_key collision (CR-04)"
        )
        # Only TimeoutError indicates the bug; other exceptions are unexpected
        assert result is None, f"execute() call {i} raised unexpected error: {result}"

    # Both calls should have been rejected with a failed event (CR-04 fix)
    assert len(eq1.events) == 1 and eq1.events[0].status.state == TASK_STATE_FAILED
    assert len(eq2.events) == 1 and eq2.events[0].status.state == TASK_STATE_FAILED
    # handler.handle must NOT have been called (rejected before dispatch)
    handler.handle.assert_not_called()


# ---------------------------------------------------------------------------
# Boot-time builders (§14.6) — regression guards for two latent bugs that were
# untested: make_a2a_agent_card passed a non-existent `url=` kwarg (pb2 AgentCard
# has no `url` field → ValueError), and build_a2a_app omitted the required
# `agent_card` arg to LegacyRequestHandler (→ TypeError). Both crash the moment
# the A2A receiver is booted via main.py.
# ---------------------------------------------------------------------------


def test_make_a2a_agent_card_builds_without_error() -> None:
    """make_a2a_agent_card must construct a valid pb2 AgentCard (Bug 1 guard)."""
    from ach_agent.channels.a2a import make_a2a_agent_card

    card = make_a2a_agent_card("my-channel")
    assert card.name == "my-channel"
    assert "my-channel" in card.description
    assert card.version == "1.0.0"


def test_build_a2a_app_constructs_sub_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """build_a2a_app must wire LegacyRequestHandler with agent_card (Bug 2 guard)."""
    from fastapi import FastAPI

    from ach_agent.channels.a2a import build_a2a_app, make_a2a_agent_card

    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=_make_accepted_handler(), channel_cfg=channel_cfg)
    agent_card = make_a2a_agent_card(channel_cfg.name)

    sub_app = build_a2a_app(agent_card, bridge)
    assert isinstance(sub_app, FastAPI)


def test_agent_card_is_0_3_x_compatible(monkeypatch: pytest.MonkeyPatch) -> None:
    """Served card must satisfy a2a-sdk 0.3.x consumers (e.g. LiteLLM proxy, which pins
    a2a-sdk<1.0): top-level `url` + `skills` + defaultInput/OutputModes, at BOTH the
    canonical agent-card.json and the legacy agent.json. The 1.x AgentCard drops `url`
    and omits empty `skills`, so build_a2a_app injects them into the served dict."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ach_agent.channels.a2a import build_a2a_app, make_a2a_agent_card

    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=_make_accepted_handler(), channel_cfg=channel_cfg)
    sub_app = build_a2a_app(make_a2a_agent_card("review"), bridge)

    parent = FastAPI()
    parent.mount("/a2a/review", sub_app)  # mirror main.py's mount prefix
    client = TestClient(parent)

    for path in (
        "/a2a/review/.well-known/agent-card.json",
        "/a2a/review/.well-known/agent.json",
    ):
        resp = client.get(path)
        assert resp.status_code == 200, path
        card = resp.json()
        # the four fields a2a-sdk 0.3.x requires but the 1.x card does not serve
        assert card["url"].endswith("/a2a/review"), path
        assert card["skills"] == [], path
        assert card["defaultInputModes"] == ["text"], path
        assert card["defaultOutputModes"] == ["text"], path


def test_agent_card_advertises_1x_jsonrpc_interface(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 1.x client must pick the native JSON-RPC transport (`SendMessage`), not the legacy
    CompatJsonRpcTransport (`message/send`, which our handler -32601s). That requires the card
    to advertise an explicit supportedInterfaces at protocolVersion 1.0; otherwise
    parse_agent_card synthesizes a 0.3.0 interface from `url` and the client goes legacy."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from ach_agent.channels.a2a import build_a2a_app, make_a2a_agent_card

    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=_make_accepted_handler(), channel_cfg=channel_cfg)
    sub_app = build_a2a_app(make_a2a_agent_card("review"), bridge)
    parent = FastAPI()
    parent.mount("/a2a/review", sub_app)

    card = TestClient(parent).get("/a2a/review/.well-known/agent-card.json").json()

    # explicit native 1.x interface present
    assert card["protocolVersion"] == "1.0"
    assert card["preferredTransport"] == "JSONRPC"
    assert any(
        i["protocolBinding"] == "JSONRPC" and i["protocolVersion"] == "1.0"
        for i in card["supportedInterfaces"]
    )
    # and the a2a-sdk 1.x resolver reads it as a NON-legacy (1.0) interface, not 0.3.0
    from a2a.client.card_resolver import parse_agent_card

    parsed = parse_agent_card(dict(card))
    versions = [i.protocol_version for i in parsed.supported_interfaces]
    assert versions and all(v == "1.0" for v in versions), versions
    # still 0.3.x-compatible
    assert card["url"].endswith("/a2a/review")
    assert card["skills"] == []


# ---------------------------------------------------------------------------
# signal_failure — FAILED callback on invalid terminal output
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_signal_failure_enqueues_failed_event_and_unblocks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """signal_failure pops pending, enqueues a FAILED event, and sets the completion Event.

    Mirrors signal_completion: the executor blocked on completion.wait() must unblock
    (Pitfall 5 — never hang), the peer must receive a FAILED TaskStatusUpdateEvent (not a
    COMPLETED one), and the session_key must no longer be pending.
    """
    from a2a.types.a2a_pb2 import TASK_STATE_FAILED

    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    # Populate _pending exactly the way execute() would (mirror signal_completion setup).
    session_key = "ctx-fail"
    eq = MockEventQueue()
    completion = asyncio.Event()
    bridge._pending[session_key] = (eq, completion, "task-fail", session_key)

    bridge.signal_failure(session_key, "bad terminal")

    # The async task scheduled by signal_failure runs on the next loop tick.
    await asyncio.wait_for(completion.wait(), timeout=2.0)

    assert completion.is_set()
    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_FAILED
    # ids must be stamped so TaskManager.save_task_event accepts the event
    assert eq.events[0].task_id == "task-fail"
    assert eq.events[0].context_id == session_key
    # session_key must be popped from pending
    assert session_key not in bridge._pending


@pytest.mark.asyncio
async def test_a2a_signal_failure_unknown_session_key_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """signal_failure for an unknown session_key must not raise (mirror signal_completion)."""
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=_make_accepted_handler(), channel_cfg=channel_cfg)

    # Should log a warning and return without error.
    bridge.signal_failure("does-not-exist", "reason")


# ---------------------------------------------------------------------------
# Terminal sequence regression guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a2a_terminal_sequence_is_working_then_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full accepted turn enqueues WORKING (interim) then COMPLETED (terminal), in order,
    both carrying ids matching the TaskManager. This is the sequence the SDK's background
    consumer persists to the task_store for GetTask polling."""
    from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED, TASK_STATE_WORKING

    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-seq", context_id="ctx-seq")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Interim WORKING is in flight; now the engine delivers.
    bridge.signal_completion("ctx-seq", "done")
    await asyncio.wait_for(task, timeout=2.0)  # signal_completion sets the Event → execute returns

    states = [e.status.state for e in eq.events]
    assert states == [TASK_STATE_WORKING, TASK_STATE_COMPLETED]
    assert eq.events[-1].status.message.parts[0].text == "done"
    for e in eq.events:
        assert e.task_id == "task-seq"
        assert e.context_id == "ctx-seq"
