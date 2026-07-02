# SPDX-License-Identifier: Apache-2.0
"""FIFO Lane — one asyncio.Queue + one consumer task per session key.

RTR-02: Events with the same session_key are processed strictly in FIFO order,
with at most one invocation in-flight per session key at any time.

RTR-03: The global asyncio.Semaphore (maxConcurrentInvocations) is acquired via
`async with` before dispatching, enforcing the concurrency cap across all lanes.

Pitfall 4: All semaphore acquisition is via `async with` (never bare acquire/release).
Pitfall 6: Empty lanes are evicted via `router._maybe_evict_lane(session_key)` after
each task_done() to prevent unbounded Queue/task leak over long-lived deployments.

Constraint: NEVER import from hermes_agent.* here (RTR-06, D-08).
"""

from __future__ import annotations

import asyncio
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.metrics import ENGINE_WATCHDOG_KILLS
from ach_agent.router.slots import make_on_kill

if TYPE_CHECKING:
    from ach_agent.router.router import Router

log = structlog.get_logger(__name__)

# engine_runner callable type: async (event, on_kill) -> Any
EngineRunner = Callable[[MessageEvent, Callable[[], None]], Any]


class Lane:
    """Per-session-key FIFO lane.

    Owns a single asyncio.Queue[MessageEvent] and a single consumer task.
    The consumer serializes all events for this session key, ensuring at most
    one invocation is in-flight at a time (RTR-02).

    Lifecycle:
      - Created by Router._get_or_create_lane() on first event for a session key.
      - Evicted by Router._maybe_evict_lane() when the queue drains (Pitfall 6).
    """

    def __init__(
        self,
        session_key: str,
        router_ref: weakref.ref[Router],
        global_sem: asyncio.Semaphore,
        channel_sem: asyncio.Semaphore,
        engine_runner: EngineRunner,
        max_invocation_seconds: float,
    ) -> None:
        self._session_key = session_key
        self._router_ref = router_ref
        self._global_sem = global_sem
        self._channel_sem = channel_sem
        self._engine_runner = engine_runner
        self._max_invocation_seconds = max_invocation_seconds
        self._queue: asyncio.Queue[MessageEvent] = asyncio.Queue()
        self._task = asyncio.create_task(self._consume())

    async def put(self, event: MessageEvent) -> None:
        """Enqueue an event into this lane's FIFO queue."""
        await self._queue.put(event)

    def is_done(self) -> bool:
        """True if the consumer task has finished (for eviction check)."""
        return self._task.done()

    async def _consume(self) -> None:
        """Consumer loop: drain the queue one event at a time (FIFO, RTR-02).

        For each event:
          1. Acquire global_sem + channel_sem via `async with` (Pitfall 4 / RTR-03).
             The `async with` blocks are the SOLE owner of the semaphores — they
             release exactly once on every path (success, timeout, error, cancel).
          2. Dispatch through engine_runner with asyncio.timeout (maxInvocationSeconds).
          3. on_kill (idempotent, queued_total only) fires once in `finally`, so the
             lane is the authoritative release point and does NOT depend on the engine
             calling on_kill on the happy path (RTR-04 — prevents queued_total leak).
          4. task_done() + evict if empty (Pitfall 6).
        """
        while True:
            try:
                event = await self._queue.get()
            except asyncio.CancelledError:
                return

            # on_kill decrements queued_total only — semaphores are released by the
            # `async with` blocks below. Idempotent, so the engine watchdog may also
            # call it on a timeout kill without double-counting.
            on_kill = make_on_kill(queued_total_dec_fn=self._queued_total_dec)
            try:
                async with self._global_sem:
                    async with self._channel_sem:
                        try:
                            async with asyncio.timeout(self._max_invocation_seconds):
                                await self._engine_runner(event, on_kill)
                        except TimeoutError:
                            # RTR-04: the lane is the single maxInvocationSeconds owner, so
                            # the watchdog-kill metric is incremented HERE (not in
                            # run_invocation). This fires ONLY on a real deadline; a
                            # shutdown-cancel hits `except CancelledError` below, not this
                            # branch, so it is never over-counted.
                            ENGINE_WATCHDOG_KILLS.inc()
                            log.warning(
                                "lane: invocation exceeded maxInvocationSeconds",
                                session_key=self._session_key,
                                idempotency_key=event.idempotency_key,
                                max_invocation_seconds=self._max_invocation_seconds,
                            )
                        except Exception:
                            log.exception(
                                "lane: invocation failed",
                                session_key=self._session_key,
                                idempotency_key=event.idempotency_key,
                            )
            except asyncio.CancelledError:
                return
            finally:
                # Single, idempotent queued_total release for this event — fires on
                # every outcome, including when the engine never called on_kill.
                on_kill()
                self._queue.task_done()
                # Pitfall 6: evict empty lane to prevent Queue/task memory leak
                if self._queue.empty():
                    router = self._router_ref()
                    if router is not None:
                        router._maybe_evict_lane(self._session_key)

    def _queued_total_dec(self) -> None:
        """Decrement the router's queued_total counter.

        Called from on_kill (the queued_total release point). The router_ref is
        used to reach the counter without a strong reference cycle.
        """
        router = self._router_ref()
        if router is not None:
            router._queued_total_dec()

    def cancel(self) -> None:
        """Cancel the consumer task (called during lane eviction)."""
        self._task.cancel()

    async def join(self) -> None:
        """Block until all queued + in-flight events have been processed (D-11 drain).

        Delegates to the internal queue's join(): returns once every put() has a
        matching task_done() from the consumer loop. Used by the graceful drain
        sequence to preserve in-flight work before cancelling idle lanes.
        """
        await self._queue.join()

    async def wait_closed(self) -> None:
        """Await the consumer task after cancel() so drain can confirm teardown.

        Swallows the expected CancelledError raised by a cancelled idle consumer.
        """
        try:
            await self._task
        except asyncio.CancelledError:
            pass
