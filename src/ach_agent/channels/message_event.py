# SPDX-License-Identifier: Apache-2.0
"""Canonical MessageEvent — the ONLY type that crosses the channel→router seam.

RTR-06: channels and the router communicate ONLY via this type.
Engine never imports this module (D-08).

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal


@dataclass(slots=True)
class MessageEvent:
    """Canonical inbound event crossing the channel→router seam.

    Fields:
        idempotency_key:  Derived adapter-side (per D-06). Never empty/None —
                          fallback is ms-timestamp (IDM-01/02, Pitfall 1).
        session_key:      Per-session FIFO lane key. For cron: channel name (D-08).
        channel_name:     Which channel produced this event (for dedup namespace).
        payload:          Channel-specific raw payload (opaque to router).
        delivery_context: Channel-specific delivery coordinates (opaque to router).
                          Webhook sets project_id/mr_iid per D-07; cron leaves empty.
        source_trait:     "sync" (can return 503) or "async_no_retry"
                          (cron/fire-and-forget: drop+log on full queue, RTR-05).
        received_at:      UTC timestamp when event entered the harness.
        reply_future:     Future set by engine_runner with the reply text for
                          deliver.type=="reply" channels (CR-01 / ACT-01).
                          None = async/gitlab_comment mode (default, unchanged path).
                          When not None, engine_runner MUST call set_result or
                          set_exception so the route never hangs.
        task_id:          Correlation id echoed to the caller on 202; empty for channels
                          that don't set one.
    """

    idempotency_key: str
    session_key: str
    channel_name: str
    # Optional SECONDARY dedup key — the GitLab logical content composite (gitlab source
    # only in v1; None for every other channel). Checked & marked alongside the primary in
    # Router.handle on a SHORT window, catching logical double-fires the UUID key misses.
    secondary_idempotency_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    delivery_context: dict[str, Any] = field(default_factory=dict)
    source_trait: Literal["sync", "async_no_retry"] = "async_no_retry"
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    reply_future: asyncio.Future[str] | None = field(default=None, compare=False, repr=False)
    # Correlation id (uuid4 hex) echoed to the caller on the webhook 202 accept and logged
    # by engine_runner for log/trace correlation ONLY — not persisted, not queryable.
    task_id: str = ""
