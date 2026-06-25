# SPDX-License-Identifier: Apache-2.0
"""Localhost MCP reverse-proxy.

Fronts each ACH MCP server on 127.0.0.1 so opencode points only at localhost and
NEVER sees the ``ek_`` or the real ACH endpoint. Each localhost request to
``/mcp/<id>`` is forwarded to that server's real endpoint with
``Authorization: Bearer {ek}`` ADDED, and the upstream response is streamed back
(SSE / ``text/event-stream`` safe — the body is never fully buffered).

Security: the ``ek`` lives ONLY inside the per-server handler closure. It is never
stored on an instance attribute and never logged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import aiohttp
import structlog
from aiohttp import web

from ach_agent.engine.hydrate import McpServer

log = structlog.get_logger(__name__)

# Hop-by-hop / connection-specific headers that must not be forwarded verbatim.
# Authorization is dropped from the inbound request because the proxy injects its own.
_DROP_REQUEST_HEADERS = frozenset({"host", "content-length", "authorization"})
_DROP_RESPONSE_HEADERS = frozenset({"content-length", "transfer-encoding", "content-encoding"})

_Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


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
        self._session = aiohttp.ClientSession()

        app = web.Application()
        routed: list[str] = []
        for server in servers:
            if server.id in exclude:
                continue
            handler = self._make_handler(server.endpoint, ek)
            app.router.add_route("*", f"/mcp/{server.id}", handler)
            app.router.add_route("*", f"/mcp/{server.id}/{{tail:.*}}", handler)
            routed.append(server.id)

        self._runner = web.AppRunner(app)
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

            headers = {
                k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST_HEADERS
            }
            headers["Authorization"] = f"Bearer {ek}"

            assert self._session is not None  # start() always creates it
            body = await request.read()
            async with self._session.request(
                request.method,
                target,
                headers=headers,
                params=request.query,
                data=body if body else None,
            ) as upstream:
                resp = web.StreamResponse(status=upstream.status)
                for k, v in upstream.headers.items():
                    if k.lower() not in _DROP_RESPONSE_HEADERS:
                        resp.headers[k] = v
                await resp.prepare(request)
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
                return resp

        return handler
