# SPDX-License-Identifier: Apache-2.0
"""Router — wires dedup → backpressure → lane in normative order.

CONTRACT §6.2 / spec §18.8/§29: ORDER IS NORMATIVE.
Dedup MUST precede backpressure. A duplicate must NOT consume a queue slot.

RTR-06 constraint: NEVER import from hermes_agent.* or engine.* here (D-08).
Engine is injected as the `engine_runner` callable — no module-level import.
"""

from __future__ import annotations

import weakref
from collections.abc import Callable
from enum import Enum, auto
from typing import Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import DedupStore
from ach_agent.router.metrics import BACKPRESSURE_REJECTS, DEDUP_DISCARDS, EXPIRE_DROPS
from ach_agent.router.slots import SlotManager

log = structlog.get_logger(__name__)

# Default maxInvocationSeconds when not provided (conservative upper bound)
_DEFAULT_MAX_INVOCATION_SECONDS: float = 300.0

# Short window for the GitLab logical content composite (secondary dedup key). Deliberately
# SHORT (legacy used 2s): a content-based key on the long idempotency window would wrongly
# dedup two INTENTIONAL identical comments minutes apart. Exact-delivery retries are covered
# by the primary UUID key on the full idempotency_window_seconds.
_SECONDARY_DEDUP_WINDOW_S: int = 2


class RouterAdmitResult(Enum):
    """Outcome of Router.handle() — used by channels to decide response action.

    ACCEPTED:    Event enqueued for processing.
    DUPLICATE:   Event already seen within idempotency window; discarded.
    FULL_QUEUE:  maxQueuedTotal exceeded; caller maps to 503 / drop+log.
    """

    ACCEPTED = auto()
    DUPLICATE = auto()
    FULL_QUEUE = auto()


