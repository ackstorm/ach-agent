# SPDX-License-Identifier: Apache-2.0
"""AC-3/AC-4 integration coverage on a scripted ACH model and price upstream.

These tests make the streaming cost path assertable.  Acceptance claims remain gated
on the P0-v2 price path and B.7 usage-semantics evidence.
"""

from __future__ import annotations

from typing import Any

import aiohttp
import pytest
from aiohttp import web

from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.engine.cost import (
    CostAccountant,
    ModelPrices,
    PriceTable,
    TokenUsage,
    _match_entry,
    compute_cost,
)
from ach_agent.engine.mcp_proxy import ModelProxy


FULLY_PRICED: dict[str, float] = {
    "input_cost_per_token": 1e-6,
    "output_cost_per_token": 2e-6,
    "cache_read_input_token_cost": 1e-7,
    "cache_creation_input_token_cost": 5e-7,
}


def _usage_record() -> OpenCodeUsage:
    return OpenCodeUsage(
        session_id="ses_integration",
        message_id="msg_integration",
        input_tokens=0,
        output_tokens=0,
        cache_read=0,
        cache_write=0,
        cost=0.0,
        duration_ms=1,
    )


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (
            [
                {"model_name": "target", "id": "model-name-match"},
                {"model_name": "alias", "litellm_params": {"model": "target"}},
            ],
            "target",
        ),
        (
            [{"model_name": "alias", "litellm_params": {"model": "target"}}],
            "alias",
        ),
        ([{"model_name": "other"}], None),
    ],
    ids=["model-name-first", "litellm-params-second", "no-entry"],
)
def test_price_match_uses_two_steps_and_has_no_entry_path(
    data: list[dict[str, Any]], expected: str | None
) -> None:
    entry = _match_entry(data, "target")
    if expected is None:
        assert entry is None
    else:
        assert entry is not None
        assert entry["model_name"] == expected


async def _start_fake_ach(
    seen_model_info: list[dict[str, str | None]], seen_requests: list[dict[str, Any]]
) -> tuple[web.AppRunner, str]:
    """Start a scripted ACH upstream with paginated prices and a final-usage SSE chunk."""

    async def handler(request: web.Request) -> web.StreamResponse:
        if request.path == "/v2/model/info":
            seen_model_info.append(
                {
                    "model": request.query.get("model"),
                    "x-ach-key": request.headers.get("x-ach-key"),
                }
            )
            body = {
                "current_page": 1,
                "data": [
                    {"model_name": "priced-model", **FULLY_PRICED},
                    {
                        "model_name": "fallback-model",
                        "input_cost_per_token": 3e-6,
                        "output_cost_per_token": 4e-6,
                    },
                ],
                "size": 2,
                "total_count": 2,
                "total_pages": 1,
            }
            return web.json_response(body)

        if request.path == "/v1/chat/completions":
            sent = await request.json()
            seen_requests.append(sent)
            response = web.StreamResponse(
                status=200, headers={"Content-Type": "text/event-stream"}
            )
            await response.prepare(request)
            await response.write(b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n')
            await response.write(
                b'data: {"choices":[],"usage":{"prompt_tokens":1000,'
                b'"completion_tokens":100,"prompt_tokens_details":{"cached_tokens":400}}}\n\n'
            )
            await response.write(b"data: [DONE]\n\n")
            await response.write_eof()
            return response

        return web.Response(status=404)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    runner = web.AppRunner(app, shutdown_timeout=1.0)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    return runner, f"http://127.0.0.1:{port}"


@pytest.mark.parametrize("engine", ["opencode", "pi"])
async def test_streaming_cache_aware_cost_is_engine_independent_at_model_proxy(
    engine: str,
) -> None:
    """Both engines use the same ModelProxy accounting boundary for an identical wire."""
    seen_model_info: list[dict[str, str | None]] = []
    seen_requests: list[dict[str, Any]] = []
    ach_runner, ach_url = await _start_fake_ach(seen_model_info, seen_requests)
    proxy = ModelProxy()
    try:
        table = PriceTable(ach_url, "ek-integration")
        assert (await table.load("priced-model")).failure is None
        assert (await table.load("fallback-model")).failure is None
        assert table.get("fallback-model") == ModelPrices(3e-6, 4e-6, 3e-6, 3e-6)

        accountant = CostAccountant(
            source="litellm_usage",
            wire="openai",
            prices=table,
            model_name="priced-model",
        )
        proxy_url = await proxy.start(ach_url, "ek-integration", accountant=accountant)
        token = accountant.mint_token()
        accountant.begin_turn(token)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{proxy_url}/t/{token}/v1/chat/completions",
                json={"model": "priced-model", "stream": True, "engine": engine},
            ) as response:
                assert response.status == 200
                body = await response.read()

        assert b'"cached_tokens":400' in body
        assert seen_requests == [
            {
                "model": "priced-model",
                "stream": True,
                "engine": engine,
                "stream_options": {"include_usage": True},
            }
        ]
        assert seen_model_info == [
            {"model": "priced-model", "x-ach-key": "ek-integration"},
            {"model": "fallback-model", "x-ach-key": "ek-integration"},
        ]

        result = accountant.end_turn(token, _usage_record())
        expected, clamped = compute_cost(
            TokenUsage(
                prompt_tokens=1000,
                completion_tokens=100,
                cached_read_tokens=400,
                cache_creation_tokens=0,
            ),
            ModelPrices(**FULLY_PRICED),
        )
        assert clamped is False
        assert result.cost == pytest.approx(expected)
        assert result.cost > 0.0
    finally:
        await proxy.stop()
        await ach_runner.cleanup()
