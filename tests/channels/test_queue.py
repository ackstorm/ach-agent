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
from prometheus_client import REGISTRY

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
    """In-memory fake of redis.asyncio's consumer-group stream API.

    Models exactly what QueueConsumer relies on: an ordered stream history
    (id, fields — fields=None/{} models a deleted/trimmed entry), one consumer
    group with a single implicit consumer (this harness always reads/acks as
    "c1"), and that consumer's pending-entries list (PEL) — entries move into
    the PEL on delivery via ">" (new) OR when re-read via an explicit id
    cursor (recovery), and out of it via xack. A fresh QueueConsumer built
    against the SAME FakeRedis instance models a restart: stream history and
    the PEL persist across it, exactly like a real redis server would.
    """

    def __init__(self, messages: list[tuple[str, dict[str, str] | None]], order: list[str]) -> None:
        self._stream: list[tuple[str, dict[str, str] | None]] = list(messages)
        self._order = order
        self._next_new_idx = 0
        # id -> fields, insertion-ordered (== id order, since ids only ever
        # arrive in increasing order in these tests) — this consumer's PEL.
        self._pending: dict[str, dict[str, str] | None] = {}
        self.groups_created: list[tuple[str, str]] = []
        self.acked: list[str] = []

    def add_message(self, message_id: str, fields: dict[str, str] | None) -> None:
        """Append a new entry to the stream (simulates traffic arriving later)."""
        self._stream.append((message_id, fields))

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
        stream = next(iter(streams))
        cursor = streams[stream]
        limit = count if count is not None else len(self._stream) + len(self._pending)

        if cursor == ">":
            batch = self._stream[self._next_new_idx : self._next_new_idx + limit]
            self._next_new_idx += len(batch)
            for message_id, fields in batch:
                self._pending[message_id] = fields
            return [(stream, batch)] if batch else []

        # Recovery read: entries in MY OWN pel with id > cursor, in id order.
        ids = [mid for mid in self._pending if mid > cursor][:limit]
        batch = [(mid, self._pending[mid]) for mid in ids]
        return [(stream, batch)] if batch else []

    async def xack(self, name: str, groupname: str, *ids: str) -> int:
        self._order.append("xack")
        for message_id in ids:
            self._pending.pop(message_id, None)
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


def _inbound(channel: str, type_: str) -> float:
    """Current ach_agent_channel_inbound_events_total for one (channel, type) pair."""
    return (
        REGISTRY.get_sample_value(
            "ach_agent_channel_inbound_events_total", {"channel": channel, "type": type_}
        )
        or 0.0
    )


@pytest.mark.asyncio
async def test_queue_dispatches_event_with_message_id() -> None:
    """handle() receives idempotency_key == redis message id (str) + channel_name."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [("1700000000000-0", {"foo": "bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.ACCEPTED)
    channel_cfg = _make_channel_cfg("jobs", "ach:jobs")

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    before = _inbound("jobs", "queue")
    await consumer._consume_once()

    assert len(handler.events) == 1, "Expected exactly one event emitted"
    assert _inbound("jobs", "queue") == before + 1
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
    messages = [("1700000000000-0", {"foo": "bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.ACCEPTED)
    channel_cfg = _make_channel_cfg()

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer._consume_once()

    assert fake_redis.acked == ["1700000000000-0"], "message must be acked after processing"
    # Order: handle MUST precede xack (onComplete semantics).
    assert order == ["handle", "xack"], f"handle must run before xack, got order={order!r}"


@pytest.mark.asyncio
async def test_queue_no_ack_when_handler_raises() -> None:
    """If handle() raises, xack is NOT called — message stays pending for redelivery."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    messages = [("1700000000000-0", {"foo": "bar"})]
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
    messages = [("1700000000000-0", {"foo": "bar"})]
    fake_redis = FakeRedis(messages, order)
    handler = FakeHandler(order, RouterAdmitResult.FULL_QUEUE)
    channel_cfg = _make_channel_cfg()

    consumer = QueueConsumer(channel_cfg, handler=handler, redis_client=fake_redis)
    await consumer._consume_once()

    assert fake_redis.acked == ["1700000000000-0"], (
        "FULL_QUEUE on async_no_retry must ack+drop (cron parity)"
    )


@pytest.mark.asyncio
async def test_queue_recovers_pending_entry_after_restart() -> None:
    """finding 6: a message left pending by a failed dispatch (handler raised,
    never acked) is recovered by the next consumer instance sharing the same
    redis group/consumer identity — not stuck forever because the old code
    only ever read new (">") messages. The recovered entry's id is preserved,
    a new message arriving in the same pass is still processed, and a further
    pass does not replay anything already acknowledged."""
    from ach_agent.channels.queue import QueueConsumer

    order: list[str] = []
    fake_redis = FakeRedis([("1700000000000-0", {"n": "a"})], order)
    channel_cfg = _make_channel_cfg()

    # First "process": handler raises, so message A is delivered but never acked
    # — it stays in the consumer's pending-entries list (PEL).
    failing_handler = FakeHandler(order, raises=True)
    consumer1 = QueueConsumer(channel_cfg, handler=failing_handler, redis_client=fake_redis)
    await consumer1._consume_once()
    assert fake_redis.acked == [], "A must stay pending after the failed dispatch"

    # "Restart": a fresh QueueConsumer against the SAME fake redis (same group +
    # consumer identity persists there); a new message B arrives meanwhile.
    fake_redis.add_message("1700000000001-0", {"n": "b"})
    ok_handler = FakeHandler(order, RouterAdmitResult.ACCEPTED)
    consumer2 = QueueConsumer(channel_cfg, handler=ok_handler, redis_client=fake_redis)
    await consumer2._consume_once()

    ids = [e.idempotency_key for e in ok_handler.events]
    assert "1700000000000-0" in ids, (
        "the recovered (previously-pending) message must be reprocessed"
    )
    assert "1700000000001-0" in ids, "a new message must still be processed in the same pass"
    assert sorted(fake_redis.acked) == ["1700000000000-0", "1700000000001-0"]

    # A further pass must not replay anything already acknowledged.
    ok_handler.events.clear()
    await consumer2._consume_once()
    assert ok_handler.events == [], "already-acknowledged entries must not be replayed"


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
