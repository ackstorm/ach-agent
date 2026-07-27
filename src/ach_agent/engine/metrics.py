# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the engine module.

Exposes:
  - ENGINE_WATCHDOG_KILLS: counter incremented each time the maxInvocationSeconds
    watchdog kills an overrunning opencode subprocess (ENG-07, T-00-RUNAWAY).

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import prometheus_client

# ach_agent_engine_watchdog_kills_total: incremented by lifecycle.py run_invocation on
# asyncio.TimeoutError from the maxInvocationSeconds watchdog (ENG-07, D-03).
ENGINE_WATCHDOG_KILLS: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_engine_watchdog_kills_total",
    "opencode subprocesses killed by the maxInvocationSeconds watchdog",
)

# ach_agent_engine_drain_completed_total: incremented by main.py _drain() on graceful
# SIGTERM → sys.exit(0) completion (DUR-03, spec §2176).
DRAIN_COMPLETED: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_engine_drain_completed_total",
    "Graceful drain completed successfully (SIGTERM → sys.exit(0)) (DUR-03)",
)

# ach_agent_engine_launch_failures_total: incremented by main.py engine_runner when
# pool.acquire() raises — the opencode agente could not be launched for this
# session_key. Explicit observability (no silent drop) now that acceptance is
# decoupled from engine readiness.
ENGINE_LAUNCH_FAILURES: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_engine_launch_failures_total",
    "opencode agente launches that failed in engine_runner (pool.acquire raised)",
)

# ach_agent_cost_unpriced_total: every point where cost accounting gives up and a turn is
# billed 0. Boot reasons (fetch_failed/no_entry/malformed/unpriced) fire ONCE at startup —
# alert on `> 0`, not on rate(). The per-response reasons (unpriced, usage_missing) keep
# firing, so a silently-$0 agent is visible in rate() too. A model name that /v2/model/info
# does not know (e.g. the un-namespaced "gemini-flash-latest") is the common trap.
COST_UNPRICED: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_cost_unpriced_total",
    "Cost accounting gave up: the turn contributes 0 USD",
    ["reason"],
)
