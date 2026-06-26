# SPDX-License-Identifier: Apache-2.0
"""Tests for the localhost MCP reverse-proxy (ek injection + SSE streaming)."""

from __future__ import annotations

import asyncio

import aiohttp
from aiohttp import web

from ach_agent.engine.hydrate import McpServer
from ach_agent.engine.mcp_proxy import McpProxy


async def _start_fake_upstream(seen_auth: list[str | None]) -> tuple[web.AppRunner, str]:
    """Start a real aiohttp upstream on 127.0.0.1:0 that records the Authorization header."""

    async def handler(request: web.Request) -> web.Response:
        seen_auth.append(request.headers.get("x-ach-key"))
        body = await request.read()
        return web.json_response({"auth": request.headers.get("x-ach-key"), "echo": body.decode()})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def test_proxy_injects_ek_and_returns_localhost_url() -> None:
    seen_auth: list[str | None] = []
    upstream_runner, upstream_url = await _start_fake_upstream(seen_auth)
    proxy = McpProxy()
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )

        assert urls["m1"].startswith("http://127.0.0.1:")
        assert "/mcp/m1" in urls["m1"]
        assert "ek-xyz" not in urls["m1"]

        async with aiohttp.ClientSession() as session:
            async with session.post(urls["m1"], json={"hello": "world"}) as resp:
                assert resp.status == 200
                data = await resp.json()

        assert seen_auth == ["ek-xyz"]
        assert data["auth"] == "ek-xyz"
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()


async def test_proxy_excludes_listed_servers() -> None:
    seen_auth: list[str | None] = []
    upstream_runner, upstream_url = await _start_fake_upstream(seen_auth)
    proxy = McpProxy()
    try:
        urls = await proxy.start(
            [McpServer(id="ex", endpoint=upstream_url)], ek="e", exclude={"ex"}
        )
        assert "ex" not in urls
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()


async def _start_hanging_upstream() -> tuple[web.AppRunner, str]:
    """Upstream that sends one chunk then hangs — simulates a long-lived MCP/SSE stream."""

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse()
        await resp.prepare(request)
        await resp.write(b"data: hello\n\n")
        await asyncio.sleep(3600)  # never returns on its own
        return resp

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    # Short shutdown_timeout so THIS fixture's own cleanup (its handler also hangs) is fast.
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def test_stop_is_prompt_even_with_a_hanging_upstream_stream() -> None:
    """stop() force-closes a stuck streaming handler via shutdown_timeout (~1s), not ~60s.

    Regression guard: aiohttp's AppRunner defaults shutdown_timeout to 60s, so a proxied
    long-lived stream (blocked in the upstream iter loop) would hang teardown ~60s.
    """
    up_runner, up_url = await _start_hanging_upstream()
    proxy = McpProxy()
    client = aiohttp.ClientSession()
    try:
        urls = await proxy.start([McpServer(id="m1", endpoint=up_url)], ek="ek-x", exclude=set())
        # Fire a request that gets stuck mid-stream inside the proxy handler.
        req_task = asyncio.create_task(client.get(urls["m1"]))
        await asyncio.sleep(0.5)  # let the proxy handler reach the hung-stream state

        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await proxy.stop()
        elapsed = loop.time() - t0
        assert elapsed < 10.0, f"stop() took {elapsed:.1f}s — shutdown_timeout not applied"

        req_task.cancel()
    finally:
        if not client.closed:
            await client.close()
        await up_runner.cleanup()
