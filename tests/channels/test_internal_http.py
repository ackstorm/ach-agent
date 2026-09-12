from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from ach_agent.boot.channels_api import create_channels_app
from ach_agent.boot.completions import CompletionRegistry
from ach_agent.channels.client import ChannelsClient, SubmissionFailed
from ach_agent.channels.envelopes import EventEnvelope, EventRef
from ach_agent.channels.signing import NonceCache
from ach_agent.router.router import RouterAdmitResult

KEY = b"channel-harness-key"


def envelope(channel: str = "queue") -> EventEnvelope:
    return EventEnvelope(
        idempotency_key="evt-1",
        session_key="lane-1",
        channel_name=channel,
        payload={"text": "hello"},
        source_trait="async_no_retry",
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


async def _client_for(app: FastAPI, *, channel: str = "queue") -> ChannelsClient:
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://harness")
    return ChannelsClient(
        "http://harness",
        KEY,
        agent="agent-a",
        channel_name=channel,
        http_client=http,
    )


@pytest.mark.asyncio
async def test_signed_submission_and_wait_round_trip() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    client = await _client_for(app)
    try:
        accepted = await client.submit(envelope())
        assert accepted.completion is not None
        assert accepted.completion.state == "queued"
        await registry.finish(accepted.completion.ref, {"text": "done"})
        completed = await client.wait(accepted.completion.ref)
        assert completed.state == "completed"
        assert completed.result == {"text": "done"}
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_same_event_retry_keeps_identity_but_uses_fresh_nonce(monkeypatch) -> None:
    seen: list[str] = []

    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})

    class RecordingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["x-ach-request-nonce"])
            return await httpx.ASGITransport(app=app).handle_async_request(request)

    http = httpx.AsyncClient(transport=RecordingTransport(), base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, agent="agent-a", http_client=http)
    try:
        first = await client.submit(envelope())
        retry = await client.submit(envelope())
        assert first.completion is not None and retry.completion is not None
        assert first.completion.ref == retry.completion.ref
        assert seen[0] != seen[1]
        assert retry.completion.invocation_id == first.completion.invocation_id
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_signed_full_queue_is_returned_as_authenticated_admission() -> None:
    async def admit(_event):
        return RouterAdmitResult.FULL_QUEUE

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    client = await _client_for(app)
    try:
        result = await client.submit(envelope())
        assert result.admission.value == "full_queue"
        assert result.completion is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_forged_response_never_becomes_authenticated_submission() -> None:
    async def admit(_event):
        return RouterAdmitResult.FULL_QUEUE

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})

    class ForgingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            real = await httpx.ASGITransport(app=app).handle_async_request(request)
            body = await real.aread()
            return httpx.Response(
                real.status_code,
                headers={"content-type": "application/json"},
                content=body,
            )

    http = httpx.AsyncClient(transport=ForgingTransport(), base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, agent="agent-a", http_client=http)
    try:
        with pytest.raises(SubmissionFailed):
            await client.submit(envelope())
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_scope_mismatch_is_signed_rejection() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    client = await _client_for(app, channel="other")
    try:
        with pytest.raises(SubmissionFailed, match="scope"):
            await client.submit(envelope("other"))
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_same_raw_id_on_two_configured_channels_has_distinct_registry_identity() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue", "other"})
    queue_client = await _client_for(app, channel="queue")
    other_client = await _client_for(app, channel="other")
    try:
        queue_submission = await queue_client.submit(envelope("queue"))
        other_submission = await other_client.submit(envelope("other"))
        assert queue_submission.completion is not None
        assert other_submission.completion is not None
        assert queue_submission.completion.ref == EventRef(
            agent="agent-a", channel_name="queue", idempotency_key="evt-1"
        )
        assert other_submission.completion.ref == EventRef(
            agent="agent-a", channel_name="other", idempotency_key="evt-1"
        )
        assert (
            queue_submission.completion.invocation_id
            != other_submission.completion.invocation_id
        )
    finally:
        await queue_client.close()
        await other_client.close()


@pytest.mark.asyncio
async def test_mismatched_agent_cannot_submit_to_or_read_configured_channel() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    client = await _client_for(app, channel="queue")
    client.agent = "agent-b"
    try:
        with pytest.raises(SubmissionFailed, match="scope"):
            await client.submit(envelope())
        with pytest.raises(SubmissionFailed, match="scope"):
            await client.wait(
                EventRef(agent="agent-b", channel_name="queue", idempotency_key="evt-1")
            )
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_injected_empty_nonce_cache_is_used_and_saturates() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    cache = NonceCache(max_entries=1, clock=lambda: 100.0)
    app = create_channels_app(
        registry,
        KEY,
        agent="agent-a",
        channels={"queue"},
        nonce_cache=cache,
    )
    transport = httpx.ASGITransport(app=app)
    http = httpx.AsyncClient(transport=transport, base_url="http://harness")
    client = ChannelsClient(
        "http://harness", KEY, agent="agent-a", http_client=http, clock=lambda: 100.0
    )
    try:
        await client.submit(envelope())
        with pytest.raises(SubmissionFailed, match="saturated"):
            await client.submit(envelope())
    finally:
        await client.close()
