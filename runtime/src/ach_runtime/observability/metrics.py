# SPDX-License-Identifier: MIT
"""Existing low-cardinality Prometheus completion metrics and scrape identity."""

from __future__ import annotations

import math
from copy import copy
from typing import Any

from prometheus_client import REGISTRY, Counter, Histogram
from prometheus_client.registry import Collector

from ach_runtime.facades._proxy import ProcessIdentity

_TURN_BUCKETS = (0.5, 1, 2.5, 5, 10, 30, 60, 120, 300, 600, 1200, 1800, float("inf"))

SESSIONS_TOTAL = Counter(
    "ach_agent_sessions_total", "Invocations recorded", ["model", "channel"]
)
TURN_TOKENS_TOTAL = Counter(
    "ach_agent_turn_tokens_total", "Reported token usage by direction", ["model", "direction"]
)
TURN_COST_USD_TOTAL = Counter(
    "ach_agent_turn_cost_usd_total", "Reported native engine-estimated USD", ["model", "channel"]
)
TURNS_TOTAL = Counter(
    "ach_agent_turns_total", "Invocation completions by model and channel", ["model", "channel"]
)
TURN_DURATION_SECONDS = Histogram(
    "ach_agent_turn_duration_seconds", "Invocation duration", ["model"], buckets=_TURN_BUCKETS
)
COST_UNPRICED = Counter(
    "ach_agent_cost_unpriced_total",
    "Invocations whose cost metadata is unknown; this does not mean zero billing",
    ["reason"],
)
ANOMALIES_TOTAL = Counter(
    "ach_agent_anomalies_total", "Operational anomalies by finite kind", ["kind"]
)
RUNS_TOTAL = Counter(
    "ach_agent_runs_total", "Recorded invocations by channel and outcome", ["channel", "outcome"]
)


def observe(record: Any) -> None:
    RUNS_TOTAL.labels(channel=record.channel, outcome=record.outcome).inc()
    if record.outcome == "skipped":
        # sessionStart decided not to run: no engine turn, nothing billed or unpriced.
        return
    labels = {"model": record.model, "channel": record.channel}
    TURNS_TOTAL.labels(**labels).inc()
    SESSIONS_TOTAL.labels(**labels).inc()
    usage = record.usage
    if usage is None or usage.cost_usd is None:
        COST_UNPRICED.labels(reason="usage_missing").inc()
    invalid = False
    if usage is not None:
        for direction, amount in (
            ("input", usage.input), ("output", usage.output),
            ("reasoning", usage.reasoning), ("cache_read", usage.cache_read),
            ("cache_write", usage.cache_write),
        ):
            if amount is None:
                continue
            if not _valid_nonnegative(amount):
                invalid = True
            else:
                TURN_TOKENS_TOTAL.labels(model=record.model, direction=direction).inc(amount)
        if usage.cost_usd is not None:
            if _valid_nonnegative(usage.cost_usd):
                TURN_COST_USD_TOTAL.labels(**labels).inc(usage.cost_usd)
            else:
                invalid = True
        if usage.steps is not None and (not isinstance(usage.steps, int) or usage.steps < 0):
            invalid = True
    duration = record.duration_ms
    if duration is not None:
        if _valid_nonnegative(duration):
            TURN_DURATION_SECONDS.labels(model=record.model).observe(duration / 1000)
        else:
            invalid = True
    if invalid:
        from ach_runtime.observability.runs import alarm

        alarm("usage_invalid", invocation_id=record.invocation_id,
              workspace_ref=record.workspace_ref, session_ref=record.session_ref)


def _valid_nonnegative(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


class IdentityRegistry:
    """Copy scrape samples and attach immutable process identity labels."""

    def __init__(self, registry: Collector = REGISTRY, *, identity: ProcessIdentity) -> None:
        self.registry = registry
        self.identity = identity

    def collect(self):
        for original in self.registry.collect():
            samples = [
                sample._replace(labels={**sample.labels, "agent": self.identity.agent,
                                        "environment": self.identity.environment})
                for sample in original.samples
            ]
            if not samples:
                continue
            metric = copy(original)
            metric.samples = samples
            yield metric

    def describe(self):
        for metric in self.collect():
            yield metric

__all__ = [
    "ANOMALIES_TOTAL", "COST_UNPRICED", "IdentityRegistry", "SESSIONS_TOTAL",
    "TURN_COST_USD_TOTAL", "TURN_DURATION_SECONDS", "TURN_TOKENS_TOTAL", "TURNS_TOTAL",
    "_TURN_BUCKETS", "observe",
]
