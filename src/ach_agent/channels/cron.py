# SPDX-License-Identifier: Apache-2.0
"""Cron channel adapter — CronScheduler singleton (CHN-02, D-08, D-09).

Locked decisions:
  - Scheduler: croniter + asyncio.sleep only (no third-party scheduler framework).
  - Idempotency key: derive_cron_idempotency_key(name, scheduled_tick) only
    (the scheduled instant, not datetime.now() — Pitfall 5 / D-09).
  - Single CronScheduler per process multiplexing ALL cron channels (D-08, SC#3).
  - _instance_count: class-level counter for D-09 singleton assertion test.

RTR-06: NEVER import from hermes_agent.* here.

Boot-order: this module is imported after configure_logging() (Pitfall 8).
The single-scheduler invariant (D-08/D-09) is enforced by main.py constructing
exactly ONE CronScheduler instance for all cron channels.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog
from croniter import croniter

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import derive_cron_idempotency_key
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)


class CronScheduler:
    """Single scheduler multiplexing all cron channels (D-08/D-09).

    Invariant: exactly one instance per process. main.py constructs exactly one.
    _instance_count: class-level counter for D-09 singleton assertion test.

    Preserved from run_cron_channel:
      - DUR-04 no-catch-up: croniter.get_next() always computes NEXT future tick.
        croniter.get_next() from now always returns a tick strictly in the future —
        ticks missed while the pod was down are dropped (spec §30.1). Deliberate.
      - Acceptance is decoupled from engine readiness: a tick always routes — the
        engine starts lazily per session_key inside the lane.
      - RTR-05 async_no_retry: FULL_QUEUE → drop+log warning, never silent.
      - D-09: idempotency_key = derive_cron_idempotency_key(name, scheduled next_dt),
              NOT datetime.now().

    PITFALL (RESEARCH.md Pitfall 7): Call croniter.get_next() only for due channels —
    do NOT advance non-due croniter objects (stateful; can skip ticks).
    """

    _instance_count: int = 0  # D-09: singleton test increments/decrements this

    def __init__(
        self,
        channels: list[ChannelConfig],
        handler: MessageHandler,
        pool: Any = None,  # EnginePool — unused now (decoupled); kept for backward compat
    ) -> None:
        CronScheduler._instance_count += 1
        # Build slots: (channel_cfg, croniter_obj, next_dt)
        # next_dt starts as None; computed lazily on first _run() iteration.
        # DUR-04: croniter initialized from now() — get_next() always returns future tick.
        self._slots: list[tuple[ChannelConfig, croniter, datetime | None]] = [
            (ch, croniter(ch.cron.schedule, datetime.now(UTC)), None) for ch in channels if ch.cron
        ]
        self._handler = handler
        self._pool = pool
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Create the single asyncio task that drives all cron channels (D-08)."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel + await the scheduler task; decrement _instance_count for test isolation."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        CronScheduler._instance_count -= 1

    async def _run(self) -> None:
        """Single loop: compute all next ticks, sleep to earliest, fire due channels.

        Pitfall 7: advance ONLY the due channels' croniter — never advance non-due
        iterators (croniter is stateful; advancing early skips ticks).
        """
        # Initialize next_dt for all slots upfront (DUR-04 — always future)
        self._slots = [(ch, cron, cron.get_next(datetime)) for ch, cron, _ in self._slots]

        while True:
            # Find the earliest next tick across all channels
            earliest: datetime | None = None
            for _, _, next_dt in self._slots:
                if next_dt is not None and (earliest is None or next_dt < earliest):
                    earliest = next_dt

            if earliest is None:
                # No cron channels configured — nothing to do
                return

            now = datetime.now(UTC)
            sleep_secs = (earliest - now).total_seconds()
            if sleep_secs > 0:
                await asyncio.sleep(sleep_secs)

            # Fire all channels due at (or before) the earliest tick we slept towards.
            # Using `earliest` rather than datetime.now() ensures correctness even when
            # asyncio.sleep returns early (tests) or the wall clock drifts slightly.
            new_slots: list[tuple[ChannelConfig, croniter, datetime | None]] = []
            for ch, cron, next_dt in self._slots:
                if next_dt is not None and next_dt <= earliest:
                    # This channel is due — fire it and advance its croniter (Pitfall 7)
                    await self._fire(ch, next_dt)
                    # Advance ONLY this channel's croniter to the next tick
                    advanced_next: datetime = cron.get_next(datetime)
                    new_slots.append((ch, cron, advanced_next))
                else:
                    # Not due yet — do NOT advance croniter (Pitfall 7)
                    new_slots.append((ch, cron, next_dt))
            self._slots = new_slots

    async def _fire(self, channel_cfg: ChannelConfig, scheduled_next_dt: datetime) -> None:
        """Fire one cron tick for the given channel.

        Preserves:
        - Decoupled acceptance: a tick always routes, regardless of engine readiness —
          the engine starts lazily per session_key inside the lane.
        - DUR-04 no-catch-up loss mode (comment preserved, behavior unchanged).
        - D-09: idempotency_key from scheduled tick, NOT now().
        - RTR-05: FULL_QUEUE → drop+log warning, never silent.
        """
        # D-09: use SCHEDULED next_dt, NOT now() (Pitfall 5)
        idempotency_key = derive_cron_idempotency_key(channel_cfg.name, scheduled_next_dt)
        session_key = channel_cfg.name  # D-08: per-channel, all ticks on one lane

        event = MessageEvent(
            idempotency_key=idempotency_key,
            session_key=session_key,
            channel_name=channel_cfg.name,
            payload={"scheduled_tick": scheduled_next_dt.strftime("%Y-%m-%dT%H:%M:%S")},
            source_trait="async_no_retry",  # cron → drop+log on full queue (RTR-05)
        )
        result = await self._handler.handle(event)
        if result == RouterAdmitResult.FULL_QUEUE:
            # RTR-05 cron path: FULL_QUEUE → drop + log warning; never silent
            log.warning(
                "cron: tick dropped — queue full",
                channel=channel_cfg.name,
                tick=scheduled_next_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            )
        elif result == RouterAdmitResult.DUPLICATE:
            log.warning(
                "cron: tick deduplicated",
                channel=channel_cfg.name,
                tick=scheduled_next_dt.strftime("%Y-%m-%dT%H:%M:%S"),
            )
