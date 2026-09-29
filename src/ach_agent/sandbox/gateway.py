# SPDX-License-Identifier: Apache-2.0
"""One pod-network port in front of the loopback facades, for sandboxed engines.

Facades (model proxy, McpProxy, memory, repo checkout, a2a) stay bound to 127.0.0.1 and
unchanged. A sandbox reaches them only through this relay:

    [/t/<trace>]/s/<facade-token>/<port>/<tail>  →  http://127.0.0.1:<port>[/t/<trace>]/<tail>

The facade token is HMAC-derived per sandbox (sandbox.tokens) and verified on every request,
unlike the ``/t/<trace>`` correlation token, which the engine pool mints and no facade checks.
Only registered facade ports are reachable. The relay injects no credential: the facade
behind it does, exactly as on loopback. The same port receives the session archive the
mini-harness pushes at session end — accepted by any harness holding K, so a restarted
harness still takes it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import structlog
from aiohttp import web

from ach_agent.sandbox.tokens import verify_facade_token

log = structlog.get_logger(__name__)

_DROP_REQUEST = frozenset({"host", "content-length", "transfer-encoding"})
# aiohttp decompresses upstream bodies, so content-encoding must not be forwarded.
_DROP_RESPONSE = frozenset({"content-length", "transfer-encoding", "content-encoding"})
_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=10)

IsLive = Callable[[str], Awaitable[bool]]
OnArchive = Callable[[str, web.Request], Awaitable[web.StreamResponse]]


class FacadeGateway:
    def __init__(self, *, key: bytes, is_live: IsLive, on_archive: OnArchive) -> None:
        self._key = key
        self._is_live = is_live
        self._on_archive = on_archive
        self._ports: set[int] = set()
        self._runner: web.AppRunner | None = None
        self._session: aiohttp.ClientSession | None = None

    def allow_port(self, port: int) -> None:
        self._ports.add(port)

    def rewrite(self, url: str, token: str, *, public_base: str) -> str:
        """Map a registered loopback facade URL to its gateway URL for one sandbox."""
        parts = urlsplit(url)
        if parts.hostname != "127.0.0.1" or parts.port is None or parts.port not in self._ports:
            raise ValueError("only registered loopback facade URLs can be rewritten")
        base = urlsplit(public_base)
        return urlunsplit(
            (base.scheme, base.netloc, f"/s/{token}/{parts.port}{parts.path}", parts.query, "")
        )

    async def start(self, host: str, port: int) -> int:
        self._session = aiohttp.ClientSession(timeout=_TIMEOUT)
        # bodies are streamed, never buffered; the archive handler caps its own
        app = web.Application(client_max_size=0)
        app.router.add_put("/s/{token}/session/archive", self._archive)
        app.router.add_route("*", r"/t/{trace}/s/{token}/{port:\d+}/{tail:.*}", self._relay)
        app.router.add_route("*", r"/s/{token}/{port:\d+}/{tail:.*}", self._relay)
        self._runner = web.AppRunner(app, shutdown_timeout=1.0)
        await self._runner.setup()
        await web.TCPSite(self._runner, host=host, port=port).start()
        bound: int = self._runner.addresses[0][1]
        log.info("facade gateway started", port=bound)
        return bound

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def _claim(self, request: web.Request) -> str:
        claim = verify_facade_token(self._key, request.match_info["token"])
        if claim is None or not await self._is_live(claim):
            raise web.HTTPNotFound()
        return claim

    async def _archive(self, request: web.Request) -> web.StreamResponse:
        return await self._on_archive(await self._claim(request), request)

    async def _relay(self, request: web.Request) -> web.StreamResponse:
        await self._claim(request)
        port = int(request.match_info["port"])
        if port not in self._ports:
            raise web.HTTPNotFound()
        trace = request.match_info.get("trace")
        prefix = f"/t/{trace}" if trace else ""
        target = f"http://127.0.0.1:{port}{prefix}/{request.match_info['tail']}"
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _DROP_REQUEST}
        assert self._session is not None
        # Streamed, never buffered: the caller is an untrusted agent.
        body = request.content if request.body_exists else None
        async with self._session.request(
            request.method, target, headers=headers, params=request.query, data=body
        ) as upstream:
            resp = web.StreamResponse(status=upstream.status)
            for k, v in upstream.headers.items():
                if k.lower() not in _DROP_RESPONSE:
                    resp.headers[k] = v
            await resp.prepare(request)
            try:
                async for chunk in upstream.content.iter_any():
                    await resp.write(chunk)
                await resp.write_eof()
            except (ConnectionResetError, aiohttp.ClientError) as exc:
                log.debug("facade gateway: client gone mid-stream", error=str(exc))
            return resp