class Router:
    """Router — enforces normative dedup → backpressure → lane admission order.

    Core invariants (CONTRACT §6):
      RTR-01: dedup precedes backpressure (ORDER IS NORMATIVE, §6.2)
      RTR-02: per-session FIFO serialization (one Lane per session_key)
      RTR-03: maxConcurrentInvocations enforced via asyncio.Semaphore
      RTR-04: maxQueuedTotal enforced via a plain int counter
      RTR-05: full queue is NEVER silent (FULL_QUEUE / drop+log+metric)

    Constructor parameters match the conftest.py router fixture (01-02).
    """

    def __init__(
        self,
        max_concurrent_invocations: int,
        max_queued_total: int,
        idempotency_window_seconds: int,
        dedup_store: DedupStore,
        engine_runner: Callable[..., Any],
        delivery_adapter: Any,
        max_invocation_seconds: float = _DEFAULT_MAX_INVOCATION_SECONDS,
        channel_concurrency: dict[str, int] | None = None,
    ) -> None:
        self._max_queued_total = max_queued_total
        self._idempotency_window_seconds = idempotency_window_seconds
        self._dedup = dedup_store
        self._engine_runner = engine_runner
        self._delivery_adapter = delivery_adapter
        self._max_invocation_seconds = max_invocation_seconds

        # Slot manager: global semaphore (maxConcurrentInvocations) + per-channel slot
        self._slot_manager = SlotManager(
            max_concurrent_invocations=max_concurrent_invocations,
            channel_concurrency=channel_concurrency,
        )

        # queued_total: count of events waiting in lane queues OR being processed
        # Plain int is safe in single-threaded asyncio (no locks needed): inc/dec
        # only run on the event loop thread (router.handle + lane consumers).
        self._queued_total: int = 0

        # Lane map: session_key → Lane (bounded by eviction, Pitfall 6)
        self._lanes: dict[str, Any] = {}  # dict[str, Lane] — deferred import avoided

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        """Admit or reject an event. ORDER IS NORMATIVE (CONTRACT §6.2).

        Step 1 DEDUP — cheapest check; must NOT consume a queue slot (RTR-01).
        Step 2 BACKPRESSURE — only reached for distinct events (RTR-04).
        Step 3 MARK + INCREMENT — dedup mark before enqueue (mark-before-enqueue).
        Step 4 LANE — enqueue into per-session FIFO lane (RTR-02).
        """
        # ORDER IS NORMATIVE — dedup MUST precede backpressure (CONTRACT §6.2)
        dedup_key = f"{event.channel_name}:{event.idempotency_key}"
        sec = event.secondary_idempotency_key
        sec_key = f"{event.channel_name}:sec:{sec}" if sec else None

        # 1. DEDUP — primary OR secondary; neither may consume a queue slot (RTR-01)
        if self._dedup.seen(dedup_key) or (sec_key is not None and self._dedup.seen(sec_key)):
            DEDUP_DISCARDS.inc()
            log.debug("dedup: duplicate discarded", key=dedup_key, secondary=sec_key)
            return RouterAdmitResult.DUPLICATE

        # 2. BACKPRESSURE — only reached for distinct events
        if self._queued_total >= self._max_queued_total:
            BACKPRESSURE_REJECTS.inc()
            # RTR-05: full queue is NEVER silent (CONTRACT §6.4, Pitfall 3)
            dropped = event.source_trait == "async_no_retry"
            if dropped:
                EXPIRE_DROPS.inc()
            log.warning(
                "router: drop — queue full" if dropped else "router: backpressure — queue full",
                source_trait=event.source_trait,
                session_key=event.session_key,
                idempotency_key=event.idempotency_key,
                queued=self._queued_total,
                max_queued_total=self._max_queued_total,
            )
            return RouterAdmitResult.FULL_QUEUE

        # 3. MARK dedup BEFORE enqueuing (mark-before-enqueue invariant). Secondary on the
        #    SHORT window — see _SECONDARY_DEDUP_WINDOW_S.
        self._dedup.mark(dedup_key, self._idempotency_window_seconds)
        if sec_key is not None:
            self._dedup.mark(sec_key, _SECONDARY_DEDUP_WINDOW_S)
        self._queued_total += 1

        # 4. LANE — enqueue into per-session FIFO
        lane = self._get_or_create_lane(event.session_key, event.channel_name)
        await lane.put(event)
        return RouterAdmitResult.ACCEPTED

    def _get_or_create_lane(self, session_key: str, channel_name: str) -> Any:
        """Get the existing lane for session_key or create a new one.

        Deferred import of Lane to avoid circular imports (lane.py imports
        Router via TYPE_CHECKING only).
        """
        from ach_agent.router.lane import Lane

        if session_key not in self._lanes:
            self._lanes[session_key] = Lane(
                session_key=session_key,
                router_ref=weakref.ref(self),
                global_sem=self._slot_manager.global_sem,
                channel_sem=self._slot_manager.channel_sem(channel_name),
                engine_runner=self._engine_runner,
                max_invocation_seconds=self._max_invocation_seconds,
            )
        return self._lanes[session_key]

    def _maybe_evict_lane(self, session_key: str) -> None:
        """Remove an empty lane from the lane map and cancel its consumer task.

        Called from Lane._consume() after task_done() when the queue is empty.
        This prevents unbounded accumulation of Lane objects and asyncio.Queue
        instances over long-lived deployments (Pitfall 6, T-01-LANELEAK).
        """
        lane = self._lanes.get(session_key)
        if lane is not None and lane._queue.empty():
            del self._lanes[session_key]
            # Cancel the consumer task so it does not leak (Pitfall 6)
            lane.cancel()

    def _queued_total_dec(self) -> None:
        """Decrement queued_total counter.

        Called from Lane._queued_total_dec() via on_kill (idempotent). This is
        the canonical decrement point — the lane finally calls on_kill on every
        outcome, so queued_total is released exactly once per invocation whether
        the engine completed normally, timed out, or errored (Pitfall 4 / RTR-04).
        """
        self._queued_total -= 1

    @property
    def lanes(self) -> dict[str, Any]:
        """Read-only view of the lane map (for test introspection)."""
        return self._lanes
