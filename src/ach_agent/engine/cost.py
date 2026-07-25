# SPDX-License-Identifier: Apache-2.0
"""Harness cost accounting: pricing math, per-wire usage parse, attribution.

Subtractive billable-input is the default (A.2): billable_input = max(prompt -
cache_read - cache_creation, 0). Switching to additive (billable_input = prompt_tokens)
for a wire is permitted only on recorded B.7 evidence, cited in the change (U2). The
clamp below is a guard against a wire reporting cache tokens exceeding prompt_tokens —
it is NOT an additive-vs-subtractive detector (spec-revalidation.md §4.3).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ModelPrices:
    input_cost_per_token: float
    output_cost_per_token: float
    cache_read_input_token_cost: float  # fallback: input_cost_per_token
    cache_creation_input_token_cost: float  # fallback: input_cost_per_token


@dataclass(frozen=True, slots=True)
class TokenUsage:
    prompt_tokens: int
    completion_tokens: int
    cached_read_tokens: int
    cache_creation_tokens: int


def compute_cost(usage: TokenUsage, prices: ModelPrices) -> tuple[float, bool]:
    """Return (cost_usd, clamped)."""
    raw_billable = usage.prompt_tokens - usage.cached_read_tokens - usage.cache_creation_tokens
    billable_input = max(raw_billable, 0)
    clamped = raw_billable < 0
    cost = (
        billable_input * prices.input_cost_per_token
        + usage.cached_read_tokens * prices.cache_read_input_token_cost
        + usage.cache_creation_tokens * prices.cache_creation_input_token_cost
        + usage.completion_tokens * prices.output_cost_per_token
    )
    return cost, clamped
