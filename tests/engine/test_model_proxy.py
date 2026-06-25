# SPDX-License-Identifier: Apache-2.0
"""Tests for the localhost MODEL reverse-proxy (ek injection + SSE streaming)."""

from __future__ import annotations

import aiohttp
from aiohttp import web

from ach_agent.engine.mcp_proxy import start_model_proxy, stop_model_proxies


async def _start_fake_ach(seen_auth: list[str | None]) -> tuple[web.AppRunner, str]:
    """Start a real aiohttp ACH upstream on 127.0.0.1:0 that streams an SSE body."""

    async def handler(request: web.Request) -> web.StreamResponse:
        seen_auth.append(request.headers.get("Authorization"))
        resp = web.StreamResponse(
            status=200, headers={"Content-Type": "text/event-stream"}
        )
        await resp.prepare(request)
        for chunk in (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"):
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    app = web.Application()
    app.router.add_route("*", "/v1/responses", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def test_model_proxy_injects_ek_and_streams_sse() -> None:
    seen_auth: list[str | None] = []
    ach_runner, ach_url = await _start_fake_ach(seen_auth)
    try:
        base = await start_model_proxy(ach_url, "ek-model-1")

        assert base.startswith("http://127.0.0.1:")
        assert "ek-model-1" not in base

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/v1/responses", json={"x": 1}) as resp:
                assert resp.status == 200
                body = await resp.read()

        assert b"data: a\n\n" in body
        assert b"data: b\n\n" in body
        assert b"data: c\n\n" in body
        assert body.index(b"data: a") < body.index(b"data: b") < body.index(b"data: c")
        assert seen_auth == ["Bearer ek-model-1"]
    finally:
        await stop_model_proxies()
        await ach_runner.cleanup()
