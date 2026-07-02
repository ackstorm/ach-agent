# SPDX-License-Identifier: Apache-2.0
"""StatsSink — best-effort, non-blocking per-invocation stats.

record() does ONLY inline Prometheus increments + queue.put_nowait(). It never touches redis. A
single supervised writer task (Task 5) owns the redis client and drains the bounded queue. See
design spec §5.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import structlog

from ach_agent.stats import metrics
from ach_agent.stats.models import SessionStat

log = structlog.get_logger()

_DEFAULT_RETENTION_S = 3_024_000  # 35 days


class StatsSink:
    def __init__(
        self,
        redis_url: str | None,
        *,
        retention_s: int = _DEFAULT_RETENTION_S,
        maxsize: int = 256,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._retention_s = retention_s
        self._client_factory = client_factory
        self._queue: asyncio.Queue[SessionStat] | None = (
            asyncio.Queue(maxsize=maxsize) if redis_url else None
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._redis_url is not None

    def record(self, stat: SessionStat) -> None:
        """Inline metrics + non-blocking enqueue. Never awaits, never raises."""
        try:
            metrics.observe(stat)
        except Exception:  # noqa: BLE001 — metrics must never break a turn
            pass
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(stat)
        except asyncio.QueueFull:
            metrics.STATS_DEGRADED.inc()
