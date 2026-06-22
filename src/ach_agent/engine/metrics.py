# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the engine module.

Exposes:
  - ENGINE_WATCHDOG_KILLS: counter incremented each time the maxInvocationSeconds
    watchdog kills an overrunning opencode subprocess (ENG-07, T-00-RUNAWAY).

Constraint: No router or Hermes imports (D-08, RTR-06).
"""

from __future__ import annotations

import prometheus_client

# engine_watchdog_kills_total: incremented by lifecycle.py run_invocation on
# asyncio.TimeoutError from the maxInvocationSeconds watchdog (ENG-07, D-03).
ENGINE_WATCHDOG_KILLS: prometheus_client.Counter = prometheus_client.Counter(
    "engine_watchdog_kills_total",
    "opencode subprocesses killed by the maxInvocationSeconds watchdog",
)

# engine_drain_completed_total: incremented by main.py _drain() on graceful
# SIGTERM → sys.exit(0) completion (DUR-03, spec §2176).
DRAIN_COMPLETED: prometheus_client.Counter = prometheus_client.Counter(
    "engine_drain_completed_total",
    "Graceful drain completed successfully (SIGTERM → sys.exit(0)) (DUR-03)",
)
