# SPDX-License-Identifier: Apache-2.0
"""Tests for the localhost MCP reverse-proxy (ek injection + SSE streaming)."""

from __future__ import annotations

import asyncio
import json

import aiohttp
from aiohttp import web

from ach_agent import identity
from ach_agent.engine import trace
from ach_agent.engine.hydrate import McpServer
from ach_agent.engine.mcp_proxy import McpProxy


def _routable(url: str) -> str:
    """Bind an unbound proxy URL to a fresh token.

    The proxy serves only ``/t/{token}/…``, so even a test that does not care about
    correlation has to go through the tube — same as the pool does in production.
    """
    return trace.tokenize_url(url, trace.mint_token())


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
        target = _routable(urls["m1"])

        async with aiohttp.ClientSession() as session:
            async with session.post(target, json={"hello": "world"}) as resp:
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
        req_task = asyncio.create_task(client.get(_routable(urls["m1"])))
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


async def test_mcp_proxy_replaces_case_variant_client_identity() -> None:
    seen_identity: list[list[tuple[str, str]]] = []

    async def handler(request: web.Request) -> web.Response:
        decoded = [(key.decode().lower(), value.decode()) for key, value in request.raw_headers]
        seen_identity.append(
            [(key, value) for key, value in decoded if key in {"x-ach-agent", "x-ach-environment"}]
        )
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    upstream_runner = web.AppRunner(app)
    await upstream_runner.setup()
    site = web.TCPSite(upstream_runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    proxy = McpProxy()
    identity.configure("classifier", "platform")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=f"http://127.0.0.1:{port}")],
            ek="ek-mcp",
            exclude=set(),
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _routable(urls["m1"]),
                headers={
                    "X-Ach-Agent": "spoofed-agent",
                    "x-ACH-environment": "spoofed-environment",
                },
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        identity.reset_for_testing()

    assert seen_identity == [
        [("x-ach-agent", "classifier"), ("x-ach-environment", "platform")]
    ]


async def _start_header_recording_upstream(
    seen: list[dict[str, str]],
) -> tuple[web.AppRunner, str]:
    """Upstream that records the correlation headers of every request it receives."""

    async def handler(request: web.Request) -> web.Response:
        seen.append(
            {
                key.lower(): value
                for key, value in request.headers.items()
                # Deliberately NOT trace.is_correlation_header: a recorder keyed on the
                # code under test goes blind on exactly the header that predicate
                # wrongly lets through.
                if key.lower().startswith(("langfuse", "trace", "x-litellm"))
            }
        )
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def test_a_tool_call_joins_its_invocations_trace() -> None:
    """The defect this route fixes: MCP calls used to reach Langfuse uncorrelated.

    The engine subprocess sets no correlation header of its own, so the token in the
    path is the only thing tying a tool call to the invocation that caused it.
    """
    seen: list[dict[str, str]] = []
    upstream_runner, upstream_url = await _start_header_recording_upstream(seen)
    proxy = McpProxy()
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    trace.set_session(token, "ses_0583b1827ffeaLtpVshBDEtCfe")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )
        tokenized = trace.tokenize_url(urls["m1"], token)
        assert f"/t/{token}/mcp/m1" in tokenized

        async with aiohttp.ClientSession() as session:
            async with session.post(tokenized, json={"hello": "world"}) as resp:
                assert resp.status == 200
                await resp.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    invocation_traceparent = trace.traceparent_for("agent", "webhook", "delivery-1")
    assert seen == [
        {
            "traceparent": invocation_traceparent,
            "langfuse_session_id": "ses_0583b1827ffeaLtpVshBDEtCfe",
            "x-litellm-session-id": "ses_0583b1827ffeaLtpVshBDEtCfe",
            "x-litellm-trace-id": invocation_traceparent.split("-")[1],
        }
    ], "an MCP tool call must carry the same trace + session as the model calls"


async def test_an_untokenized_request_is_rejected() -> None:
    """No route without a token, on purpose.

    We define the base URL the engine is handed, so everything it appends stays
    inside the token prefix. A request that arrives without one has escaped the
    tube: 404 makes that visible, whereas forwarding it would silently produce
    uncorrelated observability data — the exact failure class this whole path
    exists to prevent.
    """
    seen: list[dict[str, str]] = []
    upstream_runner, upstream_url = await _start_header_recording_upstream(seen)
    proxy = McpProxy()
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(urls["m1"], json={"hello": "world"}) as resp:
                assert resp.status == 404
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    assert seen == [], "an untokenized request must never reach the upstream"


