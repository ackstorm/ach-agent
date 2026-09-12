from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime

import httpx
import pytest
from fastapi import FastAPI

from ach_agent.boot.channels_api import create_channels_app
from ach_agent.boot.completions import CompletionRegistry
from ach_agent.channels.client import ChannelsClient, SubmissionFailed
from ach_agent.channels.envelopes import Completion, EventEnvelope, EventRef
from ach_agent.channels.signing import NonceCache, request_mac, response_mac
from ach_agent.router.router import RouterAdmitResult

KEY = b"channel-harness-key"


def envelope(channel: str = "queue", *, key: str = "evt-1") -> EventEnvelope:
    return EventEnvelope(
        idempotency_key=key,
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
        assert await client.probe_harness()
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
async def test_draining_h_rejects_signed_retryable_admission() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    app.extra["state"].draining = True
    client = await _client_for(app)
    try:
        with pytest.raises(SubmissionFailed, match="draining"):
            await client.submit(envelope())
        assert registry.lookup(envelope().event_ref("agent-a")).invocation_id == ""
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
            queue_submission.completion.invocation_id != other_submission.completion.invocation_id
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


@pytest.mark.asyncio
async def test_signed_accepted_without_completion_does_not_ack_queue_message() -> None:
    from ach_agent.channels.queue import QueueConsumer
    from tests.channels.test_queue import FakeRedis, _make_channel_cfg

    body = json.dumps(
        {"kind": "submission", "admission": "accepted", "completion": None},
        separators=(",", ":"),
    ).encode()

    class MalformedTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            nonce = request.headers["x-ach-request-nonce"]
            return httpx.Response(
                202,
                headers={"X-ACH-Response-MAC": response_mac(KEY, nonce, 202, body)},
                content=body,
            )

    http = httpx.AsyncClient(transport=MalformedTransport(), base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, channel_name="jobs", http_client=http)
    redis = FakeRedis([("1700000000000-0", {"foo": "bar"})], [])
    consumer = QueueConsumer(_make_channel_cfg(), client, redis)
    try:
        await consumer._consume_once()
        assert redis.acked == []
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_wait_rejects_a_changed_invocation_id() -> None:
    ref = EventRef(agent="agent-a", channel_name="queue", idempotency_key="evt-1")
    first = {
        "kind": "completion",
        "completion": {"ref": ref.model_dump(), "invocation_id": "one", "state": "queued"},
    }
    second = {
        "kind": "completion",
        "completion": {
            "ref": ref.model_dump(),
            "invocation_id": "two",
            "state": "completed",
            "result": {"ok": True},
        },
    }

    class ChangingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.calls += 1
            payload = first if self.calls == 1 else second
            body = json.dumps(payload, separators=(",", ":")).encode()
            nonce = request.headers["x-ach-request-nonce"]
            return httpx.Response(
                200,
                headers={"X-ACH-Response-MAC": response_mac(KEY, nonce, 200, body)},
                content=body,
            )

    transport = ChangingTransport()
    http = httpx.AsyncClient(transport=transport, base_url="http://harness")
    client = ChannelsClient(
        "http://harness", KEY, agent="agent-a", poll_interval=0, http_client=http
    )
    try:
        with pytest.raises(SubmissionFailed, match="invocation"):
            await client.wait(ref)
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_wait_with_external_http_client() -> None:
    ref = EventRef(agent="agent-a", channel_name="queue", idempotency_key="evt-1")
    completion = {"ref": ref.model_dump(), "invocation_id": "one", "state": "queued"}
    body = json.dumps(
        {"kind": "completion", "completion": completion}, separators=(",", ":")
    ).encode()

    class PendingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.cancelled = False

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            self.started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.cancelled = True
                raise
            return httpx.Response(200, content=body)

    transport = PendingTransport()
    external = httpx.AsyncClient(transport=transport, base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, agent="agent-a", http_client=external)
    waiter = asyncio.create_task(client.wait(ref))
    await transport.started.wait()
    await client.close()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert transport.cancelled
    assert not external.is_closed


@pytest.mark.asyncio
async def test_response_stream_over_limit_is_rejected_before_signature_parse() -> None:
    class OversizeStream(httpx.AsyncByteStream):
        def __init__(self) -> None:
            self.consumed = 0
            self.closed = False

        async def __aiter__(self):
            for chunk in (b"a" * (512 * 1024), b"b" * (512 * 1024), b"c" * (512 * 1024), b"d"):
                self.consumed += 1
                yield chunk

        async def aclose(self) -> None:
            self.closed = True

    class HugeTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.stream = OversizeStream()

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, stream=self.stream)

    transport = HugeTransport()
    external = httpx.AsyncClient(transport=transport, base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, http_client=external)
    try:
        with pytest.raises(SubmissionFailed, match="too large"):
            await client.submit(envelope())
        assert transport.stream.consumed == 3
        assert transport.stream.closed
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_app_rejects_tampered_target_body_and_replays() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    transport = httpx.ASGITransport(app=app)
    external = httpx.AsyncClient(transport=transport, base_url="http://harness")
    body = json.dumps(
        {"agent": "agent-a", "event": envelope().model_dump(mode="json")},
        separators=(",", ":"),
    ).encode()
    timestamp = int(time.time())
    nonce = "fixed-nonce"

    def signed_headers(
        request_nonce: str, signed_target: str, signed_body: bytes
    ) -> dict[str, str]:
        return {
            "X-ACH-Request-Timestamp": str(timestamp),
            "X-ACH-Request-Nonce": request_nonce,
            "X-ACH-Request-MAC": request_mac(
                KEY, "POST", signed_target, timestamp, request_nonce, signed_body
            ),
        }

    headers = signed_headers(nonce, "/internal/v1/events", body)
    try:
        first = await external.post("/internal/v1/events", content=body, headers=headers)
        replay = await external.post("/internal/v1/events", content=body, headers=headers)
        tampered_target = await external.post(
            "/internal/v1/events?x=1",
            content=body,
            headers=signed_headers("target", "/internal/v1/events", body),
        )
        tampered_body = await external.post(
            "/internal/v1/events",
            content=body + b" ",
            headers=signed_headers("body", "/internal/v1/events", body),
        )
        assert first.status_code == 202
        assert replay.status_code == 401
        assert tampered_target.status_code == 401
        assert tampered_body.status_code == 401
    finally:
        await external.aclose()


