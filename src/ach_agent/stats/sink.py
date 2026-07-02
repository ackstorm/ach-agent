# SPDX-License-Identifier: Apache-2.0
"""StatsSink — best-effort, non-blocking per-invocation stats.

record() does ONLY inline Prometheus increments + queue.put_nowait(). It never touches redis. A
single supervised writer task (Task 5) owns the redis client and drains the bounded queue. See
design spec §5.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any, cast

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

    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import redis.asyncio as redis_asyncio

        # redis.asyncio.from_url has no return annotation upstream, so --strict flags it
        # as an untyped call. Route it through a typed factory alias (see channels/queue.py).
        from_url = cast(Callable[..., Any], redis_asyncio.from_url)
        return from_url(
            self._redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )

    async def start(self) -> None:
        if self._queue is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="stats-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2.0)
            except TimeoutError:
                pass
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        assert self._queue is not None
        backoff = 1.0
        last_log = 0.0
        while True:
            client = None
            try:
                client = self._make_client()
                while True:
                    stat = await self._queue.get()
                    try:
                        minid = int((time.time() - self._retention_s) * 1000)
                        await client.xadd(
                            "ach:sessions",
                            stat.to_entry(),
                            minid=minid,
                            approximate=True,
                        )
                        backoff = 1.0
                    finally:
                        self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the writer die silently
                now = time.time()
                if now - last_log > 10:
                    log.warning("stats: redis writer error", error=str(exc), backoff=backoff)
                    last_log = now
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass


def build_session_stat(
    event: Any, obj: dict[str, Any], turn_stats: dict[str, Any], *, ts_ms: int
) -> SessionStat:
    """Map the engine turn-summary outputs to a SessionStat. Pure; unit-testable."""
    usage = turn_stats.get("usage")
    aborted = bool(turn_stats.get("aborted"))
    return SessionStat.build(
        ts_ms=ts_ms,
        session_key=getattr(event, "session_key", "unknown"),
        channel=getattr(event, "channel_name", "unknown"),
        source=getattr(event, "source", getattr(event, "channel_name", "unknown")),
        model=str(obj.get("model", "unknown")),
        provider="unknown",  # provider is resolved by the stats service's model-map (A2), not here
        raw_task=str(obj.get("text", "")),
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_read=getattr(usage, "cache_read", 0),
        cache_write=getattr(usage, "cache_write", 0),
        cost=getattr(usage, "cost", 0.0),
        turns=int(turn_stats.get("tool_count", 0)),
        duration_ms=getattr(usage, "duration_ms", 0),
        status="aborted" if aborted else "completed",
        retry=bool(turn_stats.get("retry", False)),
    )
