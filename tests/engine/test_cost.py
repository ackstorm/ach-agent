# SPDX-License-Identifier: Apache-2.0
"""Cache-aware pricing math (A.2), per-wire usage parse (AC-7), price cache (B.5),
attribution + warn budget (AC-9, AC-10)."""

from __future__ import annotations

import asyncio

import pytest
from prometheus_client import REGISTRY
from aiohttp import web
from structlog.testing import capture_logs

from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.engine.cost import (
    CostAccountant,
    CostObserver,
    ModelPrices,
    PriceTable,
    TokenUsage,
    UsageObserver,
    compute_cost,
    report_price_load_result,
    report_price_load_result,
    validate_cost_source,
)


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
        assert result is None
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
        assert result is None
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
        assert result == "no_entry"
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
        assert result is None
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
        assert result is None
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
        assert result == "fetch_failed"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


async def test_successful_invalid_json_is_a_malformed_price_load_failure() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=200, body=b"{not-json", content_type="application/json")

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        result = await table.load("gpt-4")
        assert result == "malformed"
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
    assert result == "fetch_failed"
    assert table.get("gpt-4") is None


async def test_fetch_failed_on_hanging_response_timeout() -> None:
    async def handler(request: web.Request) -> web.Response:
        await asyncio.sleep(30)
        return web.json_response({"data": []})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek", timeout_seconds=0.3)
        result = await table.load("gpt-4")
        assert result == "fetch_failed"
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
        assert result == "malformed"
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
        assert result == "unpriced"
        assert table.get("gpt-4") is None
    finally:
        await runner.cleanup()


async def test_boot_reports_an_unpriced_model_once_after_price_load() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [_entry("gpt-4", input_cost_per_token=1e-6)]})

    runner, url = await _start_fake_model_info(handler)
    try:
        table = PriceTable(url, "ek")
        with capture_logs() as cap:
            result = await table.load("gpt-4")
            report_price_load_result(result, "gpt-4")
        assert result == "unpriced"
        assert len(cap) == 1
        assert cap[0]["event"] == "cost: model is unpriced"
        assert cap[0]["model"] == "gpt-4"
        assert cap[0]["failure"] == "unpriced"
    finally:
        await runner.cleanup()


# ---------------------------------------------------------------------------
# CostAccountant (A.3, A.9): per-server-token attribution, warn budget.
# ---------------------------------------------------------------------------


def _usage_record(**overrides: object) -> OpenCodeUsage:
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


def _unpriced(reason: str) -> float:
    """Current ach_agent_cost_unpriced_total for one reason."""
    return REGISTRY.get_sample_value("ach_agent_cost_unpriced_total", {"reason": reason}) or 0.0


def _priced_table(model_name: str, prices: ModelPrices) -> PriceTable:
    table = PriceTable("http://unused", "ek")
    table._prices[model_name] = prices  # test-only poke; PriceTable exposes no setter
    return table


def test_interleaved_tokens_attributed_independently() -> None:
    prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    table = _priced_table("m", prices)
    acc = CostAccountant(source="litellm_usage", wire="openai", prices=table, model_name="m")
    t1 = acc.mint_token()
    t2 = acc.mint_token()
    acc.begin_turn(t1)
    acc.begin_turn(t2)

    u1 = TokenUsage(
        prompt_tokens=100, completion_tokens=10, cached_read_tokens=0, cache_creation_tokens=0
    )
    u2 = TokenUsage(
        prompt_tokens=1000, completion_tokens=100, cached_read_tokens=0, cache_creation_tokens=0
    )
    acc.record_usage(t1, u1)
    acc.record_usage(t2, u2)
    acc.record_usage(t1, u1)  # interleaved second call on t1, before t2's turn ends

    result1 = acc.end_turn(t1, _usage_record())
    assert result1.cost == pytest.approx(2 * compute_cost(u1, prices)[0])

    # t2's bucket must be untouched by t1's end_turn (read-and-reset is per-token).
    result2 = acc.end_turn(t2, _usage_record())
    assert result2.cost == pytest.approx(compute_cost(u2, prices)[0])


