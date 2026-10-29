# SPDX-License-Identifier: MIT
"""Shared reverse-proxy core for `facades/{model,mcp,memory}.py` — the ONE place every
outbound header/auth/identity/correlation decision is made (reused from old-ach-agent
`engine/mcp_proxy.py::_forward`, `identity.py` and `engine/trace.py`; task-7-reuse-handoff.md).

Authentication is the accepted Workspace HMAC (`harness/tokens.py::authenticate_facade`),
verified by each facade handler before this module is ever reached — the capability/
resolver/clock/deadline machinery that used to live here is gone; it was never
contract-required (contract §5/§11) and the root-reviewed correction removes it rather
than adapting it.

`ProcessIdentity`/`with_identity_headers` are copied from old-ach-agent `identity.py`
WITHOUT its module-level `_current`/`configure()` global: the caller already knows its
own identity (`config.agent.name`/`config.identity_environment`) and passes it explicitly, so
there is nothing to commit process-wide. `is_correlation_header`/`inject_meta` are copied
from old-ach-agent `engine/trace.py`; the per-token correlation REGISTRY that function used
to read is replaced by an explicit `correlation: Mapping[str,str] | None` snapshot the
caller supplies per request (no registry, no token lifecycle to own here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

# ---------------------------------------------------------------------------
# Identity — copied old-ach-agent identity.py, no global current/configure state.
# ---------------------------------------------------------------------------

_IDENTITY_HEADER_NAMES = frozenset({"x-ach-agent", "x-ach-environment"})


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    agent: str
    environment: str


def with_identity_headers(headers: Mapping[str, str], identity: ProcessIdentity) -> dict[str, str]:
    """Replace any caller identity with exactly one authoritative header pair."""
    result = {
        key: value for key, value in headers.items() if key.lower() not in _IDENTITY_HEADER_NAMES
    }
    result["x-ach-agent"] = identity.agent
    result["x-ach-environment"] = identity.environment
    return result


# ---------------------------------------------------------------------------
# Correlation — copied old-ach-agent engine/trace.py's predicate + _meta injector.
# ---------------------------------------------------------------------------

_LITELLM_SESSION_HEADER = "x-litellm-session-id"
_LITELLM_TRACE_HEADER = "x-litellm-trace-id"


def is_correlation_header(name: str) -> bool:
    """True for a header that could claim a trace/session the harness did not grant."""
    lowered = name.lower()
    return (
        lowered.startswith("langfuse_")
        or lowered in ("traceparent", "tracestate")
        or lowered in (_LITELLM_SESSION_HEADER, _LITELLM_TRACE_HEADER)
    )


def inject_meta(body: bytes, carrier: Mapping[str, str]) -> bytes:
    """Put `carrier` into a JSON-RPC request message's `params._meta` (additive to headers).

    Anything that is not a JSON-RPC request object (invalid JSON, non-dict, no `method` —
    i.e. a response or a batch) is returned untouched: correlation must never break a call.
    """
    if not body or not carrier:
        return body
    try:
        message = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(message, dict) or "method" not in message:
        return body
    params = message.get("params")
    if not isinstance(params, dict):
        params = {}
        message["params"] = params
    meta = params.get("_meta")
    if not isinstance(meta, dict):
        meta = {}
        params["_meta"] = meta
    meta.update(carrier)
    return json.dumps(message).encode("utf-8")


# ---------------------------------------------------------------------------
# Forwarding — the single place every outbound header is decided.
# ---------------------------------------------------------------------------

# Inbound auth is always stripped and replaced with the Harness-held credential: the engine
# sends a dummy Authorization/x-ach-key/x-goog-api-key, never the real one (old-ach-agent
# invariant). x-ach-agent/x-ach-environment are also dropped here (belt-and-suspenders —
# with_identity_headers strips them too); correlation headers are matched separately via
# `is_correlation_header` below since that set is open-ended (a `langfuse_*` prefix), not a
# fixed name.
DROP_REQUEST_HEADERS = frozenset(
    {
        "host",
        "content-length",
        # httpx then sends only the encodings it can decode (`aiter_bytes`/`aread`).
        "accept-encoding",
        "transfer-encoding",
        "authorization",
        "x-ach-key",
        "x-goog-api-key",
        "x-api-key",
        "x-ach-agent",
        "x-ach-environment",
    }
)
# RFC 9110 hop-by-hop fields are per-connection: never forwarded upstream.
HOP_BY_HOP_HEADERS = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "upgrade",
    }
)
DROP_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "content-encoding"})


def join_tail(base: str, tail: str) -> str | None:
    """`base` plus the client-controlled path `tail`, or None when the tail climbs out of
    `base`'s path. httpx normalizes literal `..`/`.` segments (also once Starlette has
    decoded `%2e%2e`), so the prefix is checked on the URL httpx will actually send; a dot
    segment still present after decoding was double-encoded (`%252e%252e`) and is refused
    too, since the upstream may decode it once more."""
    target = f"{base}/{tail}" if tail else base
    base_path = httpx.URL(base).path.rstrip("/")
    path = httpx.URL(target).path
    if any(segment in (".", "..") for segment in path.split("/")):
        return None
    return target if path == base_path or path.startswith(base_path + "/") else None


class _UpstreamGuardedStreamingResponse(StreamingResponse):
    """Closes the upstream response even if ASGI `send` fails before body iteration starts
    (e.g. `http.response.start` raises): without this, the body generator's own `finally`
    never runs since the generator itself is never iterated, so the upstream response and
    its `_responses` tracking entry would leak until the owning facade's next `aclose()`."""

    def __init__(self, *args: Any, cleanup: Callable[[], Awaitable[None]], **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._cleanup = cleanup

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await self._cleanup()


async def _wait_for_disconnect(request: Request) -> None:
    while True:
        message = await request.receive()
        if message["type"] == "http.disconnect":
            return


async def _send_until_headers_or_disconnect(
    client: httpx.AsyncClient, upstream_request: httpx.Request, request: Request
) -> httpx.Response | None:
    """Race the upstream header wait against the downstream client disconnecting, so a
    client that leaves before headers arrive does not leave the owned upstream socket
    open for a response nobody will ever read. Returns None when the client left first. Cancelling `client.send()` mid-flight runs
    httpx's own `except BaseException` cleanup, which closes the real connection since the
    h11 request/response cycle never reached DONE/DONE. Whichever side loses the race is
    cancelled and joined before this returns — no watcher survives past this call."""
    send_task = asyncio.create_task(client.send(upstream_request, stream=True))
    listener_task = asyncio.create_task(_wait_for_disconnect(request))
    response: httpx.Response | None = None
    try:
        done, _ = await asyncio.wait({send_task, listener_task}, return_when=asyncio.FIRST_COMPLETED)
        if listener_task in done:
            # Disconnect wins even if the send also finished in this same pass: a response
            # the caller already left is not handed back, and the finally below still closes
            # it via send_task's own cleanup. listener_task.result() is not swallowed — an
            # unexpected failure there (not a plain disconnect) propagates as-is.
            listener_task.result()
            return None
        response = send_task.result()
        return response
    finally:
        if response is None:
            send_task.cancel()
            with contextlib.suppress(BaseException):
                leaked = await send_task
                if leaked is not None:
                    await leaked.aclose()
        if not listener_task.done():
            listener_task.cancel()
        with contextlib.suppress(BaseException):
            await listener_task


async def forward(
    client: httpx.AsyncClient,
    target: str,
    request: Request,
    inject_headers: dict[str, str],
    *,
    identity: ProcessIdentity,
    correlation: Mapping[str, str] | None = None,
    mcp: bool = False,
    body: bytes | None = None,
    transform: Callable[[bytes], bytes] | None = None,
    _responses: set[httpx.Response] | None = None,
) -> Response:
    """Forward `request` to `target`, streaming the response body chunk-by-chunk so SSE
    (`text/event-stream`) is never fully buffered.

    `inject_headers` are the caller's upstream credentials (e.g. `{"x-ach-key": ek}`); they replace any inbound header of the same name, case-insensitively; `identity` is
    always stamped on, replacing any caller-supplied value; `correlation` is an optional
    trusted snapshot — its absence strips caller correlation and adds none, never a failure.
    `mcp=True` additionally injects `correlation` into a JSON-RPC request body's
    `params._meta` (SEP-414 style) since one streamable-HTTP session multiplexes many
    messages and a transport header alone would glue them all under the first request.

    `body` overrides the request's own body when the caller already rewrote it (e.g.
    `facades/memory.py` injecting `scope`/`project_slug` into a `tools/call`).

    `transform` rewrites the response body before it is sent. It buffers the whole body,
    so it is for small, finite replies only (e.g. an MCP `tools/list`), never a stream.

    `_responses` is a private, caller-owned tracking set (one per facade instance) that
    this response is added to immediately after the upstream send succeeds and removed
    from on cleanup — it lets the owning facade's `aclose()`/lifespan shutdown close a
    response whose body was never iterated by the ASGI layer (unstarted generators never
    run their own `finally`). Not a public interface; each facade owns exactly one set.
    """
    injected = {name.lower() for name in inject_headers}
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in DROP_REQUEST_HEADERS
        and key.lower() not in HOP_BY_HOP_HEADERS
        and key.lower() not in injected
        and not is_correlation_header(key)
    }
    headers.update(inject_headers)
    headers = with_identity_headers(headers, identity)
    if correlation:
        headers.update(correlation)

    if body is None:
        body = await request.body()
    if mcp:
        body = inject_meta(body, correlation or {})

    upstream_request = client.build_request(
        request.method,
        target,
        headers=headers,
        params=request.query_params,
        content=body or None,
    )
    upstream = await _send_until_headers_or_disconnect(client, upstream_request, request)
    if upstream is None:
        # The client left before upstream headers (e.g. OpenCode closing an idle MCP GET
        # stream): nothing to send, but return normally so the server does not log it as
        # an unhandled exception.
        return Response(status_code=499)
    if _responses is not None:
        _responses.add(upstream)

    async def _cleanup() -> None:
        # httpx `aclose()` is idempotent, so the body generator's own `finally` and this
        # wrapper's `finally` can both call it unconditionally without double-close errors;
        # a genuine close failure still propagates rather than being silently claimed.
        await upstream.aclose()
        if _responses is not None:
            _responses.discard(upstream)

    async def body_iter() -> AsyncIterator[bytes]:
        try:
            # Decoded bytes: Content-Encoding is dropped above, so raw gzip would reach the
            # client unannounced.
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await _cleanup()

    response_headers = {
        key: value for key, value in upstream.headers.items() if key.lower() not in DROP_RESPONSE_HEADERS
    }
    if transform is not None:
        try:
            content = transform(await upstream.aread())
        finally:
            await _cleanup()
        return Response(content, status_code=upstream.status_code, headers=response_headers)
    return _UpstreamGuardedStreamingResponse(
        body_iter(), status_code=upstream.status_code, headers=response_headers, cleanup=_cleanup
    )


__all__ = [
    "DROP_REQUEST_HEADERS",
    "DROP_RESPONSE_HEADERS",
    "HOP_BY_HOP_HEADERS",
    "ProcessIdentity",
    "forward",
    "inject_meta",
    "is_correlation_header",
    "join_tail",
    "with_identity_headers",
]
