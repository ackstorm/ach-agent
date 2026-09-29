from __future__ import annotations

import dataclasses
import json

import httpx
import pytest

from ach_agent.engine import trace
from ach_agent.engine.base.events import OpenCodeUsage
from ach_agent.engine.cost import CostAccountant, ModelPrices, PriceTable, TokenUsage
from ach_agent.execution.wire import ExecutionHandle


@pytest.mark.asyncio
@pytest.mark.parametrize("engine", ["opencode", "pi"])
@pytest.mark.parametrize("cost_source", ["engine", "proxy", "none"])
async def test_first_split_session_ack_has_trace_and_native_session(
    engine: str, cost_source: str
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient

    token = trace.mint_token()
    trace.begin(token, "agent", "webhook", "delivery")
    handle = ExecutionHandle(
        instance_id="instance",
        controller_id="controller",
        execution_id="execution",
        invocation_id="invocation",
        proxy_route=token,
    )
    seen: dict[str, str] = {}
    event_ref = "ses_native" if engine == "opencode" else "/pi/sess.json"
    usage = {
        "session_id": "ses_native",
        "message_id": "message",
        "input_tokens": 11,
        "output_tokens": 7,
        "cache_read": 3,
        "cache_write": 2,
        "cost": 0.42,
        "duration_ms": 125,
    }
    events = [
        {
            "kind": "session_resolved",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {"session_ref": event_ref},
        },
        {
            "kind": "usage",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": usage,
        },
        {
            "kind": "turn_done",
            "execution_id": "execution",
            "invocation_id": "invocation",
            "turn_id": "turn-1",
            "payload": {"text": "reply", "session_ref": event_ref, "stats": {"tool_count": 2}},
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/execution/v1/session-ready":
            seen.update(trace.headers(token))
            return httpx.Response(200, json={"status": "ok"}, request=request)
        if request.url.path == "/execution/v1/turn":
            body = b"".join(json.dumps(event).encode() + b"\n" for event in events)
            return httpx.Response(200, content=body, request=request)
        return httpx.Response(200, json={"status": "ok"}, request=request)

    client = ExecutionClient(
        "http://execution", controller_id="controller", transport=httpx.MockTransport(handler)
    )
    client._handles["invocation"] = handle
    client._turn_ids["invocation"] = iter([1])  # type: ignore[assignment]
    stats: dict[str, object] = {}
    result = await client.turn_callable(handle)(
        prompt="prompt", max_tool_calls=0, on_text=None, on_tool=None, stats=stats
    )
    assert result.session_ref == event_ref
    usage_result = stats["usage"]
    assert isinstance(usage_result, OpenCodeUsage)
    assert usage_result.cache_read == 3
    assert usage_result.cache_write == 2
    assert usage_result.duration_ms == 125
    assert stats["tool_count"] == 2
    assert seen["langfuse_session_id"] == trace.session_id_for(event_ref)
    assert "traceparent" in seen
    if cost_source == "engine":
        effective = usage_result
    elif cost_source == "proxy":
        table = PriceTable("http://unused", "ek")
        table._prices["model"] = ModelPrices(0.1, 0.2, 0.01, 0.02)
        accountant = CostAccountant("litellm_usage", "openai", table, "model")
        accountant.adopt_token(token)
        accountant.begin_turn(token)
        accountant.record_usage(
            token,
            TokenUsage(
                prompt_tokens=usage_result.input_tokens,
                completion_tokens=usage_result.output_tokens,
                cached_read_tokens=usage_result.cache_read,
                cache_creation_tokens=usage_result.cache_write,
            ),
        )
        effective = accountant.end_turn(token, usage_result)
    else:
        effective = dataclasses.replace(usage_result, cost=0.0)
    assert effective.cost == pytest.approx(
        {"engine": 0.42, "proxy": 2.07, "none": 0.0}[cost_source]
    )
    await client.close()
    trace.drop(token)
