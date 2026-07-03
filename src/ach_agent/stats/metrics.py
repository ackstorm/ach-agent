# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the stats sink (design spec §4.3).

Low-cardinality labels only (model, channel, provider). NEVER session_key or task as labels.
Exposed via the already-mounted /metrics (http/app.py). Follows router/metrics.py conventions.
"""

from __future__ import annotations

import prometheus_client

from ach_agent.stats.models import SessionStat, ToolStat

SESSIONS_TOTAL = prometheus_client.Counter(
    "ach_agent_sessions_total", "Invocations recorded", ["model", "channel"]
)
TURN_TOKENS_TOTAL = prometheus_client.Counter(
    "ach_agent_turn_tokens_total", "Tokens by direction", ["model", "direction"]
)
TURN_COST_USD_TOTAL = prometheus_client.Counter(
    "ach_agent_turn_cost_usd_total", "Cost in USD", ["model", "channel"]
)
TURNS_TOTAL = prometheus_client.Counter(
    "ach_agent_turns_total", "Within-invocation loop/tool count", ["model", "channel"]
)
TURN_DURATION_SECONDS = prometheus_client.Histogram(
    "ach_agent_turn_duration_seconds", "Invocation duration", ["model"]
)
STATS_DEGRADED = prometheus_client.Counter(
    "ach_agent_stats_degraded_total", "Session records dropped (queue full / writer error)"
)

# Per-tool (Tier 1 agent trace). Labels are low-cardinality: tool is the cleaned name (finite
# tool set), tool_type ∈ {mcp,builtin}, status ∈ {completed,error}. NEVER session_key.
# gen_ai.execute_tool.duration analogue; count carries the pass/fail split.
TOOL_CALLS_TOTAL = prometheus_client.Counter(
    "ach_agent_tool_calls_total", "Tool executions", ["tool", "tool_type", "status"]
)
TOOL_DURATION_SECONDS = prometheus_client.Histogram(
    "ach_agent_tool_duration_seconds", "Per-tool execution duration", ["tool", "tool_type"]
)


def observe(stat: SessionStat) -> None:
    """Apply all counters/histogram from one invocation record. Always safe, in-process."""
    SESSIONS_TOTAL.labels(stat.model, stat.channel).inc()
    TURN_TOKENS_TOTAL.labels(stat.model, "input").inc(stat.input_tokens)
    TURN_TOKENS_TOTAL.labels(stat.model, "output").inc(stat.output_tokens)
    TURN_TOKENS_TOTAL.labels(stat.model, "cache_read").inc(stat.cache_read)
    TURN_TOKENS_TOTAL.labels(stat.model, "cache_write").inc(stat.cache_write)
    TURN_COST_USD_TOTAL.labels(stat.model, stat.channel).inc(stat.cost)
    TURNS_TOTAL.labels(stat.model, stat.channel).inc(stat.turns)
    TURN_DURATION_SECONDS.labels(stat.model).observe(stat.duration_ms / 1000.0)


def observe_tool(stat: ToolStat) -> None:
    """One tool call → count (always) + duration histogram (only when a start was stamped)."""
    TOOL_CALLS_TOTAL.labels(stat.tool, stat.tool_type, stat.status).inc()
    if stat.duration_ms is not None:
        TOOL_DURATION_SECONDS.labels(stat.tool, stat.tool_type).observe(stat.duration_ms / 1000.0)
