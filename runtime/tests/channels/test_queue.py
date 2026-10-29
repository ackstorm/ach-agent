from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from ach_runtime.channels.event import RoutedEvent
from ach_runtime.config.schema import ChannelConfig


def _channel() -> ChannelConfig:
    return ChannelConfig.model_validate({
        "name": "jobs",
        "type": "queue",
        "queue": {"type": "redis", "key": "ach:jobs"},
        "routing": {"sessionKey": "main"},
    })


class FakeRedis:
    def __init__(self) -> None:
        self.pending: list[tuple[str, dict[str, str] | None]] = [("1-0", None), ("2-0", {"task": "a"})]
        self.new: list[tuple[str, dict[str, str] | None]] = [("3-0", {"task": "b"})]
        self.acked: list[str] = []
        self.groups: list[tuple[str, str, str, bool]] = []
        self.reads: list[tuple[str, str]] = []

    async def xgroup_create(self, name, groupname, id="0", mkstream=False):
        self.groups.append((name, groupname, id, mkstream))

    async def xreadgroup(self, groupname, consumername, streams, count=None, block=None):
        stream, cursor = next(iter(streams.items()))
        self.reads.append((consumername, cursor))
        if cursor == ">":
            batch, self.new = self.new, []
        else:
            batch, self.pending = self.pending, []
        return [(stream, batch)] if batch else []

    async def xack(self, name, groupname, *ids):
        self.acked.extend(map(str, ids))
        return len(ids)

    async def aclose(self):
        return None


@pytest.mark.asyncio
async def test_queue_pending_recovery_ack_after_started() -> None:
    from ach_runtime.channels.queue import QueueConsumer

    client = FakeRedis()
    started = asyncio.get_running_loop().create_future()
    admitted: list[RoutedEvent] = []

    class Admission:
        def admit(self, routed):
            admitted.append(routed)
            return SimpleNamespace(started=started)

    consumer = QueueConsumer(_channel(), SimpleNamespace(admission=Admission()), client)
    consume = asyncio.create_task(consumer._consume_once())
    for _ in range(100):
        if admitted:
            break
        await asyncio.sleep(0)
    assert client.groups == []  # _consume_once is also the legacy direct test seam
    assert [item.event.event_id for item in admitted] == ["2-0"]
    assert client.acked == ["1-0"]  # stale PEL entry has no stream body
    assert "2-0" not in client.acked  # no XACK before the mini Started boundary
    started.set_result(None)
    await consume
    assert client.acked == ["1-0", "2-0", "3-0"]
    assert admitted[0].event.source == "queue"
    assert admitted[0].event.payload == {"task": "a"}
    assert client.reads == [("c1", "0"), ("c1", ">")]


@pytest.mark.asyncio
async def test_queue_full_and_started_failure_leave_entries_pending() -> None:
    from ach_runtime.channels.queue import QueueConsumer

    class Admission:
        def __init__(self, failure: Exception):
            self.failure = failure

        def admit(self, routed):
            raise self.failure

    class Redis:
        def __init__(self):
            self.acked: list[str] = []

        async def xack(self, *_args):
            self.acked.append(str(_args[-1]))

    redis = Redis()
    consumer = QueueConsumer(_channel(), SimpleNamespace(admission=Admission(RuntimeError("full"))), redis)
    await consumer._handle_message("4-0", {"task": "full"})
    assert redis.acked == []

    started = asyncio.get_running_loop().create_future()
    started.set_exception(RuntimeError("workspace failed before Started"))

    class StartedAdmission:
        def admit(self, routed):
            return SimpleNamespace(started=started)

    consumer = QueueConsumer(_channel(), SimpleNamespace(admission=StartedAdmission()), redis)
    await consumer._handle_message("5-0", {"task": "failed"})
    assert redis.acked == []


@pytest.mark.asyncio
async def test_queue_duplicate_started_acks_each_delivery_once() -> None:
    from ach_runtime.channels.queue import QueueConsumer

    started = asyncio.get_running_loop().create_future()
    started.set_result(None)
    redis = FakeRedis()
    redis.pending = []
    redis.new = []
    calls: list[RoutedEvent] = []

    class Admission:
        def admit(self, routed):
            calls.append(routed)
            return SimpleNamespace(started=started)

    consumer = QueueConsumer(_channel(), SimpleNamespace(admission=Admission()), redis)
    await consumer._handle_message("6-0", {"task": "same"})
    await consumer._handle_message("6-0", {"task": "same"})
    assert len(calls) == 2
    assert redis.acked == ["6-0", "6-0"]


@pytest.mark.asyncio
async def test_queue_start_uses_legacy_group_consumer_and_redis_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from ach_runtime.channels import queue

    assert queue._DEFAULT_REDIS_URL == "redis://localhost:6379"
    assert queue._CONSUMER_NAME == "c1"
    client = FakeRedis()
    monkeypatch.delenv("REDIS_URL", raising=False)
    import redis.asyncio

    seen: list[str] = []
    monkeypatch.setattr(redis.asyncio, "from_url", lambda url, **_kwargs: seen.append(url) or client)
    consumer = queue.QueueConsumer(_channel(), SimpleNamespace(admission=object()))
    await consumer.start()
    await consumer.stop()
    assert seen == ["redis://localhost:6379"]
    assert client.groups == [("ach:jobs", "ach-jobs", "0", True)]