def test_none_and_unknown_token_are_unattributed_never_billed() -> None:
    prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    table = _priced_table("m", prices)
    acc = CostAccountant(source="litellm_usage", wire="openai", prices=table, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)

    u = TokenUsage(
        prompt_tokens=100, completion_tokens=10, cached_read_tokens=0, cache_creation_tokens=0
    )
    acc.record_usage(None, u)
    acc.record_usage("unknown-token-not-minted", u)

    result = acc.end_turn(t1, _usage_record())
    assert result.cost == 0.0


def test_token_with_no_in_flight_turn_is_unattributed() -> None:
    """A warm-window background call on a still-minted (idle_ttl) token is unattributed."""
    prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    table = _priced_table("m", prices)
    acc = CostAccountant(source="litellm_usage", wire="openai", prices=table, model_name="m")
    t1 = acc.mint_token()  # minted, never begin_turn'd — no invocation currently in flight

    u = TokenUsage(
        prompt_tokens=100, completion_tokens=10, cached_read_tokens=0, cache_creation_tokens=0
    )
    acc.record_usage(t1, u)

    acc.begin_turn(t1)
    result = acc.end_turn(t1, _usage_record())
    assert result.cost == 0.0


def test_warn_once_per_condition_per_invocation_resets_on_end_turn() -> None:
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)

    with capture_logs() as cap:
        acc.warn_once(t1, "same_condition")
        acc.warn_once(t1, "same_condition")
        acc.warn_once(t1, "same_condition")
        acc.warn_once(t1, "other_condition")
    assert len(cap) == 2  # one line per DISTINCT condition, repeats suppressed

    acc.end_turn(t1, _usage_record())  # turn boundary resets the per-token warn budget
    acc.begin_turn(t1)
    with capture_logs() as cap2:
        acc.warn_once(t1, "same_condition")
    assert len(cap2) == 1


