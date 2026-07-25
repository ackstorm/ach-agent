# SPDX-License-Identifier: Apache-2.0
"""Harness cost accounting: pricing math, per-wire usage parse, attribution.

Subtractive billable-input is the default (A.2): billable_input = max(prompt -
cache_read - cache_creation, 0). Switching to additive (billable_input = prompt_tokens)
for a wire is permitted only on recorded B.7 evidence, cited in the change (U2). The
clamp below is a guard against a wire reporting cache tokens exceeding prompt_tokens —
it is NOT an additive-vs-subtractive detector (spec-revalidation.md §4.3).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Bounds the non-streaming body buffer (mirrors tags.go's maxBodyForTagInjection). A body
# over this cap yields finish() -> None (an A.5 "usage never arrived" row) rather than an
# unbounded in-memory buffer.
_MAX_BODY_BYTES = 1024 * 1024


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


def _usage_from_openai(obj: dict[str, Any]) -> TokenUsage | None:
    usage = obj.get("usage")
    if not isinstance(usage, dict):
        return None
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens", 0) if isinstance(details, dict) else 0
    return TokenUsage(
        prompt_tokens=int(usage.get("prompt_tokens", 0) or 0),
        completion_tokens=int(usage.get("completion_tokens", 0) or 0),
        cached_read_tokens=int(cached or 0),
        cache_creation_tokens=int(usage.get("cache_creation_input_tokens", 0) or 0),
    )


def _usage_from_gemini(obj: dict[str, Any]) -> TokenUsage | None:
    meta = obj.get("usageMetadata")
    if not isinstance(meta, dict):
        return None
    return TokenUsage(
        prompt_tokens=int(meta.get("promptTokenCount", 0) or 0),
        # thinking bills at the output rate (A.1) — folded into completion here.
        completion_tokens=int(meta.get("candidatesTokenCount", 0) or 0)
        + int(meta.get("thoughtsTokenCount", 0) or 0),
        cached_read_tokens=int(meta.get("cachedContentTokenCount", 0) or 0),
        cache_creation_tokens=0,  # gemini implicit caching has no creation cost (A.1)
    )


_USAGE_PARSERS = {"openai": _usage_from_openai, "gemini": _usage_from_gemini}


class UsageObserver:
    """Line-buffered SSE / JSON observer extracting the final usage payload.

    Parses a COPY of the relayed bytes — never gates or mutates the proxy write (A.12,
    N7/N8). Logs nothing itself; callers must log usage fields only, never bodies.
    """

    def __init__(self, wire: str) -> None:
        self._parse = _USAGE_PARSERS[wire]
        self._streaming = False
        self._buf = bytearray()
        self._usage: TokenUsage | None = None
        self._over_cap = False

    def begin(self, status: int, content_type: str) -> None:
        self._streaming = content_type.startswith("text/event-stream")

    def feed(self, chunk: bytes) -> None:
        if self._over_cap:
            return
        self._buf.extend(chunk)
        if len(self._buf) > _MAX_BODY_BYTES:
            self._over_cap = True
            self._buf.clear()
            return
        if self._streaming:
            self._drain_lines()

    def _drain_lines(self) -> None:
        while b"\n" in self._buf:
            line, _, rest = self._buf.partition(b"\n")
            self._buf = bytearray(rest)
            self._parse_line(bytes(line))

    def _parse_line(self, line: bytes) -> None:
        text = line.strip()
        if text.startswith(b"data:"):
            text = text[len(b"data:") :].strip()
        if not text or text == b"[DONE]":
            return
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            return
        if not isinstance(obj, dict):
            return
        usage = self._parse(obj)
        if usage is not None:
            self._usage = usage

    def finish(self) -> TokenUsage | None:
        """Return the final parsed usage, or None if the body exceeded the cap."""
        if self._over_cap:
            return None
        if self._streaming:
            if self._buf:
                self._parse_line(bytes(self._buf))
                self._buf.clear()
        else:
            try:
                obj = json.loads(bytes(self._buf))
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                usage = self._parse(obj)
                if usage is not None:
                    self._usage = usage
        return self._usage
