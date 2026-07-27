# SPDX-License-Identifier: Apache-2.0
"""Harness cost accounting: pricing math, per-wire usage parse, attribution.

Subtractive billable-input is the default (A.2): billable_input = max(prompt -
cache_read - cache_creation, 0). Switching to additive (billable_input = prompt_tokens)
for a wire is permitted only on recorded B.7 evidence, cited in the change (U2). The
clamp below is a guard against a wire reporting cache tokens exceeding prompt_tokens —
it is NOT an additive-vs-subtractive detector (spec-revalidation.md §4.3).
"""

from __future__ import annotations

import dataclasses
import json
import math
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import aiohttp
import structlog

from ach_agent.engine.metrics import COST_UNPRICED

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
        self._status = 0
        self._streaming = False
        self._buf = bytearray()
        self._usage: TokenUsage | None = None
        self._over_cap = False

    def begin(self, status: int, content_type: str) -> None:
        self._status = status
        self._streaming = content_type.startswith("text/event-stream")

    @property
    def response_is_success(self) -> bool:
        """Whether a missing usage payload is an A.5 usage-missing condition.

        Non-2xx responses and their error bodies are already logged by the proxy; they
        must not create a second cost warning.
        """
        return 200 <= self._status < 300

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
        if not self.response_is_success:
            return None
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


def validate_cost_source(source: str, model_type: str) -> None:
    """Apply the cost-source/wire boot restrictions (AC-8).

    Two combinations are a deliberate hard-fail:

    * ``litellm_usage`` + ``anthropic`` — usage parsing is implemented for the OpenAI
      and Gemini wires only, and the forwarder serves no ``/anthropic`` route for it.
    * ``litellm_headers`` + any non-OpenAI wire — LiteLLM injects
      ``x-litellm-response-cost`` from its ``/v1`` router only. ``/gemini`` and
      ``/anthropic`` are passthrough routes: measured against LiteLLM 1.93.0, neither
      the plain nor the ``?alt=sse`` Gemini response carries the header in any form, so
      the source would report $0 for every turn while looking perfectly healthy.
    """
    if source == "litellm_usage" and model_type == "anthropic":
        raise ValueError(
            "cost.source=litellm_usage is unsupported with model.type=anthropic: "
            "the forwarder serves no /anthropic route for this usage wire; use "
            "engine, none, or litellm_headers instead"
        )
    if source == "litellm_headers" and model_type != "openai":
        raise ValueError(
            f"cost.source=litellm_headers is unsupported with model.type={model_type}: "
            "LiteLLM injects x-litellm-response-cost from its /v1 router only, and "
            f"/{model_type} is a passthrough route, so every turn would be billed 0; "
            "use litellm_usage (or engine/none)"
        )


def report_price_load_result(failure: PriceFailure | None, model_name: str) -> None:
    """Emit the A.5 boot outcome without turning a price failure into a hard-fail."""
    if failure is None:
        return
    COST_UNPRICED.labels(reason=failure).inc()
    if failure == "fetch_failed":
        log.error("cost: price fetch failed at boot", model=model_name, failure=failure)
        return
    log.warning("cost: model is unpriced", model=model_name, failure=failure)


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


def _parse_price_entry(entry: dict[str, Any]) -> ModelPrices | PriceFailure:
    raw_input = _price_field(entry, "input_cost_per_token")
    raw_output = _price_field(entry, "output_cost_per_token")
    raw_cache_read = _price_field(entry, "cache_read_input_token_cost")
    raw_cache_creation = _price_field(entry, "cache_creation_input_token_cost")

    for raw in (raw_input, raw_output, raw_cache_read, raw_cache_creation):
        if raw is not None and not _is_valid_price(raw):
            return "malformed"

    # Absent/null/zero base input or output — including a partial input-only or
    # output-only pair — is unpriced. Never synthesize the missing base price (A.5).
    if raw_input in (None, 0) or raw_output in (None, 0):
        return "unpriced"

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

    async def load(self, model_name: str) -> PriceFailure | None:
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
                    return "fetch_failed"
                try:
                    body = await resp.json(content_type=None)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return "malformed"
        except (aiohttp.ClientError, TimeoutError):
            return "fetch_failed"

        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return "no_entry"

        entry = _match_entry(data, model_name)
        if entry is None:
            return "no_entry"

        parsed = _parse_price_entry(entry)
        if not isinstance(parsed, ModelPrices):
            return parsed

        self._prices[model_name] = parsed
        return None

    def get(self, model_name: str) -> ModelPrices | None:
        return self._prices.get(model_name)


