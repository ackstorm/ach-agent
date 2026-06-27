# SPDX-License-Identifier: Apache-2.0
"""Localhost MCP reverse-proxy.

Fronts each ACH MCP server on 127.0.0.1 so opencode points only at localhost and
NEVER sees the ``ek_`` or the real ACH endpoint. Each localhost request to
``/mcp/<id>`` is forwarded to that server's real endpoint with the ACH
``x-ach-key: {ek}`` header ADDED, and the upstream response is streamed back
(SSE / ``text/event-stream`` safe — the body is never fully buffered).

Security: the ``ek`` lives ONLY inside the per-server handler closure. It is never
stored on an instance attribute and never logged.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable

import aiohttp
import structlog
from aiohttp import web

from ach_agent.engine.hydrate import McpServer

log = structlog.get_logger(__name__)

# Hop-by-hop / connection-specific headers that must not be forwarded verbatim.
# Inbound auth headers are dropped because the proxy injects ACH's own (`x-ach-key`):
# opencode sends a dummy `Authorization` bearer; we strip it and any client-supplied
# `x-ach-key`, then add the real ek_ as `x-ach-key` (ACH's auth scheme — Bearer 401s).
_DROP_REQUEST_HEADERS = frozenset({"host", "content-length", "authorization", "x-ach-key"})
_DROP_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "content-encoding"})

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]

# Path prefixes the model proxy forwards to ACH (OpenAI / Gemini / Anthropic wires).
_MODEL_PREFIXES = ("/v1", "/gemini", "/anthropic")

# aiohttp's AppRunner defaults shutdown_timeout to 60s: cleanup() waits that long for
# in-flight handlers to finish. A proxied long-lived MCP/SSE stream (blocked in the
# upstream iter_any loop) never returns on its own, so the default makes teardown hang
# ~60s after the reply. At shutdown the reply is already delivered and opencode is dead,
# so there is nothing worth waiting for — force-close lingering streams promptly.
_SHUTDOWN_TIMEOUT_S = 1.0

# Upstream client timeout. aiohttp's ClientSession defaults to total=300s, which would abort
# any proxied MCP tool call or model/SSE stream that legitimately runs longer than 5 min —
# even though invocations are bounded at a higher layer by maxInvocationSeconds (up to 1800s).
# Disable the overall cap (total=None) and rely on that watchdog; keep a short connect timeout
# so an unreachable upstream still fails fast instead of hanging the handler.
_UPSTREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=30)


def _rpc_method(body: bytes) -> str:
    """Best-effort JSON-RPC ``method`` from an MCP request body (diagnostics only).

    Returns "" for non-JSON / non-dict bodies. Only the method NAME is extracted —
    never params/args — so no payload data is logged.
    """
    if not body:
        return ""
    try:
        parsed = json.loads(body)
    except (ValueError, TypeError):
        return ""
    return str(parsed.get("method", "")) if isinstance(parsed, dict) else ""


async def _forward(
    session: aiohttp.ClientSession,
    target: str,
    request: web.Request,
    auth_value: str,
    label: str = "proxy",
    auth_header: str = "x-ach-key",
) -> web.StreamResponse:
    """Forward ``request`` to ``target`` injecting auth, streaming the response.

    The upstream body is streamed chunk-by-chunk via a ``web.StreamResponse`` so
    SSE (``text/event-stream``) is never fully buffered. ``auth_value`` is used only
    here (caller keeps it in a closure) and is never logged. ``auth_header`` defaults
    to ACH's ``x-ach-key``; the model proxy can override it (e.g. ``Authorization`` for
    a litellm-direct bypass).
    """
    headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS}
    headers[auth_header] = auth_value

    body = await request.read()
    # Diagnostics for MCP toolCount=0 triage: the JSON-RPC method opencode sends and the
    # MCP session-id round-trip. NEVER logs the ek (injected above, never read here) nor
    # request/response bodies/params — only the method name + header presence + status.
    rpc = _rpc_method(body) if label == "mcp" else ""
    async with session.request(
        request.method,
        target,
        headers=headers,
        params=request.query,
        data=body if body else None,
    ) as upstream:
        log.debug(
            f"{label} forward",
            method=request.method,
            path=request.path,
            rpc=rpc,
            status=upstream.status,
            content_type=upstream.headers.get("Content-Type", ""),
            req_session_id=bool(request.headers.get("Mcp-Session-Id")),
            resp_session_id=bool(upstream.headers.get("Mcp-Session-Id")),
            set_cookie=bool(upstream.headers.get("Set-Cookie")),
        )
        resp = web.StreamResponse(status=upstream.status)
        for k, v in upstream.headers.items():
            if k.lower() not in _DROP_RESPONSE_HEADERS:
                resp.headers[k] = v
        try:
            await resp.prepare(request)
            async for chunk in upstream.content.iter_any():
                await resp.write(chunk)
            await resp.write_eof()
        except (ConnectionResetError, aiohttp.ClientError) as exc:
            # The client (opencode) went away mid-stream. Common at teardown: a long-lived
            # MCP poll (e.g. calendar auth_wait) outlives the opencode subprocess, so the
            # final upstream chunk lands after opencode's connection closed. Nothing left to
            # deliver — log quietly instead of dumping a traceback to stderr.
            log.debug(f"{label} client gone mid-stream", path=request.path, error=str(exc))
        return resp


class McpProxy:
    """aiohttp reverse-proxy that fronts ACH MCP servers on 127.0.0.1.

    Lifecycle::

        proxy = McpProxy()
        urls = await proxy.start(servers, ek, exclude)   # {id: "http://127.0.0.1:<port>/mcp/<id>"}
        ...
        await proxy.stop()
    """

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self, servers: list[McpServer], ek: str, exclude: set[str]) -> dict[str, str]:
        """Start the localhost proxy and return ``{server_id: localhost_url}``.

        Servers whose ``id`` is in ``exclude`` are not started and get no route.
        """
        self._session = aiohttp.ClientSession(timeout=_UPSTREAM_TIMEOUT)

        app = web.Application()
        routed: list[str] = []
        for server in servers:
            if server.id in exclude:
                continue
            handler = self._make_handler(server.endpoint, ek)
            app.router.add_route("*", f"/mcp/{server.id}", handler)
            app.router.add_route("*", f"/mcp/{server.id}/{{tail:.*}}", handler)
            routed.append(server.id)

        self._runner = web.AppRunner(app, shutdown_timeout=_SHUTDOWN_TIMEOUT_S)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()

        port = self._runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        log.info("mcp proxy started", port=port, servers=routed)
        return {sid: f"{base}/mcp/{sid}" for sid in routed}

    async def stop(self) -> None:
        """Stop the site/runner and close the shared upstream client session."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _make_handler(self, endpoint: str, ek: str) -> _Handler:
        """Build a catch-all handler that forwards to ``endpoint`` injecting the ek.

        ``ek`` is captured in this closure only — never stored on the instance.
        """
        base = endpoint.rstrip("/")

        async def handler(request: web.Request) -> web.StreamResponse:
            tail = request.match_info.get("tail", "")
            target = f"{base}/{tail}" if tail else base
            assert self._session is not None  # start() always creates it
            return await _forward(self._session, target, request, ek, label="mcp")

        return handler


