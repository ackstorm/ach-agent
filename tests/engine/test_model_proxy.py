# SPDX-License-Identifier: Apache-2.0
"""Tests for the localhost MODEL reverse-proxy (ek injection + SSE streaming, cost
observer hook + /t/{token} route + include_usage injection — Plan 1 Task 1.6)."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from ach_agent import identity
from ach_agent.engine import trace
from ach_agent.engine.cost import CostAccountant, ModelPrices, PriceTable, TokenUsage, compute_cost
from ach_agent.engine.mcp_proxy import _forward, start_model_proxy, stop_model_proxies


async def _start_fake_ach(seen_auth: list[str | None]) -> tuple[web.AppRunner, str]:
    """Start a real aiohttp ACH upstream on 127.0.0.1:0 that streams an SSE body."""

    async def handler(request: web.Request) -> web.StreamResponse:
        seen_auth.append(request.headers.get("x-ach-key"))
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
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
            url = f"{base}/t/{trace.mint_token()}/v1/responses"
            async with session.post(url, json={"x": 1}) as resp:
                assert resp.status == 200
                body = await resp.read()

        assert b"data: a\n\n" in body
        assert b"data: b\n\n" in body
        assert b"data: c\n\n" in body
        assert body.index(b"data: a") < body.index(b"data: b") < body.index(b"data: c")
        assert seen_auth == ["ek-model-1"]
    finally:
        await stop_model_proxies()
        await ach_runner.cleanup()


# ---------------------------------------------------------------------------
# Task 1.6: observer hook, /t/{token} route, include_usage injection.
# ---------------------------------------------------------------------------


def _usage_record(**overrides: object) -> object:
    from ach_agent.engine.base.events import OpenCodeUsage

    base: dict[str, object] = {
        "session_id": "ses_1",
        "message_id": "msg_1",
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read": 0,
        "cache_write": 0,
        "cost": 0.0,
        "duration_ms": 0,
    }
    base.update(overrides)
    return OpenCodeUsage(**base)  # type: ignore[arg-type]


def _make_accountant(source: str = "litellm_usage", wire: str = "openai") -> CostAccountant:
    prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    table = PriceTable("http://unused", "ek")
    table._prices["m"] = prices  # test-only poke; PriceTable exposes no setter
    return CostAccountant(source=source, wire=wire, prices=table, model_name="m")


async def _start_fake_ach_router(handler) -> tuple[web.AppRunner, str]:
    """Real aiohttp ACH upstream on 127.0.0.1:0 whose single route captures everything."""
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


class _BrokenObserver:
    """Every method raises — proves `_forward` isolates observer failures (A.12)."""

    def mutate_request(self, body: bytes, content_type: str) -> bytes:
        raise RuntimeError("boom-mutate")

    def begin(self, status: int, content_type: str) -> None:
        raise RuntimeError("boom-begin")

    def feed(self, chunk: bytes) -> None:
        raise RuntimeError("boom-feed")

    def finish(self) -> None:
        raise RuntimeError("boom-finish")


async def _run_forward_with_observer(
    fake_ach_url: str, observer: object, request_json: dict | None = None
) -> tuple[int, bytes]:
    """Stand up a tiny app whose single route calls `_forward` directly with `observer`."""

    async def proxy_handler(request: web.Request) -> web.StreamResponse:
        async with aiohttp.ClientSession() as session:
            return await _forward(
                session,
                f"{fake_ach_url}/v1/chat/completions",
                request,
                "ek",
                label="model",
                observer=observer,  # type: ignore[arg-type]
            )

    app = web.Application()
    app.router.add_route("*", "/proxy", proxy_handler)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        async with aiohttp.ClientSession() as client:
            async with client.post(
                f"http://127.0.0.1:{port}/proxy", json=request_json or {}
            ) as resp:
                status = resp.status
                body = await resp.read()
    finally:
        await runner.cleanup()
    return status, body


async def test_include_usage_injected_on_streaming_openai_request_missing_it() -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"ok": True})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/v1/chat/completions", json={"model": "m", "stream": True}
            ) as resp:
                await resp.read()
        assert seen_bodies[0]["stream_options"]["include_usage"] is True
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_include_usage_not_injected_when_already_true() -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"ok": True})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        sent = {"stream": True, "stream_options": {"include_usage": True}}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/t/{token}/v1/chat/completions", json=sent) as resp:
                await resp.read()
        assert seen_bodies[0]["stream_options"] == {"include_usage": True}
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_include_usage_false_changed_to_true() -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"ok": True})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        sent = {"stream": True, "stream_options": {"include_usage": False}}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/t/{token}/v1/chat/completions", json=sent) as resp:
                await resp.read()
        assert seen_bodies[0]["stream_options"]["include_usage"] is True
    finally:
        await stop_model_proxies()
        await runner.cleanup()


@pytest.mark.parametrize("body", [{"stream": False}, {}], ids=["stream-false", "stream-absent"])
async def test_include_usage_not_injected_on_non_streaming_request(body: dict) -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"ok": True})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/t/{token}/v1/chat/completions", json=body) as resp:
                await resp.read()
        assert "stream_options" not in seen_bodies[0]
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_include_usage_never_injected_on_gemini_wire() -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"candidates": []})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant(wire="gemini")
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/gemini/v1beta/models/foo:generateContent",
                json={"stream": True},
            ) as resp:
                await resp.read()
        assert "stream_options" not in seen_bodies[0]
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_token_route_strips_prefix_and_streams_sse_with_ek() -> None:
    seen_auth: list[str | None] = []
    seen_paths: list[str] = []

    async def handler(request: web.Request) -> web.StreamResponse:
        seen_auth.append(request.headers.get("x-ach-key"))
        seen_paths.append(request.path)
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for chunk in (b"data: a\n\n", b"data: b\n\n", b"data: c\n\n"):
            await resp.write(chunk)
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        base = await start_model_proxy(ach_url, "ek-token-route")  # no accountant
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/sometoken/v1/chat/completions", json={"x": 1}
            ) as resp:
                assert resp.status == 200
                body = await resp.read()
        assert seen_paths == ["/v1/chat/completions"]
        assert seen_auth == ["ek-token-route"]
        assert b"data: a\n\n" in body
        assert b"data: b\n\n" in body
        assert b"data: c\n\n" in body
        assert body.index(b"data: a") < body.index(b"data: b") < body.index(b"data: c")
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_broken_observer_never_breaks_a_normal_streaming_response() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b"data: hello\n\n")
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        status, body = await _run_forward_with_observer(
            ach_url, _BrokenObserver(), request_json={"stream": True}
        )
        assert status == 200
        assert body == b"data: hello\n\n"
    finally:
        await runner.cleanup()


async def test_mutate_request_failure_forwards_original_body_unchanged() -> None:
    seen_bodies: list[bytes] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.read())
        return web.json_response({"ok": True})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        status, _body = await _run_forward_with_observer(
            ach_url, _BrokenObserver(), request_json={"stream": True, "a": 1}
        )
        assert status == 200
        import json as _json

        assert _json.loads(seen_bodies[0]) == {"stream": True, "a": 1}
    finally:
        await runner.cleanup()


async def test_multiple_sse_events_in_one_chunk_usage_flows_to_accountant() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        # Two SSE events delivered in a SINGLE write/chunk.
        await resp.write(
            b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n'
            b'data: {"usage":{"prompt_tokens":100,"completion_tokens":10}}\n\n'
        )
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/v1/chat/completions", json={"stream": True}
            ) as resp:
                await resp.read()
        result = acc.end_turn(token, _usage_record())
        prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
        expected = compute_cost(TokenUsage(100, 10, 0, 0), prices)[0]
        assert result.cost == pytest.approx(expected)
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_unterminated_final_sse_event_still_parsed() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        # No trailing \n\n before write_eof — the final event is unterminated.
        await resp.write(b'data: {"usage":{"prompt_tokens":5,"completion_tokens":1}}')
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/v1/chat/completions", json={"stream": True}
            ) as resp:
                await resp.read()
        result = acc.end_turn(token, _usage_record())
        prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
        expected = compute_cost(TokenUsage(5, 1, 0, 0), prices)[0]
        assert result.cost == pytest.approx(expected)
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_unknown_token_still_forwards_and_is_unattributed() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(status=200, headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b'data: {"usage":{"prompt_tokens":100,"completion_tokens":10}}\n\n')
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = _make_accountant()
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        real_token = acc.mint_token()
        acc.begin_turn(real_token)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/never-minted-garbage/v1/chat/completions", json={"stream": True}
            ) as resp:
                assert resp.status == 200
                body = await resp.read()
        assert b"usage" in body  # traffic reached the client — never dropped

        result = acc.end_turn(real_token, _usage_record())
        assert result.cost == 0.0  # the garbage-token usage never landed on the real token
    finally:
        await stop_model_proxies()
        await runner.cleanup()


# ---------------------------------------------------------------------------
# Task 1.8: litellm_headers mode wired end-to-end through the proxy.
# ---------------------------------------------------------------------------


async def test_litellm_headers_non_streaming_cost_summed_and_stream_flag_untouched() -> None:
    seen_bodies: list[dict] = []

    async def handler(request: web.Request) -> web.Response:
        seen_bodies.append(await request.json())
        return web.json_response({"ok": True}, headers={"x-litellm-response-cost": "0.0123"})

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        sent = {"stream": False}
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/t/{token}/v1/chat/completions", json=sent) as resp:
                await resp.read()
        assert seen_bodies[0] == sent  # request body / stream flag never mutated (A.4)
        result = acc.end_turn(token, _usage_record())
        assert result.cost == pytest.approx(0.0123)
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_litellm_headers_streaming_response_preserved_and_zero_cost() -> None:
    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream", "x-litellm-response-cost": "0.5"},
        )
        await resp.prepare(request)
        await resp.write(b"data: hello\n\n")
        await resp.write_eof()
        return resp

    runner, ach_url = await _start_fake_ach_router(handler)
    try:
        acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
        base = await start_model_proxy(ach_url, "ek", accountant=acc)
        token = acc.mint_token()
        acc.begin_turn(token)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/v1/chat/completions", json={"stream": True}
            ) as resp:
                assert resp.status == 200
                assert resp.headers.get("x-litellm-response-cost") == "0.5"
                body = await resp.read()
        assert body == b"data: hello\n\n"  # response body preserved unchanged
        result = acc.end_turn(token, _usage_record())
        assert result.cost == 0.0  # streaming litellm_headers response always contributes 0.0
    finally:
        await stop_model_proxies()
        await runner.cleanup()


async def test_direct_model_override_auth_still_replaces_identity_headers() -> None:
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.headers))
        return web.json_response({"ok": True})

    runner, upstream_url = await _start_fake_ach_router(handler)
    identity.configure("classifier", "platform")
    try:
        base = await start_model_proxy(
            upstream_url,
            "Bearer provider-key",
            auth_header="Authorization",
        )
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{trace.mint_token()}/v1/chat/completions",
                headers={
                    "X-ACH-AGENT": "spoofed-agent",
                    "X-Ach-Environment": "spoofed-environment",
                },
                json={"model": "test-model"},
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await stop_model_proxies()
        await runner.cleanup()
        identity.reset_for_testing()

    lowered = {key.lower(): value for key, value in captured[0].items()}
    assert lowered["authorization"] == "Bearer provider-key"
    assert lowered["x-ach-agent"] == "classifier"
    assert lowered["x-ach-environment"] == "platform"
    assert sum(key.lower() == "x-ach-agent" for key in captured[0]) == 1
    assert sum(key.lower() == "x-ach-environment" for key in captured[0]) == 1


async def test_token_route_injects_trace_and_session_headers() -> None:
    """The /t/{token} route carries the invocation's correlation headers upstream.

    test_trace.py covers the registry in isolation; this asserts the wiring —
    that what trace.begin() records actually reaches ACH on a real request, and
    that a client-sent header of the same name (any case) does not survive
    alongside it.
    """
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.headers))
        return web.json_response({"ok": True})

    runner, upstream_url = await _start_fake_ach_router(handler)
    trace.reset_for_testing()
    token = trace.mint_token()
    trace.set_session(token, "ses_8a1b2c3d")
    trace.begin(token, "classifier", "gitlab", "delivery-abc")
    expected = trace.headers(token)
    try:
        base = await start_model_proxy(upstream_url, "ek-1")
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{base}/t/{token}/v1/chat/completions",
                headers={"Traceparent": "00-" + "f" * 32 + "-" + "f" * 16 + "-01"},
                json={"model": "test-model"},
            ) as response:
                assert response.status == 200
                await response.read()
    finally:
        await stop_model_proxies()
        await runner.cleanup()
        trace.reset_for_testing()

    lowered = {key.lower(): value for key, value in captured[0].items()}
    assert lowered["traceparent"] == expected["traceparent"]
    assert lowered["langfuse_session_id"] == expected["langfuse_session_id"]
    assert sum(key.lower() == "traceparent" for key in captured[0]) == 1


async def test_an_untokenized_request_is_rejected() -> None:
    """No route without a token, on purpose.

    We define the base URL the engine is handed — `main` appends the wire prefix,
    the pool inserts the token — so everything the engine appends rides inside the
    tube. A request that arrives without one has escaped it, and a 404 makes that
    visible instead of silently producing uncorrelated observability data.
    """
    captured: list[dict[str, str]] = []

    async def handler(request: web.Request) -> web.Response:
        captured.append(dict(request.headers))
        return web.json_response({"ok": True})

    runner, upstream_url = await _start_fake_ach_router(handler)
    try:
        base = await start_model_proxy(upstream_url, "ek-1")
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{base}/v1/chat/completions", json={}) as response:
                assert response.status == 404
    finally:
        await stop_model_proxies()
        await runner.cleanup()

    assert captured == [], "an untokenized request must never reach the upstream"
