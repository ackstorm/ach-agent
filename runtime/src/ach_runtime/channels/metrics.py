# SPDX-License-Identifier: MIT
"""Metrics copied for the existing webhook and deterministic script channel paths."""

from prometheus_client import Counter

CHANNEL_INBOUND = Counter(
    "ach_agent_channel_inbound_events_total",
    "Events received per channel adapter, labeled by channel name and event type",
    ["channel", "type"],
)

WEBHOOK_SCRIPT_FAILURES = Counter(
    "ach_agent_webhook_script_failures_total",
    "webhook-script handlers that failed after the event was admitted",
    ["reason"],
)

WEBHOOK_SCRIPT_RUNS = Counter(
    "ach_agent_webhook_script_runs_total",
    "webhook-script handlers run, labeled by channel and outcome",
    ["channel", "status"],
)