@dataclass(frozen=True, slots=True)
class TurnTokens:
    """Wire-observed token totals for a turn, summed over its upstream calls.

    Field names match OpenCodeUsage's so end_turn can replace them directly. ``input``
    is the BILLABLE input (prompt net of cache read/creation), keeping the four fields
    disjoint and exactly the terms compute_cost() priced.
    """

    input: int
    output: int
    cache_read: int
    cache_write: int

    @classmethod
    def add(cls, prev: TurnTokens | None, usage: TokenUsage) -> TurnTokens:
        cached = usage.cached_read_tokens + usage.cache_creation_tokens
        billable = max(usage.prompt_tokens - cached, 0)
        base = prev or cls(0, 0, 0, 0)
        return cls(
            input=base.input + billable,
            output=base.output + usage.completion_tokens,
            cache_read=base.cache_read + usage.cached_read_tokens,
            cache_write=base.cache_write + usage.cache_creation_tokens,
        )


def tokenize_model_base_url(url: str, token: str) -> str:
    """Insert /t/<token> after the authority: http://h:p/v1 -> http://h:p/t/<tok>/v1."""
    parts = urlsplit(url)
    return urlunsplit(
        (parts.scheme, parts.netloc, f"/t/{token}{parts.path}", parts.query, parts.fragment)
    )


class _TokenBucket:
    """Per-server-token turn state: accumulated cost + tokens + the warn_once condition set.

    ``tokens`` accumulates the SAME basis the cost is computed from — one entry per
    upstream call of the turn, billable input already net of cache (A.2) — so
    cost / tokens stay divisible (see CostAccountant.end_turn).
    """

    __slots__ = ("cost", "in_flight", "tokens", "warned")

    def __init__(self) -> None:
        self.cost = 0.0
        self.tokens: TurnTokens | None = None
        self.warned: set[str] = set()
        self.in_flight = False


class CostAccountant:
    """Per-server-token cost attribution + the source override at the turn boundary (A.3, A.9).

    EnginePool keys one ManagedServer per session_key and the router serializes strictly
    per session_key (one Lane, FIFO), so at most one invocation per session_key — and
    hence per token — is ever in flight. Attribution below is therefore EXACT, not
    best-effort: a token's bucket cannot be touched by a concurrent turn.
    """

    def __init__(self, source: str, wire: str, prices: PriceTable | None, model_name: str) -> None:
        self.source = source  # public: CostObserver reads this
        self.wire = wire  # public: CostObserver reads this to pick the usage parser
        self._prices = prices
        self._model_name = model_name
        self._buckets: dict[str, _TokenBucket] = {}
        self._unattributed_warned = False

    def mint_token(self) -> str:
        token = secrets.token_urlsafe(16)
        self._buckets[token] = _TokenBucket()
        return token

    def drop_token(self, token: str) -> None:
        self._buckets.pop(token, None)

    @staticmethod
    def _reset(bucket: _TokenBucket, *, in_flight: bool) -> None:
        bucket.cost = 0.0
        bucket.tokens = None
        bucket.warned = set()
        bucket.in_flight = in_flight

    def begin_turn(self, token: str) -> None:
        self._reset(self._buckets.setdefault(token, _TokenBucket()), in_flight=True)

    def _price(self, usage: TokenUsage) -> float:
        prices = self._prices.get(self._model_name) if self._prices is not None else None
        if prices is None:
            # The model never matched a /v2/model/info entry: this response — and every
            # other one — is billed 0. Per-response so rate() sees it, not just boot.
            COST_UNPRICED.labels(reason="unpriced").inc()
            return 0.0
        cost, _clamped = compute_cost(usage, prices)
        return cost

    def record_usage(self, token: str | None, usage: TokenUsage | None) -> None:
        """Attribute usage to token's in-flight turn, or the unattributed tally (A.3)."""
        if usage is None:
            return
        bucket = self._buckets.get(token) if token is not None else None
        if bucket is None or not bucket.in_flight:
            # Unknown token, no token, or a warm-window call outside any in-flight turn —
            # never billed to a turn. One log line per turn boundary (reset in end_turn).
            if not self._unattributed_warned:
                log.warning(
                    "cost: usage could not be attributed to an in-flight invocation",
                    token_known=bucket is not None,
                    model=self._model_name,
                )
                self._unattributed_warned = True
            return
        bucket.cost += self._price(usage)
        bucket.tokens = TurnTokens.add(bucket.tokens, usage)

    def record_header_cost(self, token: str | None, header: str | None, streaming: bool) -> None:
        """litellm_headers mode (A.4): sum x-litellm-response-cost; streaming is 0.0."""
        bucket = self._buckets.get(token) if token is not None else None
        if streaming:
            self.warn_once(
                token, "litellm_headers_unpriced", cost_source=self.source, streaming=True
            )
            return
        cost: float | None = None
        if header is not None:
            try:
                cost = float(header)
            except (TypeError, ValueError):
                cost = None
        if cost is None:
            self.warn_once(
                token, "litellm_headers_unpriced", cost_source=self.source, streaming=False
            )
            return
        if bucket is None or not bucket.in_flight:
            return
        bucket.cost += cost

    def warn_once(self, token: str | None, condition: str, **fields: Any) -> None:
        """Emit at most one log line per condition per invocation; reset on end_turn."""
        bucket = self._buckets.get(token) if token is not None else None
        if bucket is not None:
            if condition in bucket.warned:
                return
            bucket.warned.add(condition)
        log.warning(f"cost: {condition}", **fields)

    def end_turn(self, token: str, usage: Any) -> Any:
        """Read-and-reset the token's bucket; apply the source override exactly once (A.9).

        Cost and tokens are overridden TOGETHER so they share one basis: the sum over
        every upstream call the turn made. The engine's own numbers cover roughly the
        last message only, so keeping them next to a whole-turn cost made $/token wrong
        by the turn's call count (~16x on a 10-tool-call turn). ``litellm_headers`` gives
        no token data, so it overrides cost only and the engine's tokens stand.
        """
        bucket = self._buckets.get(token)
        total = bucket.cost if bucket is not None else 0.0
        tokens = bucket.tokens if bucket is not None else None
        if bucket is not None:
            self._reset(bucket, in_flight=False)
        self._unattributed_warned = False  # a turn boundary — allow one more unattributed warn
        if self.source == "engine" or usage is None:
            return usage
        if tokens is None:
            return dataclasses.replace(usage, cost=total)
        return dataclasses.replace(
            usage,
            cost=total,
            input_tokens=tokens.input,
            output_tokens=tokens.output,
            cache_read=tokens.cache_read,
            cache_write=tokens.cache_write,
        )

    def discard_turn(self, token: str) -> None:
        """Idempotent reset for the finally path — never disturbs a bucket end_turn already took."""
        bucket = self._buckets.get(token)
        if bucket is not None:
            self._reset(bucket, in_flight=False)


