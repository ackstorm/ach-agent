# SPDX-License-Identifier: Apache-2.0
"""Cache-aware pricing math (A.2), per-wire usage parse (AC-7), price cache (B.5)."""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web

from ach_agent.engine.cost import ModelPrices, PriceTable, TokenUsage, UsageObserver, compute_cost


def test_cost_is_cache_aware_and_subtractive() -> None:
    u = TokenUsage(
        prompt_tokens=1000, completion_tokens=100, cached_read_tokens=400, cache_creation_tokens=100
    )
    p = ModelPrices(1e-6, 2e-6, 1e-7, 5e-7)
    cost, clamped = compute_cost(u, p)
    # billable = 1000-400-100 = 500
    assert cost == pytest.approx(500 * 1e-6 + 400 * 1e-7 + 100 * 5e-7 + 100 * 2e-6)
    assert clamped is False


def test_billable_input_clamps_at_zero() -> None:
    u = TokenUsage(
        prompt_tokens=100, completion_tokens=0, cached_read_tokens=400, cache_creation_tokens=0
    )
    cost, clamped = compute_cost(u, ModelPrices(1e-6, 2e-6, 1e-7, 1e-6))
    assert clamped is True
    assert cost == pytest.approx(400 * 1e-7)


def test_openai_final_chunk_usage_parsed() -> None:
    obs = UsageObserver("openai")
    obs.begin(200, "text/event-stream")
    obs.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    obs.feed(
        b'data: {"usage":{"prompt_tokens":50,"completion_tokens":10,'
        b'"prompt_tokens_details":{"cached_tokens":5},"cache_creation_input_tokens":2}}\n\n'
    )
    u = obs.finish()
    assert u == TokenUsage(
        prompt_tokens=50, completion_tokens=10, cached_read_tokens=5, cache_creation_tokens=2
    )


def test_openai_non_streaming_usage_parsed() -> None:
    obs = UsageObserver("openai")
    obs.begin(200, "application/json")
    obs.feed(
        b'{"choices":[{"message":{"content":"hi"}}],'
        b'"usage":{"prompt_tokens":30,"completion_tokens":8}}'
    )
    u = obs.finish()
    # cache_creation_input_tokens and prompt_tokens_details are both absent ⇒ 0
    assert u == TokenUsage(
        prompt_tokens=30, completion_tokens=8, cached_read_tokens=0, cache_creation_tokens=0
    )


def test_gemini_takes_last_cumulative_never_the_sum() -> None:
    obs = UsageObserver("gemini")
    obs.begin(200, "text/event-stream")
    obs.feed(b'data: {"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":5}}\n\n')
    obs.feed(
        b'data: {"usageMetadata":{"promptTokenCount":10,"candidatesTokenCount":12,'
        b'"thoughtsTokenCount":3,"cachedContentTokenCount":4}}\n\n'
    )
    u = obs.finish()
    assert u == TokenUsage(
        prompt_tokens=10, completion_tokens=15, cached_read_tokens=4, cache_creation_tokens=0
    )


def test_body_over_cap_returns_none() -> None:
    obs = UsageObserver("openai")
    obs.begin(200, "application/json")
    obs.feed(b"x" * (1024 * 1024 + 1))
    assert obs.finish() is None


# ---------------------------------------------------------------------------
# PriceTable (B.5): /v2/model/info paginated envelope, two-step match.
# ---------------------------------------------------------------------------

PRICED: dict[str, float] = {
    "input_cost_per_token": 1e-6,
    "output_cost_per_token": 2e-6,
    "cache_read_input_token_cost": 1e-7,
    "cache_creation_input_token_cost": 5e-7,
}


def _entry(model_name: str, **fields: object) -> dict[str, object]:
    return {"model_name": model_name, **fields}


async def _start_fake_model_info(handler) -> tuple[web.AppRunner, str]:
    """Real aiohttp upstream on 127.0.0.1:0 serving GET /v2/model/info."""
    app = web.Application()
    app.router.add_route("GET", "/v2/model/info", handler)
    # Short shutdown_timeout (mcp_proxy.py precedent): a hanging handler must not stall
    # test teardown for the aiohttp AppRunner default of 60s.
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


async def test_price_fields_read_from_top_level_and_unknown_keys_ignored() -> None:
    async def handler(request: web.Request) -> web.Response:
        assert request.query.get("model") == "gpt-4"
        body = {
            "current_page": 1,
            "data": [_entry("gpt-4", **PRICED), _entry("other-model", **PRICED)],
            "size": 2,
            "total_count": 2,
            "total_pages": 1,
            "unknown_envelope_key": "ignored",
        }
        return web.json_response(body)

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure is None
        assert table.get("gpt-4") == ModelPrices(**PRICED)
    finally:
        await runner.cleanup()


