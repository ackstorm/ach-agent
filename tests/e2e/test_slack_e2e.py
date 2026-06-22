"""Slack end-to-end test (CHN-03) — Plan 04-02 GREEN.

Full-harness e2e: MockSlackAdapter → shim translation → router → engine → reply via send().

Architecture (hermetic — no live Slack, no credentials):
  - MockSlackAdapter (conftest.py): replaces the real SlackAdapter, captures send() calls,
    fires inbound events via inject_inbound()
  - fake_engine_runner: returns a known reply action (mirrors test_gitlab_e2e.py pattern)
  - delivery_done asyncio.Event + asyncio.timeout(5.0): no naked polling loops (CLAUDE.md)

Threat T-04-05: SEC sentinel asserts fake SLACK_BOT_TOKEN never appears in captured logs.
"""

from __future__ import annotations

import asyncio
from io import StringIO
from typing import Any

import pytest
import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.slack import _make_slack_shim
from ach_agent.config.schema import ChannelConfig
from ach_agent.engine.sanitized_env import redact_ek_processor
from ach_agent.router import Router
from ach_agent.router.dedup import InMemoryDedupStore
from tests.e2e.conftest import MockSlackAdapter

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REPLY_TEXT = "Hello from the engine!"
_SLACK_BOT_TOKEN_SENTINEL = "xoxb-fake-test-sentinel-do-not-log"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_slack_channel_cfg(name: str = "slack-test") -> ChannelConfig:
    return ChannelConfig.model_validate({"name": name, "type": "slack"})


def _configure_json_logging_to(stream: StringIO) -> None:
    """Configure structlog to emit JSON to a StringIO for SEC assertion."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(0),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=stream),
    )


class FakePool:
    """Minimal EnginePool stand-in with engine_has_been_ready_once=True."""

    engine_has_been_ready_once: bool = True


# ---------------------------------------------------------------------------
# CHN-03 e2e happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_message_routes_to_engine_and_delivers_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CHN-03 e2e: Slack inbound message → governed pipeline → out-of-band reply via send()."""
    monkeypatch.setenv("SLACK_BOT_TOKEN", _SLACK_BOT_TOKEN_SENTINEL)

    # Capture log output for SEC-03 / T-04-05 assertion
    log_stream = StringIO()
    _configure_json_logging_to(log_stream)

    channel_cfg = _make_slack_channel_cfg("slack-e2e")
    mock_adapter = MockSlackAdapter()
    delivery_done: asyncio.Event = asyncio.Event()

    # Patch send() to signal delivery
    original_send = mock_adapter.send

    async def signaling_send(chat_id: str, content: str, **kwargs: Any) -> None:
        await original_send(chat_id, content, **kwargs)
        delivery_done.set()

    mock_adapter.send = signaling_send  # type: ignore[method-assign]

    # Build engine_runner: delivers reply via adapter.send() out-of-band
    async def fake_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        chat_id = event.payload.get("chat_id", "")
        await mock_adapter.send(chat_id, _REPLY_TEXT)
        on_kill()

    pool = FakePool()
    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine_runner,
        delivery_adapter=None,
    )

    # Wire shim into mock adapter (mirrors connect_slack_adapter logic without real connection)
    shim = _make_slack_shim(handler=router, pool=pool, channel_cfg=channel_cfg)
    mock_adapter.set_message_handler(shim)

    # Inject an inbound Slack message
    await mock_adapter.inject_inbound(
        text="Hello agent!",
        channel_id="C001",
        ts="1717000000.000001",
    )

    # Wait for engine to deliver the reply (bounded — no naked polling)
    try:
        async with asyncio.timeout(5.0):
            await delivery_done.wait()
    except TimeoutError:
        pytest.fail("CHN-03 e2e: timed out waiting for Slack reply delivery")

    # Assert the reply was delivered to the correct channel
    assert len(mock_adapter.sent_messages) >= 1, "Expected at least one send() call"
    msg = mock_adapter.sent_messages[0]
    assert msg["text"] == _REPLY_TEXT, f"Reply text mismatch: {msg['text']!r}"
    assert msg["chat_id"] == "C001", f"chat_id mismatch: {msg['chat_id']!r}"

    # T-04-05 / SEC: SLACK_BOT_TOKEN sentinel must not appear in logs
    log_output = log_stream.getvalue()
    assert _SLACK_BOT_TOKEN_SENTINEL not in log_output, (
        f"T-04-05: SLACK_BOT_TOKEN sentinel found in harness log output:\n{log_output[:500]}"
    )


