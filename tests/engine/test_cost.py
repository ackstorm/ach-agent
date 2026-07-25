# SPDX-License-Identifier: Apache-2.0
"""Cache-aware pricing math (A.2)."""

from __future__ import annotations

import pytest

from ach_agent.engine.cost import ModelPrices, TokenUsage, compute_cost


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
