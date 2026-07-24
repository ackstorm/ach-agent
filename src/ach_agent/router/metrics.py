# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the router module (OBS-02).

Exposes:
  - DEDUP_DISCARDS: counter for duplicate events discarded before backpressure
  - BACKPRESSURE_REJECTS: counter for events rejected by maxQueuedTotal check
  - EXPIRE_DROPS: counter for async-no-retry events dropped on full queue

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06, D-08).
"""

from __future__ import annotations

import prometheus_client

# ach_agent_router_dedup_discards_total: incremented when dedup.seen() returns True
# (OBS-02, RTR-01 — dedup MUST precede backpressure, CONTRACT §6.2)
DEDUP_DISCARDS: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_router_dedup_discards_total",
    "Events discarded by the dedup store (duplicate idempotency key)",
)

# ach_agent_router_backpressure_rejects_total: incremented when maxQueuedTotal exceeded
# (OBS-02, RTR-04)
BACKPRESSURE_REJECTS: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_router_backpressure_rejects_total",
    "Events rejected by backpressure (maxQueuedTotal exceeded)",
)

# ach_agent_router_expire_drops_total: incremented when async-no-retry event dropped on full queue
# (OBS-02, RTR-05 — full queue is NEVER silent, CONTRACT §6.4)
EXPIRE_DROPS: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_router_expire_drops_total",
    "Events dropped on full queue (async-no-retry source_trait)",
)

# Persistence fail-open: dedup.db corrupt at startup (DUR-01, D-04b)
PERSISTENCE_DEGRADED: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_router_persistence_degraded_total",
    "dedup.db corrupt at startup — fail-open with fresh store (DUR-01, D-04b)",
)

# Per-channel inbound counter: events received per channel + type (CHN-03/04/05)
CHANNEL_INBOUND: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_channel_inbound_events_total",
    "Events received per channel adapter, labeled by channel name and event type",
    ["channel", "type"],
)

# Memory degraded mode: invocations where memory backend was unreachable (MEM-02, D-02)
MEMORY_DEGRADED: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_memory_degraded_total",
    "Invocations where memory backend was unreachable — fail-open, "
    "running without context (MEM-02, D-02)",
)
