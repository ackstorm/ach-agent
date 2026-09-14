from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from ach_agent.boot.channels_api import create_channels_app
from ach_agent.boot.completions import CompletionRegistry
from ach_agent.channels.client import ChannelsClient, SubmissionFailed
from ach_agent.channels.envelopes import Completion, EventEnvelope, EventRef
from ach_agent.router.router import RouterAdmitResult


def envelope(channel: str = "queue", *, key: str = "evt-1") -> EventEnvelope:
    return EventEnvelope(
        idempotency_key=key,
        session_key="lane-1",
        channel_name=channel,
        payload={"text": "hello"},
        source_trait="async_no_retry",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def client_for(app: object, *, channel: str = "queue") -> ChannelsClient:
    external = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ach-internal"
    )
    return ChannelsClient(
        "/run/ach-agent/channels/channel.sock",
        agent="agent-a",
        channel_name=channel,
        http_client=external,
    )


@pytest.mark.asyncio
async def test_submission_wait_source_config_and_duplicate_round_trip() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, agent="agent-a", channels={"queue"})
    client = await client_for(app)
    try:
        assert (await client.fetch_config()).agent_name == "agent-a"
        first = await client.submit(envelope())
        duplicate = await client.submit(envelope())
        assert first.admission.value == "accepted"
        assert duplicate.admission.value == "duplicate"
        assert first.completion is not None
        await registry.finish(first.completion.ref, {"text": "done"})
        completed = await client.wait(first.completion.ref)
        assert completed.state == "completed"
        assert completed.result == {"text": "done"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_scope_and_full_queue_are_rejected_without_internal_auth() -> None:
    async def full(_event):
        return RouterAdmitResult.FULL_QUEUE

    registry = CompletionRegistry(full, agent="agent-a")
    app = create_channels_app(registry, agent="agent-a", channels={"queue"})
    client = await client_for(app)
    try:
        result = await client.submit(envelope())
        assert result.admission.value == "full_queue"
        assert result.completion is None
        client.agent = "agent-b"
        with pytest.raises(SubmissionFailed, match="scope"):
            await client.submit(envelope())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_draining_and_registry_busy_preserve_retryable_errors() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def admit(_event):
        started.set()
        await release.wait()
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a", max_active_entries=1)
    app = create_channels_app(registry, agent="agent-a", channels={"queue"})
    first = await client_for(app)
    second = await client_for(app)
    task = asyncio.create_task(first.submit(envelope(key="first")))
    await started.wait()
    try:
        with pytest.raises(SubmissionFailed, match="active-entry limit"):
            await second.submit(envelope(key="second"))
        release.set()
        accepted = await task
        assert accepted.admission.value == "accepted"
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_malformed_completion_does_not_ack_queue_message() -> None:
    from ach_agent.channels.queue import QueueConsumer
    from tests.channels.test_queue import FakeRedis, _make_channel_cfg

    body = json.dumps({"kind": "submission", "admission": "accepted", "completion": None}).encode()

    class MalformedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(202, content=body)

    external = httpx.AsyncClient(transport=MalformedTransport(), base_url="http://ach-internal")
    client = ChannelsClient("/run/ach-agent/channels/channel.sock", channel_name="jobs", http_client=external)
    redis = FakeRedis([("1700000000000-0", {"foo": "bar"})], [])
    consumer = QueueConsumer(_make_channel_cfg(), client, redis)
    try:
        await consumer._consume_once()
        assert redis.acked == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_rejects_changed_invocation_id() -> None:
    ref = EventRef(agent="agent-a", channel_name="queue", idempotency_key="evt-1")
    first = {"kind": "completion", "completion": {"ref": ref.model_dump(), "invocation_id": "one", "state": "queued"}}
    second = {"kind": "completion", "completion": {"ref": ref.model_dump(), "invocation_id": "two", "state": "completed", "result": {"ok": True}}}

    class ChangingTransport(httpx.AsyncBaseTransport):
        calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            payload = first if self.calls == 1 else second
            return httpx.Response(200, content=json.dumps(payload).encode())

    external = httpx.AsyncClient(transport=ChangingTransport(), base_url="http://ach-internal")
    client = ChannelsClient("/run/ach-agent/channels/channel.sock", agent="agent-a", poll_interval=0, http_client=external)
    try:
        with pytest.raises(SubmissionFailed, match="invocation"):
            await client.wait(ref)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_wait_with_injected_client() -> None:
    ref = EventRef(agent="agent-a", channel_name="queue", idempotency_key="evt-1")

    class PendingTransport(httpx.AsyncBaseTransport):
        started = asyncio.Event()
        cancelled = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise

    transport = PendingTransport()
    external = httpx.AsyncClient(transport=transport, base_url="http://ach-internal")
    client = ChannelsClient("/run/ach-agent/channels/channel.sock", agent="agent-a", http_client=external)
    waiter = asyncio.create_task(client.wait(ref))
    await transport.started.wait()
    await client.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert transport.cancelled
    assert not external.is_closed


@pytest.mark.asyncio
async def test_maximum_retained_result_survives_wire_margin() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, agent="agent-a", channels={"queue"})
    client = await client_for(app)
    try:
        accepted = await client.submit(envelope())
        assert accepted.completion is not None
        base = Completion(ref=accepted.completion.ref, invocation_id=accepted.completion.invocation_id, state="completed", result={"blob": ""})
        blob_size = 1 * 1024 * 1024 - len(base.model_dump_json().encode())
        large_result = {"blob": "x" * blob_size}
        await registry.finish(accepted.completion.ref, large_result)
        assert len(registry.lookup(accepted.completion.ref).model_dump_json().encode()) == 1 * 1024 * 1024
        assert (await client.wait(accepted.completion.ref)).result == large_result
    finally:
        await client.close()
