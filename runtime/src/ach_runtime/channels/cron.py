# SPDX-License-Identifier: MIT
"""Cron channel adapter — one croniter/asyncio scheduler for configured cron channels."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from croniter import croniter

from ach_runtime.channels.event import Event
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.channels.routing import route
from ach_runtime.config.schema import ChannelConfig

log = structlog.get_logger(__name__)


class CronScheduler:
    """Single scheduler multiplexing all cron channels, preserving scheduled ticks."""

    def __init__(self, channels: list[ChannelConfig], control: Any) -> None:
        self._slots: list[tuple[ChannelConfig, croniter, datetime | None]] = [
            (
                channel,
                croniter(channel.cron.schedule, datetime.now(ZoneInfo(channel.cron.timezone))),
                None,
            )
            for channel in channels
            if channel.cron is not None
        ]
        self._control = control
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Create the single asyncio task that drives all cron channels."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel and await the scheduler task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        """Compute future ticks, sleep to the earliest, and advance due channels only."""
        self._slots = [(channel, cron, cron.get_next(datetime)) for channel, cron, _ in self._slots]

        while True:
            earliest = min(
                (next_tick for _, _, next_tick in self._slots if next_tick is not None),
                default=None,
            )
            if earliest is None:
                return

            seconds = (earliest - datetime.now(UTC)).total_seconds()
            if seconds > 0:
                await asyncio.sleep(seconds)

            updated: list[tuple[ChannelConfig, croniter, datetime | None]] = []
            for channel, cron, next_tick in self._slots:
                if next_tick is not None and next_tick <= earliest:
                    await self._fire(channel, next_tick)
                    updated.append((channel, cron, cron.get_next(datetime)))
                else:
                    updated.append((channel, cron, next_tick))
            self._slots = updated

    async def _fire(self, channel: ChannelConfig, scheduled_tick: datetime) -> None:
        """Admit one scheduled tick; log Started or drop a failed tick without retry."""
        utc_tick = scheduled_tick.astimezone(UTC).isoformat()
        event = Event(
            event_id=f"{channel.name}:{utc_tick}",
            channel=channel.name,
            type="cron",
            source="cron",
            payload={"scheduled_tick": utc_tick},
            context={"cron": {"scheduledTick": utc_tick}},
        )
        CHANNEL_INBOUND.labels(channel=channel.name, type="cron").inc()
        try:
            ticket = self._control.admission.admit(route(event, channel))
            await asyncio.shield(ticket.started)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "cron: tick dropped before Started",
                channel=channel.name,
                tick=utc_tick,
                error=str(exc),
            )
            return
        log.info("cron: tick started", channel=channel.name, tick=utc_tick)
