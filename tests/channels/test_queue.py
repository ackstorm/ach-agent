"""Queue channel runtime unit tests (CHN, redis stream consumer, ackMode:onComplete).

Fast suite — drives a single consume iteration deterministically via the internal
`_consume_once()` helper, against an in-memory fake redis client (no live redis).

Verifies:
  - handler.handle is called with idempotency_key == <redis message id> (as str)
    and channel_name == channel_cfg.name (CONTRACT §6.1: id is the message id).
  - onComplete ack semantics: xack happens ONLY AFTER handle() returns.
  - On handler raising, xack is NOT called for that message (stays pending).
"""

from __future__ import annotations

from typing import Any

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.router import RouterAdmitResult


class FakeHandler:
    """Captures emitted MessageEvents and returns a configurable result.

    Records the global call-order sequence (shared with the fake redis client)
    so tests can assert that handle() runs BEFORE xack().
    """

    def __init__(
        self,
        order: list[str],
        result: RouterAdmitResult = RouterAdmitResult.ACCEPTED,
        raises: bool = False,
    ) -> None:
        self._order = order
        self._result = result
        self._raises = raises
        self.events: list[MessageEvent] = []

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        self._order.append("handle")
        if self._raises:
            raise RuntimeError("boom")
        return self._result


class FakeRedis:
    """Minimal in-memory fake of redis.asyncio client for stream consumption.

    Exposes async xgroup_create, xreadgroup, xack. xreadgroup returns the queued
    messages once, then empty lists thereafter (so the loop would block/idle).
    """

    def __init__(self, messages: list[tuple[bytes, dict[bytes, bytes]]], order: list[str]) -> None:
        self._messages = messages
        self._order = order
        self.groups_created: list[tuple[str, str]] = []
        self.acked: list[bytes] = []
        self._drained = False

    async def xgroup_create(
        self, name: str, groupname: str, id: str = "0", mkstream: bool = False
    ) -> bool:
        self.groups_created.append((name, groupname))
        return True

    async def xreadgroup(
        self,
        groupname: str,
        consumername: str,
        streams: dict[str, str],
        count: int | None = None,
        block: int | None = None,
    ) -> list[Any]:
        if self._drained:
            return []
        self._drained = True
        stream = next(iter(streams))
        return [(stream.encode(), list(self._messages))]

    async def xack(self, name: str, groupname: str, *ids: bytes) -> int:
        self._order.append("xack")
        self.acked.extend(ids)
        return len(ids)

    async def aclose(self) -> None:
        return None


def _make_channel_cfg(name: str = "jobs", key: str = "ach:jobs") -> Any:
    """Build a minimal ChannelConfig for a queue channel."""
    from ach_agent.config.schema import ChannelConfig

    raw = {
        "name": name,
        "type": "queue",
        "queue": {"type": "redis", "key": key, "ackMode": "onComplete"},
    }
    return ChannelConfig.model_validate(raw)


@pytest.mark.asyncio
async def test_queue_dispatches_event_with_message_id() -> None:
    """handle() receives idempotency_key == redis message id (str) + channel_name."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [(b"1700000000000-0", {b"foo": b"bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.ACCEPTED)
    channel_cfg = _make_channel_cfg("jobs", "ach:jobs")

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer._consume_once()

    assert len(handler.events) == 1, "Expected exactly one event emitted"
    event = handler.events[0]
    assert event.idempotency_key == "1700000000000-0", (
        f"idempotency_key must be the redis message id, got {event.idempotency_key!r}"
    )
    assert event.idempotency_key != "", "idempotency_key MUST never be empty (CONTRACT §6.1)"
    assert event.channel_name == "jobs"
    assert event.source_trait == "async_no_retry"


@pytest.mark.asyncio
async def test_queue_acks_only_after_handle_returns() -> None:
    """onComplete: xack is called ONLY AFTER handle() returns (processed)."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [(b"1700000000000-0", {b"foo": b"bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.ACCEPTED)
    channel_cfg = _make_channel_cfg()

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer._consume_once()

    assert fake_redis.acked == [b"1700000000000-0"], "message must be acked after processing"
    # Order: handle MUST precede xack (onComplete semantics).
    assert order == ["handle", "xack"], f"handle must run before xack, got order={order!r}"


@pytest.mark.asyncio
async def test_queue_no_ack_when_handler_raises() -> None:
    """If handle() raises, xack is NOT called — message stays pending for redelivery."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [(b"1700000000000-0", {b"foo": b"bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, raises=True)
    channel_cfg = _make_channel_cfg()

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    # One bad message must not propagate out of the consume iteration.
    await consumer._consume_once()

    assert fake_redis.acked == [], "message must NOT be acked when handler raises (stays pending)"
    assert "xack" not in order, "xack must not run when handle() raises"


@pytest.mark.asyncio
async def test_queue_full_queue_acks_and_drops() -> None:
    """FULL_QUEUE on async_no_retry → ack+drop (parity with cron drop-on-full)."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [(b"1700000000000-0", {b"foo": b"bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.FULL_QUEUE)
    channel_cfg = _make_channel_cfg()

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer._consume_once()

    assert fake_redis.acked == [b"1700000000000-0"], (
        "FULL_QUEUE on async_no_retry must ack+drop (cron parity)"
    )


@pytest.mark.asyncio
async def test_queue_ensures_group_on_start() -> None:
    """start() ensures the consumer group exists (XGROUP CREATE ... MKSTREAM)."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    fake_redis = FakeRedis([], order)
    handler = FakeHandler(order)
    channel_cfg = _make_channel_cfg("jobs", "ach:jobs")

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer.start()
    await consumer.stop()

    assert fake_redis.groups_created == [("ach:jobs", "ach-jobs")], (
        f"consumer group must be created on the stream key, got {fake_redis.groups_created!r}"
    )
