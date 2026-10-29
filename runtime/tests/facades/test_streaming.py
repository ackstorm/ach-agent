# SPDX-License-Identifier: MIT
"""facades/_proxy.py::forward — real incremental streaming and upstream/client cleanup.

A real `asyncio.start_server` loopback TCP upstream speaking just enough raw HTTP/1.1
chunked framing, driven by a real (non-mocked) `httpx.AsyncClient`, plus direct ASGI
send/disconnect events against the facade's own `Request`/`StreamingResponse` objects —
the existing `httpx.ASGITransport` aggregate-byte tests (`test_secrets.py`) read the whole
body at once and cannot by themselves distinguish real incremental streaming from a fully
buffered response sent in one shot. This file is that missing proof (review focus 3).
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from starlette.requests import Request
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive

from ach_runtime.facades._proxy import ProcessIdentity, _send_until_headers_or_disconnect
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"fixture-key"
IDENTITY = ProcessIdentity(agent="classifier", environment="platform")


async def _owned(candidate: str) -> bool:
    return candidate == "owned-ws"


def _scope(*, token: str, method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": f"/t/{token}/chat/completions",
        "raw_path": f"/t/{token}/chat/completions".encode(),
        "query_string": b"",
        "headers": [],
        "path_params": {"token": token, "tail": "chat/completions"},
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }


def _request_receive(body: bytes = b"") -> tuple[Receive, "asyncio.Queue[Message]"]:
    queue: asyncio.Queue[Message] = asyncio.Queue()
    queue.put_nowait({"type": "http.request", "body": body, "more_body": False})

    async def receive() -> Message:
        return await queue.get()

    return receive, queue


def _mcp_scope(*, token: str, method: str = "POST") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "path": f"/t/{token}/mcp/m1",
        "raw_path": f"/t/{token}/mcp/m1".encode(),
        "query_string": b"",
        "headers": [],
        "path_params": {"token": token, "server_id": "m1"},
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }


class _RawChunkedUpstream:
    """A real TCP server that writes raw HTTP/1.1 chunked-transfer-encoding chunks on
    demand, so a test can observe exactly when a byte left the wire."""

    def __init__(self) -> None:
        self.server: asyncio.base_events.Server | None = None
        self.port = 0
        self.connection_closed = asyncio.Event()
        self._writer: asyncio.StreamWriter | None = None

    async def start(
        self,
        on_connected: Callable[[], Awaitable[None]] | None = None,
        *,
        send_headers: bool = True,
    ) -> None:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            self._writer = writer
            await reader.readline()  # request line
            while True:
                line = await reader.readline()
                if line in (b"\r\n", b""):
                    break
            if send_headers:
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Mcp-Session-Id: sess-xyz\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"\r\n"
                )
                await writer.drain()
            if on_connected is not None:
                await on_connected()
            try:
                # Detect the client (our forward()) closing its end — proof that
                # `upstream.aclose()` ran, not just that the test stopped reading.
                data = await reader.read(1)
                if data == b"":
                    self.connection_closed.set()
            except (ConnectionResetError, BrokenPipeError):
                self.connection_closed.set()

        self.server = await asyncio.start_server(handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def write_chunk(self, data: bytes) -> None:
        assert self._writer is not None
        self._writer.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        await self._writer.drain()

    async def finish(self) -> None:
        assert self._writer is not None
        self._writer.write(b"0\r\n\r\n")
        await self._writer.drain()

    async def stop(self) -> None:
        # `handle()`'s pending `reader.read(1)` otherwise keeps its connection task alive
        # forever on a still-pooled client connection, which `wait_closed()` would then
        # wait on — close our own end first so that pending read sees EOF and returns.
        if self._writer is not None:
            self._writer.close()
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()


def _facade(upstream_port: int, *, client: httpx.AsyncClient | None = None) -> ModelFacade:
    return ModelFacade(
        endpoint=f"http://127.0.0.1:{upstream_port}/v1",
        headers={"x-ach-key": "ek-xyz"},
        key=KEY,
        owned_workspace=_owned,
        identity=IDENTITY,
        client=client,
    )


async def _cancel_and_join(task: "asyncio.Task[Any]", *, timeout: float = 2.0) -> None:
    """Cancel `task` if still pending and wait for it to actually finish, bounded, so a
    test's cleanup never leaves a dangling task behind a bare `.cancel()` call."""
    if not task.done():
        task.cancel()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), timeout=timeout)


