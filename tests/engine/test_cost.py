# SPDX-License-Identifier: Apache-2.0
"""Cache-aware pricing math (A.2)."""

from __future__ import annotations

import pytest

from ach_agent.engine.cost import ModelPrices, TokenUsage, UsageObserver, compute_cost


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
    u = TokenUsage(prompt_tokens=100, completion_tokens=0, cached_read_tokens=400, cache_creation_tokens=0)
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
