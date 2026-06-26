# SPDX-License-Identifier: Apache-2.0
"""Hermetic v3 integration guard: hydrate + proxies + structured terminal.

Exercises the Plan-2 (hydrate) + Plan-1 (localhost proxies + terminal-contract
extraction) components TOGETHER, without the opencode binary. It proves the
harness self-hydrates, fronts model + MCP traffic via localhost proxies that
inject the ``ek_``, streams SSE through the model proxy, and parses the
structured terminal object.

The real opencode-binary round-trip lives in ``scripts/e2e.sh``; this guard does
NOT need the binary. It replaces the retired v2 Codex guard.
"""

from __future__ import annotations

from typing import Any

import aiohttp
from aiohttp import web

from ach_agent.engine import hydrate as hydrate_mod
from ach_agent.engine.hydrate import McpServer, hydrate
from ach_agent.engine.mcp_proxy import McpProxy, start_model_proxy, stop_model_proxies
from ach_agent.engine.validator import extract_terminal


async def _start_fake_mcp_upstream(seen_auth: list[str | None]) -> tuple[web.AppRunner, str]:
    """Start a real aiohttp MCP upstream on 127.0.0.1:0 recording Authorization."""

    async def handler(request: web.Request) -> web.Response:
        seen_auth.append(request.headers.get("x-ach-key"))
        return web.json_response({"auth": request.headers.get("x-ach-key")})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def _start_fake_ach_model(seen_auth: list[str | None]) -> tuple[web.AppRunner, str]:
    """Start a real aiohttp ACH model upstream that streams an SSE body.

    The SSE body contains a tool-call line and the terminal object
    ``{"action":"none","text":"reviewed"}``.
    """

    async def handler(request: web.Request) -> web.StreamResponse:
        seen_auth.append(request.headers.get("x-ach-key"))
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in (
            b'data: {"type":"tool_call","name":"mcp-gofetch"}\n\n',
            b'data: {"action":"none","text":"reviewed"}\n\n',
        ):
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


async def test_hydration_returns_model_and_mcp_server(monkeypatch: Any) -> None:
    """Leg 1: the harness self-hydrates the runtime manifest."""
    mcp_runner, mcp_url = await _start_fake_mcp_upstream([])
    try:

        async def fake_post_hydrate(url: str, headers: dict[str, str]) -> dict[str, Any]:
            return {
                "environment": "guard",
                "runtime": {
                    # Real ACH shape: models are objects {id, endpoint}, not strings.
                    "models": [{"id": "openai.gpt-5", "endpoint": "https://ach.example/v1"}],
                    "mcpServers": [{"id": "mcp-gofetch", "endpoint": mcp_url}],
                },
            }

        monkeypatch.setattr(hydrate_mod, "_post_hydrate", fake_post_hydrate)

        manifest = await hydrate("https://ach.example", "ek_guard_secret")

        assert manifest.models == ["openai.gpt-5"]
        assert manifest.mcp_servers[0].id == "mcp-gofetch"
        assert manifest.mcp_servers[0].endpoint == mcp_url
    finally:
        await mcp_runner.cleanup()


async def test_mcp_via_proxy_carries_ek() -> None:
    """Leg 2: MCP traffic flows through the localhost proxy with the ek injected."""
    seen_auth: list[str | None] = []
    mcp_runner, mcp_url = await _start_fake_mcp_upstream(seen_auth)
    proxy = McpProxy()
    try:
        urls = await proxy.start(
            [McpServer(id="mcp-gofetch", endpoint=mcp_url)],
            ek="ek_guard_secret",
            exclude=set(),
        )

        localhost_url = urls["mcp-gofetch"]
        assert localhost_url.startswith("http://127.0.0.1:")
        assert "ek" not in localhost_url

        async with aiohttp.ClientSession() as session:
            async with session.post(localhost_url, json={"jsonrpc": "2.0"}) as resp:
                assert resp.status == 200
                data = await resp.json()

        assert seen_auth == ["ek_guard_secret"]
        assert data["auth"] == "ek_guard_secret"
    finally:
        await proxy.stop()
        await mcp_runner.cleanup()


async def test_model_proxy_sse_and_terminal_parse() -> None:
    """Legs 3 + 4: model-proxy streams SSE with the ek, terminal object parses."""
    seen_auth: list[str | None] = []
    ach_runner, ach_url = await _start_fake_ach_model(seen_auth)
    try:
        base = await start_model_proxy(ach_url, "ek_guard_secret")
        assert base.startswith("http://127.0.0.1:")
        assert "ek_guard_secret" not in base

        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/v1/responses", json={"x": 1}) as resp:
                assert resp.status == 200
                body = await resp.read()

        # Leg 3: all chunks arrived in order, ek reached the upstream.
        tool_chunk = b'data: {"type":"tool_call","name":"mcp-gofetch"}\n\n'
        term_chunk = b'data: {"action":"none","text":"reviewed"}\n\n'
        assert tool_chunk in body
        assert term_chunk in body
        assert body.index(tool_chunk) < body.index(term_chunk)
        assert seen_auth == ["ek_guard_secret"]

        # Leg 4: accumulate the SSE text and parse the structured terminal object.
        accumulated = body.decode()
        obj = extract_terminal(accumulated)
        assert obj is not None
        assert obj["action"] == "none"
        assert obj["text"] == "reviewed"
    finally:
        await stop_model_proxies()
        await ach_runner.cleanup()