# ---------------------------------------------------------------------------
# CHN-03/IDM-01: dedup rejects repeated ts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_dedup_rejects_repeated_ts() -> None:
    """CHN-03/IDM-01: duplicate message ts → deduplicated (router drops second)."""
    channel_cfg = _make_slack_channel_cfg("slack-dedup")
    mock_adapter = MockSlackAdapter()
    delivery_count = 0

    async def counting_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        nonlocal delivery_count
        delivery_count += 1
        on_kill()

    pool = FakePool()
    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=counting_engine_runner,
        delivery_adapter=None,
    )

    shim = _make_slack_shim(handler=router, pool=pool, channel_cfg=channel_cfg)
    mock_adapter.set_message_handler(shim)

    # Inject same ts twice
    await mock_adapter.inject_inbound(
        text="First message", channel_id="C002", ts="1717000000.000002"
    )
    # Brief yield to let the first event route through
    await asyncio.sleep(0.05)

    await mock_adapter.inject_inbound(
        text="Duplicate message", channel_id="C002", ts="1717000000.000002"
    )
    await asyncio.sleep(0.05)

    # Engine should only be called once (dedup drops the second)
    assert delivery_count == 1, (
        f"IDM-01: engine called {delivery_count} times; expected exactly 1 (dedup must drop duplicate ts)"
    )


# ---------------------------------------------------------------------------
# D-03: different threads → different lane session_keys
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_slack_thread_lane_isolates_sessions() -> None:
    """D-03: messages in different threads → different lane session_keys."""
    channel_cfg = _make_slack_channel_cfg("slack-lanes")
    mock_adapter = MockSlackAdapter()
    seen_session_keys: list[str] = []
    both_done: asyncio.Event = asyncio.Event()

    async def capturing_engine_runner(event: MessageEvent, on_kill: Any) -> None:
        seen_session_keys.append(event.session_key)
        if len(seen_session_keys) >= 2:
            both_done.set()
        on_kill()

    pool = FakePool()
    router = Router(
        max_concurrent_invocations=2,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=capturing_engine_runner,
        delivery_adapter=None,
    )

    shim = _make_slack_shim(handler=router, pool=pool, channel_cfg=channel_cfg)
    mock_adapter.set_message_handler(shim)

    # Two messages in different threads (different ts so not deduplicated)
    await mock_adapter.inject_inbound(
        text="Thread A", channel_id="C003", thread_ts="1717000001.000001", ts="1717000002.000001"
    )
    await mock_adapter.inject_inbound(
        text="Thread B", channel_id="C003", thread_ts="1717000001.000002", ts="1717000002.000002"
    )

    try:
        async with asyncio.timeout(5.0):
            await both_done.wait()
    except TimeoutError:
        pytest.fail("D-03: timed out waiting for both lane events to be processed")

    # Both session keys must differ (different threads → different lanes)
    assert len(seen_session_keys) == 2, f"Expected 2 events, got {len(seen_session_keys)}"
    assert seen_session_keys[0] != seen_session_keys[1], (
        f"D-03: session_keys must differ for different threads: {seen_session_keys}"
    )
    # session_key format: channel_id:thread_ts
    assert "C003:1717000001.000001" in seen_session_keys
    assert "C003:1717000001.000002" in seen_session_keys
