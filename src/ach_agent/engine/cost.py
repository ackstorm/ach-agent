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
import math
from dataclasses import dataclass
from typing import Any, Literal

import aiohttp
import structlog

log = structlog.get_logger(__name__)

# Bounds the non-streaming body buffer (mirrors tags.go's maxBodyForTagInjection). A body
# over this cap yields finish() -> None (an A.5 "usage never arrived" row) rather than an
# unbounded in-memory buffer.
_MAX_BODY_BYTES = 1024 * 1024

# Boot-path price load must not stall startup indefinitely on an unreachable upstream — a
# finite timeout turns a hang into a typed fetch_failed result (B.5).
_DEFAULT_PRICE_TIMEOUT_S = 10.0


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


PriceFailure = Literal["fetch_failed", "no_entry", "unpriced", "malformed"]


@dataclass(frozen=True, slots=True)
class PriceLoadResult:
    failure: PriceFailure | None


def _price_field(entry: dict[str, Any], key: str) -> Any:
    """Read a price field from the entry top level, falling back to model_info (U4)."""
    if key in entry:
        return entry[key]
    model_info = entry.get("model_info")
    if isinstance(model_info, dict) and key in model_info:
        return model_info[key]
    return None


def _is_valid_price(raw: Any) -> bool:
    return (
        isinstance(raw, int | float)
        and not isinstance(raw, bool)
        and math.isfinite(raw)
        and raw >= 0
    )


def _parse_price_entry(entry: dict[str, Any], model_name: str) -> ModelPrices | PriceLoadResult:
    raw_input = _price_field(entry, "input_cost_per_token")
    raw_output = _price_field(entry, "output_cost_per_token")
    raw_cache_read = _price_field(entry, "cache_read_input_token_cost")
    raw_cache_creation = _price_field(entry, "cache_creation_input_token_cost")

    for raw in (raw_input, raw_output, raw_cache_read, raw_cache_creation):
        if raw is not None and not _is_valid_price(raw):
            return PriceLoadResult(failure="malformed")

    # Absent/null/zero base input or output — including a partial input-only or
    # output-only pair — is unpriced. Never synthesize the missing base price (A.5).
    if raw_input in (None, 0) or raw_output in (None, 0):
        log.warning("cost: model has no priced base input/output rate", model=model_name)
        return PriceLoadResult(failure="unpriced")

    cache_read = raw_cache_read if raw_cache_read is not None else raw_input
    cache_creation = raw_cache_creation if raw_cache_creation is not None else raw_input
    return ModelPrices(
        input_cost_per_token=float(raw_input),
        output_cost_per_token=float(raw_output),
        cache_read_input_token_cost=float(cache_read),
        cache_creation_input_token_cost=float(cache_creation),
    )


def _match_entry(data: list[Any], model_name: str) -> dict[str, Any] | None:
    """Two-step match (B.5): model_name first, then litellm_params.model. No pagination —
    the ?model= filter is expected to place a match on page 1 (deliberate ceiling)."""
    for entry in data:
        if isinstance(entry, dict) and entry.get("model_name") == model_name:
            return entry
    for entry in data:
        if isinstance(entry, dict):
            params = entry.get("litellm_params")
            if isinstance(params, dict) and params.get("model") == model_name:
                return entry
    return None


class PriceTable:
    """In-process price cache used only by cost.source=litellm_usage.

    ``ach_key`` is request-scoped: used only for the GET below, kept on a private
    attribute for reuse across ``load()`` calls (refreshed on model change), and never
    included in repr/str/logs/exception text (B.5 credential hygiene).
    """

    def __init__(
        self, base_url: str, ach_key: str, *, timeout_seconds: float = _DEFAULT_PRICE_TIMEOUT_S
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._ach_key = ach_key
        self._timeout = aiohttp.ClientTimeout(total=timeout_seconds)
        self._prices: dict[str, ModelPrices] = {}

    async def load(self, model_name: str) -> PriceLoadResult:
        """GET /v2/model/info?model=<model_name> and cache the matched entry's prices."""
        url = f"{self._base_url}/v2/model/info"
        try:
            async with (
                aiohttp.ClientSession(timeout=self._timeout) as session,
                session.get(
                    url, params={"model": model_name}, headers={"x-ach-key": self._ach_key}
                ) as resp,
            ):
                if resp.status >= 400:
                    return PriceLoadResult(failure="fetch_failed")
                body = await resp.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError):
            return PriceLoadResult(failure="fetch_failed")

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return PriceLoadResult(failure="no_entry")

        entry = _match_entry(data, model_name)
        if entry is None:
            return PriceLoadResult(failure="no_entry")

        parsed = _parse_price_entry(entry, model_name)
        if isinstance(parsed, PriceLoadResult):
            return parsed

        self._prices[model_name] = parsed
        return PriceLoadResult(failure=None)

    def get(self, model_name: str) -> ModelPrices | None:
        return self._prices.get(model_name)