def _model_streaming_facade(
    upstream_port: int, *, client: httpx.AsyncClient | None = None
) -> tuple[ModelFacade, Callable[..., dict[str, Any]]]:
    return _facade(upstream_port, client=client), _scope


def _mcp_streaming_facade(
    upstream_port: int, *, client: httpx.AsyncClient | None = None
) -> tuple[McpFacade, Callable[..., dict[str, Any]]]:
    facade = McpFacade(
        servers={"m1": f"http://127.0.0.1:{upstream_port}/mcp"},
        server_headers={"m1": {"x-ach-key": "ek-xyz"}},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(),
        exclude_tools=frozenset(),
        identity=IDENTITY,
        client=client,
    )
    return facade, _mcp_scope


async def test_first_asgi_body_arrives_before_the_gated_second_chunk_is_released() -> None:
    upstream = _RawChunkedUpstream()
    second_chunk_requested = asyncio.Event()

    async def on_connected() -> None:
        await upstream.write_chunk(b"data: one\n\n")

    await upstream.start(on_connected=on_connected)
    facade = _facade(upstream.port)
    token = facade_token(KEY, "owned-ws")

    request_receive, _ = _request_receive()
    response = await facade._handle(Request(_scope(token=token), request_receive))
    assert isinstance(response, StreamingResponse)

    events: list[Message] = []
    first_body = asyncio.Event()

    async def send(message: Message) -> None:
        events.append(message)
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    async def never_disconnects() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    stream_task = asyncio.create_task(response(_scope(token=token), never_disconnects, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=2)
        assert not second_chunk_requested.is_set(), "first body must arrive before the second chunk is even sent"

        second_chunk_requested.set()
        await upstream.write_chunk(b"data: two\n\n")
        await upstream.finish()
        await asyncio.wait_for(stream_task, timeout=2)
    finally:
        await _cancel_and_join(stream_task)
        await upstream.stop()
        await facade.aclose()

    bodies = [m["body"] for m in events if m["type"] == "http.response.body"]
    joined = b"".join(bodies)
    assert joined.index(b"data: one") < joined.index(b"data: two")

    start = next(m for m in events if m["type"] == "http.response.start")
    assert start["status"] == 200
    header_map = {name.decode().lower(): value.decode() for name, value in start["headers"]}
    assert "content-length" not in header_map
    assert "transfer-encoding" not in header_map
    assert "content-encoding" not in header_map
    assert header_map["content-type"] == "text/event-stream"


async def test_disconnect_closes_tracked_upstream_and_owned_task_completes_within_two_seconds() -> None:
    upstream = _RawChunkedUpstream()
    first_sent = asyncio.Event()

    async def on_connected() -> None:
        await upstream.write_chunk(b"data: hanging\n\n")
        first_sent.set()

    await upstream.start(on_connected=on_connected)
    # No keepalive pooling: a real upstream close has to be visible as an actual TCP
    # close on our loopback socket, not just a response handed back to httpx's pool.
    no_pool_client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade = _facade(upstream.port, client=no_pool_client)
    token = facade_token(KEY, "owned-ws")

    request_receive, _ = _request_receive()
    response = await facade._handle(Request(_scope(token=token), request_receive))
    assert isinstance(response, StreamingResponse)

    first_body = asyncio.Event()
    disconnect_queue: asyncio.Queue[Message] = asyncio.Queue()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    async def receive_with_disconnect() -> Message:
        return await disconnect_queue.get()

    stream_task = asyncio.create_task(response(_scope(token=token), receive_with_disconnect, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=2)

        disconnect_queue.put_nowait({"type": "http.disconnect"})
        await asyncio.wait_for(stream_task, timeout=2)
        await asyncio.wait_for(upstream.connection_closed.wait(), timeout=2)
    finally:
        await _cancel_and_join(stream_task)
        await upstream.stop()
        await facade.aclose()
        await no_pool_client.aclose()

    assert upstream.connection_closed.is_set()


# --- Facade pre-header cancellation correction: disconnect/caller-cancel before headers ---


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_preheader_disconnect_closes_upstream_within_two_seconds(make_facade: Any) -> None:
    """A downstream disconnect that arrives while still waiting on upstream response
    headers must close the real upstream socket and cancel the owned send, not wait for
    headers that are never coming (review focus 1). The handler returns 499 instead of
    raising, so the server does not log an unhandled exception."""
    upstream = _RawChunkedUpstream()
    connected = asyncio.Event()

    async def on_connected() -> None:
        connected.set()

    await upstream.start(on_connected=on_connected, send_headers=False)
    no_pool_client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade, scope = make_facade(upstream.port, client=no_pool_client)
    token = facade_token(KEY, "owned-ws")
    request_receive, queue = _request_receive()

    handle_task = asyncio.create_task(facade._handle(Request(scope(token=token), request_receive)))
    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        assert not handle_task.done(), "handler must still be waiting on upstream headers"

        queue.put_nowait({"type": "http.disconnect"})

        response = await asyncio.wait_for(handle_task, timeout=2)
        assert response.status_code == 499
        await asyncio.wait_for(upstream.connection_closed.wait(), timeout=2)
    finally:
        await _cancel_and_join(handle_task)
        await upstream.stop()
        await facade.aclose()
        await no_pool_client.aclose()


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_preheader_caller_cancellation_closes_upstream(make_facade: Any) -> None:
    """Caller-side cancellation while still waiting on upstream response headers must close
    the real upstream socket and leave no pending owned handler behind (review focus 4)."""
    upstream = _RawChunkedUpstream()
    connected = asyncio.Event()

    async def on_connected() -> None:
        connected.set()

    await upstream.start(on_connected=on_connected, send_headers=False)
    no_pool_client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade, scope = make_facade(upstream.port, client=no_pool_client)
    token = facade_token(KEY, "owned-ws")
    request_receive, _ = _request_receive()

    handle_task = asyncio.create_task(facade._handle(Request(scope(token=token), request_receive)))
    try:
        await asyncio.wait_for(connected.wait(), timeout=2)
        assert not handle_task.done(), "handler must still be waiting on upstream headers"

        handle_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(handle_task, timeout=2)
        await asyncio.wait_for(upstream.connection_closed.wait(), timeout=2)

        assert facade._responses == set()
    finally:
        await _cancel_and_join(handle_task)
        await upstream.stop()
        await facade.aclose()
        await no_pool_client.aclose()


class _RecordingByteStream(httpx.AsyncByteStream):
    """A stream whose `aclose()` records how many times it actually closed, so a test can
    assert the response was closed exactly once rather than merely "truthy"."""

    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


class _ImmediateSendClient:
    """A `client.send()` stand-in that returns an already-built response with no `await`
    inside its body, so its task completes on its very first scheduling step — same as the
    disconnect listener's — letting both land in `asyncio.wait`'s `done` set together."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def send(self, request: httpx.Request, *, stream: bool = False) -> httpx.Response:
        return self._response


async def test_preheader_simultaneous_headers_and_disconnect_closes_response() -> None:
    """When the upstream header wait and the downstream disconnect listener finish in the
    same `asyncio.wait` pass, the disconnect must win: the helper must not hand back a
    response the caller will never read, and the already-open upstream stream must still
    close exactly once (review focus 2)."""
    stream = _RecordingByteStream()
    upstream_request = httpx.Request("GET", "http://upstream.test")
    response = httpx.Response(200, request=upstream_request, stream=stream)
    client = _ImmediateSendClient(response)
    token = facade_token(KEY, "owned-ws")

    async def receive() -> Message:
        return {"type": "http.disconnect"}

    request = Request(_scope(token=token), receive)

    assert await _send_until_headers_or_disconnect(client, upstream_request, request) is None  # type: ignore[arg-type]

    assert stream.close_count == 1


async def test_injected_client_stays_open_after_aclose_internally_owned_client_closes() -> None:
    upstream = _RawChunkedUpstream()
    await upstream.start()
    try:
        injected = httpx.AsyncClient()
        owning_facade = _facade(upstream.port, client=None)
        injected_facade = _facade(upstream.port, client=injected)

        await owning_facade.aclose()
        assert owning_facade._client.is_closed is True

        await injected_facade.aclose()
        assert injected.is_closed is False
        await injected.aclose()
    finally:
        await upstream.stop()


async def test_mcp_facade_also_exposes_aclose_for_its_internally_owned_client() -> None:
    facade = McpFacade(
        servers={"m1": "https://ach.example.com/mcp/m1"},
        server_headers={"m1": {"x-ach-key": "ek-xyz"}},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(),
        exclude_tools=frozenset(),
        identity=IDENTITY,
    )
    await facade.aclose()
    assert facade._client.is_closed is True


# --- Important2: internal-client timeout policy ---------------------------------


def test_model_facade_internal_client_has_no_read_timeout_and_thirty_second_connect_bound() -> None:
    facade = ModelFacade(
        endpoint="https://ach.example.com/v1", headers={"x-ach-key": "ek-xyz"}, key=KEY, owned_workspace=_owned, identity=IDENTITY
    )
    timeout = facade._client.timeout
    assert timeout.read is None
    assert timeout.connect == 30.0


def test_model_facade_injected_client_timeout_is_preserved() -> None:
    custom = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    facade = ModelFacade(
        endpoint="https://ach.example.com/v1",
        headers={"x-ach-key": "ek-xyz"},
        key=KEY,
        owned_workspace=_owned,
        identity=IDENTITY,
        client=custom,
    )
    assert facade._client.timeout.read == 5.0


def test_mcp_facade_internal_client_has_no_read_timeout_and_thirty_second_connect_bound() -> None:
    facade = McpFacade(
        servers={"m1": "https://ach.example.com/mcp/m1"},
        server_headers={"m1": {"x-ach-key": "ek-xyz"}},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(),
        exclude_tools=frozenset(),
        identity=IDENTITY,
    )
    timeout = facade._client.timeout
    assert timeout.read is None
    assert timeout.connect == 30.0


def test_mcp_facade_injected_client_timeout_is_preserved() -> None:
    custom = httpx.AsyncClient(timeout=httpx.Timeout(5.0))
    facade = McpFacade(
        servers={"m1": "https://ach.example.com/mcp/m1"},
        server_headers={"m1": {"x-ach-key": "ek-xyz"}},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(),
        exclude_tools=frozenset(),
        identity=IDENTITY,
        client=custom,
    )
    assert facade._client.timeout.read == 5.0


async def test_mcp_facade_app_lifespan_shutdown_closes_owned_client() -> None:
    facade = McpFacade(
        servers={"m1": "https://ach.example.com/mcp/m1"},
        server_headers={"m1": {"x-ach-key": "ek-xyz"}},
        key=KEY,
        owned_workspace=_owned,
        exclude=frozenset(),
        exclude_tools=frozenset(),
        identity=IDENTITY,
    )
    app = facade.app()
    lifespan_queue: asyncio.Queue[Message] = asyncio.Queue()
    lifespan_queue.put_nowait({"type": "lifespan.startup"})
    lifespan_queue.put_nowait({"type": "lifespan.shutdown"})
    sent: list[Message] = []

    async def receive() -> Message:
        return await lifespan_queue.get()

    async def send(message: Message) -> None:
        sent.append(message)

    await app({"type": "lifespan"}, receive, send)

    assert {"type": "lifespan.startup.complete"} in sent
    assert {"type": "lifespan.shutdown.complete"} in sent
    assert facade._client.is_closed is True


# --- Finding 1: ASGI response-start failure must close upstream immediately ------


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_response_start_failure_closes_upstream_before_body_iteration(make_facade: Any) -> None:
    """A failing `send(http.response.start)` happens before body iteration ever starts, so
    the body generator's own `finally` never runs — without a wrapper around the whole ASGI
    call, the upstream response and its tracking entry leak until the facade's next
    `aclose()` (review finding 1)."""
    upstream = _RawChunkedUpstream()
    await upstream.start()
    injected = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade, scope = make_facade(upstream.port, client=injected)
    try:
        token = facade_token(KEY, "owned-ws")
        request_receive, _ = _request_receive()
        response = await facade._handle(Request(scope(token=token), request_receive))
        assert isinstance(response, StreamingResponse)
        assert len(facade._responses) == 1
        tracked = next(iter(facade._responses))
        assert tracked.is_closed is False

        close_calls = 0
        original_aclose = tracked.aclose

        async def counted_aclose() -> None:
            nonlocal close_calls
            close_calls += 1
            await original_aclose()

        tracked.aclose = counted_aclose  # type: ignore[method-assign]

        async def failing_send(message: Message) -> None:
            if message["type"] == "http.response.start":
                raise RuntimeError("boom: send failed before body iteration")

        async def never_disconnects() -> Message:
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        with pytest.raises(RuntimeError, match="boom"):
            await response(scope(token=token), never_disconnects, failing_send)

        assert tracked.is_closed is True
        assert close_calls == 1
        assert facade._responses == set()
        assert injected.is_closed is False
    finally:
        await upstream.stop()
        await facade.aclose()
        await injected.aclose()


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_aclose_attempts_every_response_close_and_propagates_a_real_failure(make_facade: Any) -> None:
    """With two outstanding (never-iterated) responses where one's close raises, `aclose()`
    must still attempt the other response's close and the owned client's close, and
    propagate the actual failure rather than silently claiming a clean shutdown."""
    upstream = _RawChunkedUpstream()
    await upstream.start()
    facade, scope = make_facade(upstream.port)
    try:
        token = facade_token(KEY, "owned-ws")
        for _ in range(2):
            request_receive, _ = _request_receive()
            response = await facade._handle(Request(scope(token=token), request_receive))
            assert isinstance(response, StreamingResponse)
        assert len(facade._responses) == 2
        failing, healthy = list(facade._responses)

        async def _raise() -> None:
            raise RuntimeError("close failed")

        failing.aclose = _raise  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="close failed"):
            await facade.aclose()

        assert healthy.is_closed is True
        assert facade._responses == set()
        assert facade._client.is_closed is True
    finally:
        await upstream.stop()


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_aclose_preserves_injected_client_even_when_a_response_close_fails(make_facade: Any) -> None:
    """An injected client is never owned by the facade — it must stay open even when a
    tracked response's close raises and the facade's own `aclose()` propagates that failure."""
    upstream = _RawChunkedUpstream()
    await upstream.start()
    injected = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade, scope = make_facade(upstream.port, client=injected)
    try:
        token = facade_token(KEY, "owned-ws")
        request_receive, _ = _request_receive()
        response = await facade._handle(Request(scope(token=token), request_receive))
        assert isinstance(response, StreamingResponse)
        tracked = next(iter(facade._responses))

        async def _raise() -> None:
            raise RuntimeError("close failed")

        tracked.aclose = _raise  # type: ignore[method-assign]

        with pytest.raises(RuntimeError, match="close failed"):
            await facade.aclose()

        assert injected.is_closed is False
    finally:
        await upstream.stop()
        await injected.aclose()


# --- Important1: tracked response bookkeeping + lifespan shutdown ---------------


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_aclose_closes_a_response_whose_streaming_body_never_started(make_facade: Any) -> None:
    """A response can be created (upstream `send()` succeeded) and then never actually
    iterated by the ASGI layer (e.g. an ASGI response-start failure) — `aclose()` must
    still close it, not rely on the body generator's own `finally`."""
    upstream = _RawChunkedUpstream()
    await upstream.start()
    try:
        facade, scope = make_facade(upstream.port)
        token = facade_token(KEY, "owned-ws")
        request_receive, _ = _request_receive()
        response = await facade._handle(Request(scope(token=token), request_receive))
        assert isinstance(response, StreamingResponse)
        assert len(facade._responses) == 1
        tracked = next(iter(facade._responses))
        assert tracked.is_closed is False

        await facade.aclose()

        assert tracked.is_closed is True
        assert facade._responses == set()
    finally:
        await upstream.stop()


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_app_lifespan_shutdown_closes_owned_client_and_outstanding_responses(make_facade: Any) -> None:
    upstream = _RawChunkedUpstream()
    await upstream.start()
    try:
        facade, scope = make_facade(upstream.port)
        token = facade_token(KEY, "owned-ws")
        request_receive, _ = _request_receive()
        response = await facade._handle(Request(scope(token=token), request_receive))
        assert isinstance(response, StreamingResponse)
        tracked = next(iter(facade._responses))

        app = facade.app()
        lifespan_queue: asyncio.Queue[Message] = asyncio.Queue()
        lifespan_queue.put_nowait({"type": "lifespan.startup"})
        lifespan_queue.put_nowait({"type": "lifespan.shutdown"})
        sent: list[Message] = []

        async def receive() -> Message:
            return await lifespan_queue.get()

        async def send(message: Message) -> None:
            sent.append(message)

        await app({"type": "lifespan"}, receive, send)

        assert {"type": "lifespan.startup.complete"} in sent
        assert {"type": "lifespan.shutdown.complete"} in sent
        assert facade._client.is_closed is True
        assert tracked.is_closed is True
    finally:
        await upstream.stop()


@pytest.mark.parametrize("make_facade", [_model_streaming_facade, _mcp_streaming_facade])
async def test_app_lifespan_shutdown_closes_outstanding_responses_but_not_injected_client(
    make_facade: Any,
) -> None:
    upstream = _RawChunkedUpstream()
    await upstream.start()
    try:
        injected = httpx.AsyncClient()
        facade, scope = make_facade(upstream.port, client=injected)
        token = facade_token(KEY, "owned-ws")
        request_receive, _ = _request_receive()
        response = await facade._handle(Request(scope(token=token), request_receive))
        assert isinstance(response, StreamingResponse)
        tracked = next(iter(facade._responses))

        app = facade.app()
        lifespan_queue: asyncio.Queue[Message] = asyncio.Queue()
        lifespan_queue.put_nowait({"type": "lifespan.startup"})
        lifespan_queue.put_nowait({"type": "lifespan.shutdown"})

        async def receive() -> Message:
            return await lifespan_queue.get()

        async def send(message: Message) -> None:
            pass

        await app({"type": "lifespan"}, receive, send)

        assert tracked.is_closed is True
        assert injected.is_closed is False
        await injected.aclose()
    finally:
        await upstream.stop()


async def test_explicit_task_cancellation_closes_tracked_response_within_two_seconds() -> None:
    upstream = _RawChunkedUpstream()

    async def on_connected() -> None:
        await upstream.write_chunk(b"data: hanging\n\n")

    await upstream.start(on_connected=on_connected)
    no_pool_client = httpx.AsyncClient(limits=httpx.Limits(max_keepalive_connections=0))
    facade = _facade(upstream.port, client=no_pool_client)
    token = facade_token(KEY, "owned-ws")

    request_receive, _ = _request_receive()
    response = await facade._handle(Request(_scope(token=token), request_receive))
    assert isinstance(response, StreamingResponse)

    first_body = asyncio.Event()

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body" and message.get("body"):
            first_body.set()

    async def never_disconnects() -> Message:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    stream_task = asyncio.create_task(response(_scope(token=token), never_disconnects, send))
    try:
        await asyncio.wait_for(first_body.wait(), timeout=2)
        assert len(facade._responses) == 1
        tracked = next(iter(facade._responses))

        stream_task.cancel()
        await asyncio.wait_for(asyncio.gather(stream_task, return_exceptions=True), timeout=2)
        await asyncio.wait_for(upstream.connection_closed.wait(), timeout=2)

        assert tracked.is_closed is True
        assert facade._responses == set()
    finally:
        await _cancel_and_join(stream_task)
        await upstream.stop()
        await facade.aclose()
        await no_pool_client.aclose()