def test_unattributed_usage_logs_once_per_turn_boundary() -> None:
    prices = ModelPrices(1e-6, 2e-6, 1e-6, 1e-6)
    table = _priced_table("m", prices)
    acc = CostAccountant(source="litellm_usage", wire="openai", prices=table, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    u = TokenUsage(
        prompt_tokens=100, completion_tokens=10, cached_read_tokens=0, cache_creation_tokens=0
    )

    with capture_logs() as cap:
        acc.record_usage(None, u)
        acc.record_usage("never-minted", u)
        acc.record_usage(None, u)
    assert len(cap) == 1

    acc.end_turn(t1, _usage_record())  # turn boundary resets the unattributed warn budget
    with capture_logs() as cap2:
        acc.record_usage(None, u)
    assert len(cap2) == 1


def test_engine_source_end_turn_returns_usage_unmodified() -> None:
    acc = CostAccountant(source="engine", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    u = TokenUsage(
        prompt_tokens=100, completion_tokens=10, cached_read_tokens=0, cache_creation_tokens=0
    )
    acc.record_usage(t1, u)  # even if recorded, source=engine must ignore it entirely

    original = _usage_record(cost=1.23)
    result = acc.end_turn(t1, original)
    assert result is original  # byte-for-byte unmodified — no parse-driven override


# ---------------------------------------------------------------------------
# Boot validation and A.5 failure table (AC-8, AC-10).
# ---------------------------------------------------------------------------


def test_tokens_and_cost_share_one_basis_across_the_turn() -> None:
    """end_turn overrides tokens together with cost — both summed over the turn's calls.

    The engine's own usage record covers ~the last message only; leaving it beside a
    whole-turn cost made $/token wrong by the turn's upstream-call count.
    """
    prices = ModelPrices(3e-7, 2.5e-6, 7.5e-8, 3e-7)
    acc = CostAccountant(
        source="litellm_usage", wire="gemini", prices=_priced_table("m", prices), model_name="m"
    )
    token = acc.mint_token()
    acc.begin_turn(token)

    call = TokenUsage(
        prompt_tokens=1000, completion_tokens=20, cached_read_tokens=400, cache_creation_tokens=0
    )
    for _ in range(3):
        acc.record_usage(token, call)

    # The engine reported only its last message (9822/67) — the override must replace it.
    result = acc.end_turn(token, _usage_record(input_tokens=9822, output_tokens=67))

    assert result.input_tokens == 3 * 600  # billable = prompt - cache_read (A.2)
    assert result.output_tokens == 3 * 20
    assert result.cache_read == 3 * 400
    assert result.cache_write == 0
    # Divisible: the cost is exactly the price of the tokens now reported alongside it.
    assert result.cost == pytest.approx(
        result.input_tokens * 3e-7 + result.cache_read * 7.5e-8 + result.output_tokens * 2.5e-6
    )


def test_litellm_headers_overrides_cost_only_and_keeps_engine_tokens() -> None:
    """No token data on the header wire — the engine's counts must survive untouched."""
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    token = acc.mint_token()
    acc.begin_turn(token)
    acc.record_header_cost(token, "0.25", streaming=False)

    result = acc.end_turn(token, _usage_record(input_tokens=111, output_tokens=22))

    assert result.cost == pytest.approx(0.25)
    assert (result.input_tokens, result.output_tokens) == (111, 22)


def test_unpriced_model_counts_every_response_not_just_boot() -> None:
    """The prod trap: a model /v2/model/info doesn't know bills $0 forever, silently."""
    acc = CostAccountant(
        source="litellm_usage", wire="gemini", prices=_priced_table("other", ModelPrices(1, 1, 1, 1)), model_name="m"
    )
    token = acc.mint_token()
    acc.begin_turn(token)
    before = _unpriced("unpriced")

    acc.record_usage(
        token,
        TokenUsage(
            prompt_tokens=10, completion_tokens=1, cached_read_tokens=0, cache_creation_tokens=0
        ),
    )

    assert acc.end_turn(token, _usage_record()).cost == 0.0
    assert _unpriced("unpriced") == before + 1, "an unpriced response must be visible to rate()"


def test_price_load_failure_counts_by_reason() -> None:
    before = _unpriced("no_entry")
    report_price_load_result("no_entry", "gemini-flash-latest")
    assert _unpriced("no_entry") == before + 1


def test_litellm_usage_anthropic_is_a_boot_hard_fail() -> None:
    with pytest.raises(ValueError) as exc_info:
        validate_cost_source("litellm_usage", "anthropic")
    message = str(exc_info.value)
    assert "cost.source" in message
    assert "model.type" in message
    assert "anthropic" in message


@pytest.mark.parametrize("source", ["engine", "none"])
def test_anthropic_is_accepted_for_wire_independent_sources(source: str) -> None:
    validate_cost_source(source, "anthropic")


@pytest.mark.parametrize("model_type", ["gemini", "anthropic"])
def test_litellm_headers_on_a_passthrough_wire_is_a_boot_hard_fail(model_type: str) -> None:
    """LiteLLM injects x-litellm-response-cost from its /v1 router only (measured on 1.93.0):
    /gemini responses carry no cost header at all, so the source silently reports $0."""
    with pytest.raises(ValueError) as exc_info:
        validate_cost_source("litellm_headers", model_type)
    message = str(exc_info.value)
    assert "litellm_headers" in message
    assert model_type in message
    assert "litellm_usage" in message


def test_litellm_headers_stays_valid_on_the_openai_router_wire() -> None:
    validate_cost_source("litellm_headers", "openai")


def _observer_accountant() -> tuple[CostAccountant, str]:
    acc = CostAccountant(source="litellm_usage", wire="openai", prices=None, model_name="m")
    token = acc.mint_token()
    acc.begin_turn(token)
    return acc, token


def test_missing_final_usage_is_zero_and_warns_once_per_invocation() -> None:
    acc, token = _observer_accountant()
    observer = CostObserver(acc, token)
    observer.begin(200, "application/json")
    with capture_logs() as cap:
        observer.finish()
        observer.finish()
    assert len(cap) == 1
    assert cap[0]["event"] == "cost: usage_missing"
    assert acc.end_turn(token, _usage_record()).cost == 0.0


def test_non_2xx_without_usage_is_zero_without_an_extra_warning() -> None:
    acc, token = _observer_accountant()
    observer = CostObserver(acc, token)
    observer.begin(500, "application/json")
    observer.feed(b'{"usage":{"prompt_tokens":100,"completion_tokens":10}}')
    with capture_logs() as cap:
        observer.finish()
    assert len(cap) == 0
    assert acc.end_turn(token, _usage_record()).cost == 0.0


def test_non_2xx_usage_payload_is_ignored() -> None:
    observer = UsageObserver("openai")
    observer.begin(500, "application/json")
    observer.feed(b'{"usage":{"prompt_tokens":100,"completion_tokens":10}}')
    assert observer.finish() is None


@pytest.mark.parametrize("failure", ["no_entry", "unpriced", "malformed"])
def test_unpriced_a5_rows_warn_with_model_name(failure: str) -> None:
    with capture_logs() as cap:
        report_price_load_result(failure, "gpt-4")  # type: ignore[arg-type]
    assert len(cap) == 1
    assert cap[0]["model"] == "gpt-4"
    assert cap[0]["failure"] == failure


def test_price_fetch_failure_is_one_boot_error_and_not_a_hard_fail() -> None:
    with capture_logs() as cap:
        report_price_load_result("fetch_failed", "gpt-4")
    assert len(cap) == 1
    assert cap[0]["log_level"] == "error"
    assert cap[0]["model"] == "gpt-4"


# ---------------------------------------------------------------------------
# litellm_headers mode (A.4, AC-6): record_header_cost.
# ---------------------------------------------------------------------------


def test_record_header_cost_non_streaming_parses_and_sums() -> None:
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    acc.record_header_cost(t1, "0.0042", streaming=False)
    acc.record_header_cost(t1, "0.0008", streaming=False)
    result = acc.end_turn(t1, _usage_record())
    assert result.cost == pytest.approx(0.005)


def test_record_header_cost_streaming_contributes_zero_with_one_warning() -> None:
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    with capture_logs() as cap:
        acc.record_header_cost(t1, "0.01", streaming=True)
        acc.record_header_cost(t1, "0.01", streaming=True)
    assert len(cap) == 1
    assert cap[0]["cost_source"] == "litellm_headers"
    result = acc.end_turn(t1, _usage_record())
    assert result.cost == 0.0


def test_record_header_cost_missing_or_unparseable_folds_into_same_warn_bucket() -> None:
    """Streaming-zero and missing/unparseable-header share ONE warning bucket (not two)."""
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    with capture_logs() as cap:
        acc.record_header_cost(t1, None, streaming=False)  # absent
        acc.record_header_cost(t1, "not-a-number", streaming=False)  # unparseable
        acc.record_header_cost(t1, "0.01", streaming=True)  # streaming — same bucket
    assert len(cap) == 1
    result = acc.end_turn(t1, _usage_record())
    assert result.cost == 0.0


def test_record_header_cost_never_substitutes_engine_figure() -> None:
    acc = CostAccountant(source="litellm_headers", wire="openai", prices=None, model_name="m")
    t1 = acc.mint_token()
    acc.begin_turn(t1)
    acc.record_header_cost(t1, "0.02", streaming=False)
    engine_reported = _usage_record(cost=999.0)  # whatever the engine itself reported
    result = acc.end_turn(t1, engine_reported)
    assert result.cost == pytest.approx(0.02)  # harness total wins, engine figure discarded