async def test_a_forged_traceparent_is_dropped_between_turns() -> None:
    """Having no traceparent to substitute is not a reason to pass the engine's on.

    Live case, not hypothetical: `trace.end` clears the traceparent at the end of an
    invocation but keeps the session, and a warm pooled server keeps serving the
    engine's own between-turn calls (title, summary, compaction). A shadow set keyed
    on the headers being ADDED would be missing `traceparent` in exactly that window,
    letting the engine name its own trace.
    """
    seen: list[dict[str, str]] = []
    upstream_runner, upstream_url = await _start_header_recording_upstream(seen)
    proxy = McpProxy()
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    trace.set_session(token, "ses_0583b1827ffeaLtpVshBDEtCfe")
    trace.end(token)
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                trace.tokenize_url(urls["m1"], token),
                headers={"Traceparent": "00-" + "f" * 32 + "-" + "f" * 16 + "-01"},
            ) as resp:
                assert resp.status == 200
                await resp.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    assert seen == [
        {
            "langfuse_session_id": "ses_0583b1827ffeaLtpVshBDEtCfe",
            "x-litellm-session-id": "ses_0583b1827ffeaLtpVshBDEtCfe",
        }
    ], "the session survives the turn boundary; a forged traceparent must not"


async def test_a_client_supplied_traceparent_never_wins() -> None:
    """The engine must not be able to forge the correlation of its own tool calls.

    `traceparent` is not the only lever: LiteLLM's
    `LangFuseLogger.add_metadata_from_header` copies EVERY `langfuse_*` request header
    into metadata, so an engine setting `langfuse_trace_id` would attribute its call to
    a trace of its choosing. `tracestate` rides along — we replace `traceparent`, so
    keeping the engine's half of the pair leaves an incoherent W3C context. No session
    is set here, so a leaked `langfuse_*` shows up as an EXTRA key.
    """
    seen: list[dict[str, str]] = []
    upstream_runner, upstream_url = await _start_header_recording_upstream(seen)
    proxy = McpProxy()
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                trace.tokenize_url(urls["m1"], token),
                headers={
                    "Traceparent": "00-" + "f" * 32 + "-" + "f" * 16 + "-01",
                    "langfuse_trace_id": "forged-trace",
                    "Langfuse_Parent_Observation_Id": "forged-parent",
                    "langfuse_tags": "forged",
                    "tracestate": "vendor=forged",
                },
            ) as resp:
                assert resp.status == 200
                await resp.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    invocation_traceparent = trace.traceparent_for("agent", "webhook", "delivery-1")
    assert seen == [
        {
            "traceparent": invocation_traceparent,
            "x-litellm-trace-id": invocation_traceparent.split("-")[1],
        }
    ], "ours replaces the forged traceparent; no other forged key survives"


async def test_a_client_supplied_litellm_session_id_never_wins() -> None:
    """The ach forwarder allowlist (ackstorm/ach#172) opens exactly this lever.

    `x-litellm-session-id`/`x-litellm-trace-id` are the two keys LiteLLM's own
    `get_chain_id_from_headers` reads to group spend logs — an engine forging
    them would pick its own LiteLLM conversation grouping. No session is set
    here (correlation would otherwise silently overwrite the forgery with the
    right value), so a leaked one shows up as an EXTRA key instead.
    """
    seen: list[dict[str, str]] = []
    upstream_runner, upstream_url = await _start_header_recording_upstream(seen)
    proxy = McpProxy()
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=upstream_url)], ek="ek-xyz", exclude=set()
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                trace.tokenize_url(urls["m1"], token),
                headers={
                    "X-Litellm-Session-Id": "forged-session",
                    "X-Litellm-Trace-Id": "forged-trace",
                },
            ) as resp:
                assert resp.status == 200
                await resp.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    invocation_traceparent = trace.traceparent_for("agent", "webhook", "delivery-1")
    assert seen == [
        {
            "traceparent": invocation_traceparent,
            "x-litellm-trace-id": invocation_traceparent.split("-")[1],
        }
    ], "ours replaces the forged x-litellm-trace-id; the forged session-id is dropped"


async def test_a_tool_call_carries_its_trace_in_the_message_too() -> None:
    """The MCP-native carrier (SEP-414), on top of the header.

    A streamable-HTTP session multiplexes many messages over one connection, so a
    header-only correlation glues every message under the session's first request.
    LiteLLM reads this carrier only under LITELLM_OTEL_V2, so the assertion is on
    what leaves US — the contract we control.
    """
    bodies: list[bytes] = []

    async def handler(request: web.Request) -> web.Response:
        bodies.append(await request.read())
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    upstream_runner = web.AppRunner(app)
    await upstream_runner.setup()
    site = web.TCPSite(upstream_runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    proxy = McpProxy()
    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery-1")
    try:
        urls = await proxy.start(
            [McpServer(id="m1", endpoint=f"http://127.0.0.1:{port}")], ek="ek-xyz", exclude=set()
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                trace.tokenize_url(urls["m1"], token),
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "search", "arguments": {"q": "x"}},
                },
            ) as resp:
                assert resp.status == 200
                await resp.read()
    finally:
        await proxy.stop()
        await upstream_runner.cleanup()
        trace.reset_for_testing()

    sent = json.loads(bodies[0])
    assert sent["params"]["_meta"]["traceparent"] == trace.traceparent_for(
        "agent", "webhook", "delivery-1"
    )
    assert sent["params"]["arguments"] == {"q": "x"}, "the tool's own arguments must survive"