class ModelProxy:
    """aiohttp reverse-proxy that fronts the ACH model wires on 127.0.0.1.

    Routes ``/v1``, ``/gemini`` and ``/anthropic`` (and their subpaths) to
    ``{ach_base_url}/<same path>`` with the ACH ``x-ach-key: {ek}`` header injected,
    streaming the response so SSE (``/v1/responses``) is never buffered.

    Lifecycle::

        proxy = ModelProxy()
        base = await proxy.start(ach_base_url, ek)   # "http://127.0.0.1:<port>"
        ...
        await proxy.stop()
    """

    def __init__(self) -> None:
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self._session: aiohttp.ClientSession | None = None

    async def start(self, base_url: str, auth_value: str, auth_header: str = "x-ach-key") -> str:
        """Start the localhost model proxy and return its base URL."""
        self._session = aiohttp.ClientSession(timeout=_UPSTREAM_TIMEOUT)
        ach_base = base_url.rstrip("/")

        app = web.Application()
        handler = self._make_handler(ach_base, auth_value, auth_header)
        for prefix in _MODEL_PREFIXES:
            app.router.add_route("*", prefix, handler)
            app.router.add_route("*", f"{prefix}/{{tail:.*}}", handler)

        self._runner = web.AppRunner(app, shutdown_timeout=_SHUTDOWN_TIMEOUT_S)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, host="127.0.0.1", port=0)
        await self._site.start()

        port = self._runner.addresses[0][1]
        base = f"http://127.0.0.1:{port}"
        log.info("model proxy started", port=port, prefixes=list(_MODEL_PREFIXES))
        return base

    async def stop(self) -> None:
        """Stop the site/runner and close the shared upstream client session."""
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    def _make_handler(self, ach_base: str, auth_value: str, auth_header: str) -> _Handler:
        """Build a handler that forwards the incoming path upstream injecting auth.

        ``auth_value`` is captured in this closure only — never stored on the instance.
        """

        async def handler(request: web.Request) -> web.StreamResponse:
            target = f"{ach_base}{request.path}"
            assert self._session is not None  # start() always creates it
            return await _forward(
                self._session, target, request, auth_value, label="model", auth_header=auth_header
            )

        return handler


# Module-level registry so ``start_model_proxy`` can keep the README-mandated
# free-function signature while remaining cleanly stoppable at shutdown.
_MODEL_PROXIES: list[ModelProxy] = []


async def start_model_proxy(base_url: str, auth_value: str, auth_header: str = "x-ach-key") -> str:
    """Start a localhost model proxy and return its base URL (no secret in it).

    The proxy instance is tracked in ``_MODEL_PROXIES`` and torn down by
    :func:`stop_model_proxies`.
    """
    proxy = ModelProxy()
    base = await proxy.start(base_url, auth_value, auth_header)
    _MODEL_PROXIES.append(proxy)
    return base


async def stop_model_proxies() -> None:
    """Stop and clear all model proxies started via :func:`start_model_proxy`."""
    while _MODEL_PROXIES:
        await _MODEL_PROXIES.pop().stop()