def _inject_include_usage(body: bytes) -> bytes:
    """OpenAI wire only (A.1): on a streaming request whose stream_options.include_usage
    is absent or false, set it true. Never otherwise; any parse failure returns body as-is."""
    if not body:
        return body
    try:
        obj = json.loads(body)
    except (ValueError, TypeError):
        return body
    if not isinstance(obj, dict) or obj.get("stream") is not True:
        return body
    stream_options = obj.get("stream_options")
    if isinstance(stream_options, dict) and stream_options.get("include_usage") is True:
        return body
    new_options = dict(stream_options) if isinstance(stream_options, dict) else {}
    new_options["include_usage"] = True
    obj = {**obj, "stream_options": new_options}
    try:
        return json.dumps(obj).encode("utf-8")
    except (TypeError, ValueError):
        return body


class CostObserver:
    """Per-request observer passed to mcp_proxy._forward, for litellm_usage (parses
    wire usage, injects OpenAI's include_usage) and litellm_headers (reads the response
    x-litellm-response-cost header) — the two sources that ever get an observer wired in.

    Bound to exactly one attribution token per request. Every method is defensive: a
    parse failure here must never alter the relayed bytes (A.12) — callers additionally
    isolate each call, but this class never lets an internal error escape either.
    """

    def __init__(self, accountant: CostAccountant, token: str | None) -> None:
        self._accountant = accountant
        self._token = token
        self._usage_observer = (
            UsageObserver(accountant.wire)
            if accountant.source == "litellm_usage" and accountant.wire in _USAGE_PARSERS
            else None
        )

    def mutate_request(self, body: bytes, content_type: str) -> bytes:
        # A.4: litellm_headers NEVER mutates "stream" or any other request field.
        if self._accountant.source != "litellm_usage" or self._accountant.wire != "openai":
            return body
        return _inject_include_usage(body)

    def begin(self, status: int, content_type: str) -> None:
        if self._usage_observer is not None:
            self._usage_observer.begin(status, content_type)

    def response_headers(self, headers: Mapping[str, str]) -> None:
        """litellm_headers mode (A.4): read x-litellm-response-cost once headers arrive."""
        if self._accountant.source != "litellm_headers":
            return
        streaming = headers.get("Content-Type", "").startswith("text/event-stream")
        self._accountant.record_header_cost(
            self._token, headers.get("x-litellm-response-cost"), streaming
        )

    def feed(self, chunk: bytes) -> None:
        if self._usage_observer is not None:
            self._usage_observer.feed(chunk)

    def finish(self) -> None:
        if self._usage_observer is None:
            return
        usage = self._usage_observer.finish()
        if usage is None and self._usage_observer.response_is_success:
            COST_UNPRICED.labels(reason="usage_missing").inc()
            self._accountant.warn_once(
                self._token,
                "usage_missing",
                cost_source=self._accountant.source,
                model=self._accountant._model_name,
                wire=self._accountant.wire,
            )
        self._accountant.record_usage(self._token, usage)