async def test_match_falls_back_to_litellm_params_model() -> None:
    async def handler(request: web.Request) -> web.Response:
        entry = {"model_name": "alias-name", "litellm_params": {"model": "gpt-4"}, **PRICED}
        return web.json_response({"data": [entry]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure is None
        assert table.get("gpt-4") == ModelPrices(**PRICED)
    finally:
        await runner.cleanup()


async def test_no_match_is_no_entry() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [_entry("some-other-model", **PRICED)]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure == "no_entry"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


async def test_absent_cache_prices_fall_back_to_input_cost() -> None:
    async def handler(request: web.Request) -> web.Response:
        entry = _entry("gpt-4", input_cost_per_token=1e-6, output_cost_per_token=2e-6)
        return web.json_response({"data": [entry]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure is None
        assert table.get("gpt-4") == ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    finally:
        await runner.cleanup()


async def test_fields_read_from_model_info_nested() -> None:
    async def handler(request: web.Request) -> web.Response:
        entry = {"model_name": "gpt-4", "model_info": dict(PRICED)}
        return web.json_response({"data": [entry]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure is None
        assert table.get("gpt-4") == ModelPrices(**PRICED)
    finally:
        await runner.cleanup()


async def test_fetch_failed_on_server_error() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=500)

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure == "fetch_failed"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


async def test_fetch_failed_on_connection_error() -> None:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # nothing listens on this port -> ECONNREFUSED

    table = PriceTable(f"http://127.0.0.1:{port}", "ek")
    result = await table.load("gpt-4")
    assert result.failure == "fetch_failed"
    assert table.get("gpt-4") is None


async def test_fetch_failed_on_hanging_response_timeout() -> None:
    async def handler(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        return web.json_response({"data": []})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek", timeout_seconds=0.3)
        result = await table.load("gpt-4")
        assert result.failure == "fetch_failed"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


async def test_production_sends_only_url_encoded_model_and_ach_key_header() -> None:
    seen: dict[str, object] = {}

    async def handler(request: web.Request) -> web.Response:
        seen["query"] = dict(request.query)
        seen["headers"] = dict(request.headers)
        return web.json_response({"data": [_entry("weird model/name", **PRICED)]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "super-secret-ek")
        await table.load("weird model/name")
        assert seen["query"] == {"model": "weird model/name"}
        headers = seen["headers"]
        assert headers.get("x-ach-key") == "super-secret-ek"
        assert not any("auth" in k.lower() for k in headers if k.lower() != "x-ach-key")
        # never persisted where it could leak
        assert "super-secret-ek" not in repr(table)
        assert "super-secret-ek" not in str(table)
    finally:
        await runner.cleanup()


async def test_no_second_credential_or_header_introduced() -> None:
    """The dev bypass reuses its existing header/token pair — PriceTable's constructor
    has no parameter for a second credential, so exactly one auth header is ever sent."""
    seen_headers: dict[str, str] = {}

    async def handler(request: web.Request) -> web.Response:
        seen_headers.update(request.headers)
        return web.json_response({"data": []})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "dev-bypass-token")
        await table.load("gpt-4")
        auth_headers = {
            k: v for k, v in seen_headers.items() if "auth" in k.lower() or k.lower() == "x-ach-key"
        }
        assert auth_headers == {"x-ach-key": "dev-bypass-token"}
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "raw_price",
    [True, False, -1e-6, float("nan"), float("inf"), "not-a-number"],
    ids=["bool-true", "bool-false", "negative", "nan", "inf", "nonnumeric"],
)
async def test_malformed_price_values_rejected(raw_price: object) -> None:
    async def handler(request: web.Request) -> web.Response:
        entry = _entry("gpt-4", input_cost_per_token=raw_price, output_cost_per_token=2e-6)
        return web.json_response({"data": [entry]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure == "malformed"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"input_cost_per_token": None, "output_cost_per_token": 2e-6},
        {"input_cost_per_token": 1e-6, "output_cost_per_token": None},
        {"input_cost_per_token": 0, "output_cost_per_token": 2e-6},
        {"input_cost_per_token": 1e-6, "output_cost_per_token": 0},
    ],
    ids=["both-absent", "input-null", "output-null", "input-zero", "output-zero"],
)
async def test_unpriced_when_base_price_absent_null_or_zero(fields: dict) -> None:
    async def handler(request: web.Request) -> web.Response:
        entry = _entry("gpt-4", **fields)
        return web.json_response({"data": [entry]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result.failure == "unpriced"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()
