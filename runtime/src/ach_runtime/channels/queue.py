# SPDX-License-Identifier: MIT
"""Redis Streams channel adapter with stable c1 pending-entry recovery."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import Any, cast

import structlog

from ach_runtime.channels.event import Event
from ach_runtime.channels.metrics import CHANNEL_INBOUND
from ach_runtime.channels.routing import route
from ach_runtime.config.schema import ChannelConfig

log = structlog.get_logger(__name__)

_READ_COUNT = 10
_BLOCK_MS = 5000
_REDIS_URL_ENV = "REDIS_URL"
_DEFAULT_REDIS_URL = "redis://localhost:6379"
_CONSUMER_NAME = "c1"


class QueueConsumer:
    """Redis Streams consumer for one queue channel; acknowledge only after Started."""

    def __init__(self, channel_cfg: ChannelConfig, control: Any, redis_client: Any = None) -> None:
        self._cfg = channel_cfg
        self._control = control
        self._client = redis_client
        self._owns_client = redis_client is None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        assert channel_cfg.queue is not None
        self._stream = channel_cfg.queue.key
        self._group = f"ach-{channel_cfg.name}"
        self._pending_cursor = "0"
        self._next_pending_sweep = 0.0

    async def start(self) -> None:
        """Connect, ensure the consumer group exists, and start the consume loop."""
        if self._client is None:
            import redis.asyncio as redis_asyncio

            url = os.environ.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL)
            from_url = cast(Callable[..., Any], redis_asyncio.from_url)
            self._client = from_url(url, decode_responses=True)
        await self._ensure_group()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel and await the consume loop; close only a client created here."""
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning("queue: redis client close error", channel=self._cfg.name, error=str(exc))

    async def _ensure_group(self) -> None:
        try:
            await self._client.xgroup_create(
                name=self._stream, groupname=self._group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001
            if "BUSYGROUP" in str(exc):
                return
            log.warning(
                "queue: xgroup_create error",
                channel=self._cfg.name,
                stream=self._stream,
                group=self._group,
                error=str(exc),
            )

    async def _run(self) -> None:
        while not self._stopping:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "queue: consume iteration error — continuing",
                    channel=self._cfg.name,
                    error=str(exc),
                )
                await asyncio.sleep(_BLOCK_MS / 1000)

    async def _consume_once(self) -> None:
        await self._consume_pending_once()
        response = await self._client.xreadgroup(
            groupname=self._group,
            consumername=_CONSUMER_NAME,
            streams={self._stream: ">"},
            count=_READ_COUNT,
            block=_BLOCK_MS,
        )
        if not response:
            return
        for _stream_key, entries in response:
            for message_id, fields in entries:
                await self._handle_message(message_id, fields)

    async def _consume_pending_once(self) -> None:
        loop = asyncio.get_running_loop()
        if loop.time() < self._next_pending_sweep:
            return
        response = await self._client.xreadgroup(
            groupname=self._group,
            consumername=_CONSUMER_NAME,
            streams={self._stream: self._pending_cursor},
            count=_READ_COUNT,
        )
        if not response:
            self._pending_cursor = "0"
            self._next_pending_sweep = loop.time() + _BLOCK_MS / 1000
            return
        for _stream_key, entries in response:
            for message_id, fields in entries:
                self._pending_cursor = str(message_id)
                if not fields:
                    await self._ack_stale_pending(message_id)
                    continue
                await self._handle_message(message_id, fields)

    async def _ack_stale_pending(self, message_id: Any) -> None:
        try:
            await self._client.xack(self._stream, self._group, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "queue: xack failed for a deleted pending entry — retried on next sweep",
                channel=self._cfg.name,
                message_id=str(message_id),
                error=str(exc),
            )

    async def _handle_message(self, message_id: Any, fields: Any) -> None:
        event_id = str(message_id)
        event = Event(
            event_id=event_id,
            channel=self._cfg.name,
            type="queue",
            source="queue",
            payload=dict(fields or {}),
        )
        CHANNEL_INBOUND.labels(channel=self._cfg.name, type="queue").inc()
        try:
            ticket = self._control.admission.admit(route(event, self._cfg))
            await asyncio.shield(ticket.started)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "queue: delivery left pending before Started",
                channel=self._cfg.name,
                message_id=event_id,
                error=str(exc),
            )
            return
        try:
            await self._client.xack(self._stream, self._group, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "queue: xack failed — message left pending for redelivery",
                channel=self._cfg.name,
                message_id=event_id,
                error=str(exc),
            )