@pytest.mark.asyncio
async def test_registry_busy_is_signed_retryable_failure_distinct_from_full_queue() -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def admit(_event):
        started.set()
        await release.wait()
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a", max_active_entries=1)
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    first = await _client_for(app)
    second = await _client_for(app)
    first_task = asyncio.create_task(first.submit(envelope(key="first")))
    await started.wait()
    try:
        with pytest.raises(SubmissionFailed, match="active-entry limit"):
            await second.submit(envelope(key="second"))
        release.set()
        accepted = await first_task
        assert accepted.admission.value == "accepted"
    finally:
        release.set()
        await asyncio.gather(first_task, return_exceptions=True)
        await first.close()
        await second.close()


@pytest.mark.asyncio
async def test_maximum_retained_result_survives_signed_wire_envelope_margin() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})

    class RecordingTransport(httpx.AsyncBaseTransport):
        def __init__(self) -> None:
            self.response_size = 0

        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            response = await httpx.ASGITransport(app=app).handle_async_request(request)
            body = await response.aread()
            self.response_size = len(body)
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=body,
            )

    transport = RecordingTransport()
    external = httpx.AsyncClient(transport=transport, base_url="http://harness")
    client = ChannelsClient("http://harness", KEY, agent="agent-a", http_client=external)
    try:
        accepted = await client.submit(envelope())
        assert accepted.completion is not None
        base = Completion(
            ref=accepted.completion.ref,
            invocation_id=accepted.completion.invocation_id,
            state="completed",
            result={"blob": ""},
        )
        max_result_bytes = 1 * 1024 * 1024
        blob_size = max_result_bytes - len(base.model_dump_json().encode())
        large_result = {"blob": "x" * blob_size}
        await registry.finish(accepted.completion.ref, large_result)
        retained = registry.lookup(accepted.completion.ref)
        assert len(retained.model_dump_json().encode()) == max_result_bytes
        completed = await client.wait(accepted.completion.ref)
        assert completed.state == "completed"
        assert completed.result == large_result
        assert transport.response_size > max_result_bytes
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_close_during_polling_cancels_wait_but_keeps_admitted_work() -> None:
    async def admit(_event):
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit, agent="agent-a")
    app = create_channels_app(registry, KEY, agent="agent-a", channels={"queue"})
    lookup_seen = asyncio.Event()

    class ObservingTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/internal/v1/results":
                lookup_seen.set()
            return await httpx.ASGITransport(app=app).handle_async_request(request)

    external = httpx.AsyncClient(transport=ObservingTransport(), base_url="http://harness")
    client = ChannelsClient(
        "http://harness",
        KEY,
        agent="agent-a",
        http_client=external,
        poll_interval=60,
    )
    try:
        accepted = await client.submit(envelope())
        assert accepted.completion is not None
        waiter = asyncio.create_task(client.wait(accepted.completion.ref))
        await lookup_seen.wait()
        await client.close()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert registry.lookup(accepted.completion.ref).state == "queued"
        assert registry.active_count == 1
        assert not external.is_closed
    finally:
        await client.close()
        await external.aclose()
