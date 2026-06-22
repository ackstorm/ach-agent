# SPDX-License-Identifier: Apache-2.0
"""SlotManager — global and per-channel concurrency semaphores + on_kill (queued_total).

CONTRACT §6.3: maxConcurrentInvocations and maxQueuedTotal are ALWAYS enforced.

Pitfall 4: Both concurrency semaphores are acquired AND released via `async with` in
the lane consumer (RAII) — never bare acquire/release. on_kill MUST NOT release the
semaphores, or the slot would be released twice (once by on_kill, once by the
`async with` exit), inflating the semaphore and eroding the concurrency cap. on_kill
owns ONLY the router's queued_total counter, and is idempotent so it can be called
safely from both the engine watchdog (on a kill) and the lane finally (on every
outcome) without double-counting.

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06, D-08).
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable


class SlotManager:
    """Manages a global asyncio.Semaphore (maxConcurrentInvocations) and
    per-channel asyncio.Semaphore (channel concurrency cap, default 1 for cron).

    The SlotManager is passed into the Lane so the lane consumer can acquire
    the global slot before dispatching an invocation.
    """

    def __init__(
        self,
        max_concurrent_invocations: int,
        channel_concurrency: int = 1,
    ) -> None:
        self.global_sem: asyncio.Semaphore = asyncio.Semaphore(max_concurrent_invocations)
        self.channel_slot: asyncio.Semaphore = asyncio.Semaphore(channel_concurrency)


def make_on_kill(
    queued_total_dec_fn: Callable[[], None],
) -> Callable[[], None]:
    """Build the on_kill callback injected into engine.run_invocation.

    Called by:
      (1) the engine watchdog on a maxInvocationSeconds kill (lifecycle.run_invocation), and
      (2) the lane consumer's `finally` after every invocation outcome.

    Responsibility: decrement the router's queued_total counter — nothing else. The
    concurrency semaphores (global + per-channel) are owned by the lane's `async with`
    blocks and released there exactly once on every path; on_kill MUST NOT release them
    or the slot would be double-released and the concurrency cap would erode (RTR-03).

    Idempotent: fires its decrement at most once per invocation, so it is safe to call
    from BOTH the watchdog (which may kill mid-invocation) and the lane finally (which
    fires on every outcome) without double-counting queued_total (RTR-04).

    RESEARCH.md Code Examples — on_kill seam (Phase 0 seam → Phase 1 real).
    """
    fired = False

    def on_kill() -> None:
        nonlocal fired
        if fired:
            return
        fired = True
        queued_total_dec_fn()

    return on_kill
