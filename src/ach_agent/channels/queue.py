# SPDX-License-Identifier: Apache-2.0
"""Queue channel adapter — redis Streams consumer (CHN, ackMode:onComplete).

Locked decisions:
  - Consume model: redis Streams with a consumer group (XREADGROUP + XACK).
    The consumer group gives us at-least-once delivery + message-id idempotency:
    each message id is unique and stable across redeliveries, so it is the
    natural idempotency_key (CONTRACT §6.1) and feeds the router's dedup directly.
  - Recovery (finding 6): this harness runs one replica, always as the same
    stable consumer identity ("c1") — never renamed, never claiming another
    consumer's entries (no XCLAIM/XAUTOCLAIM). A crash/restart between
    XREADGROUP and XACK leaves a message in "c1"'s own pending-entries list
    (PEL); _consume_pending_once() re-reads that PEL (an explicit id cursor,
    not ">") so it is revisited on the next start, interleaved one batch at a
    time with new-message reads so neither can starve the other.
  - ackMode:onComplete — XACK is called ONLY AFTER handler.handle() returns
    (ACCEPTED or DUPLICATE = processed). If handle() raises, the message is NOT
    acked and stays pending for redelivery. On FULL_QUEUE (async_no_retry source
    trait) we ack+drop, mirroring cron's drop-on-full semantics (RTR-05) — the
    message is consumed, never redelivered, and the drop is logged loudly.

REDIS URL DEVIATION (as-built): operator contract §2 `queue` block carries only
`key`/`ackMode` — it does NOT carry a connection URL. The redis connection is
therefore read from the env var `REDIS_URL` (default "redis://localhost:6379").
This is a deliberate as-built deviation from the contract: the contract is the
source of truth for the *config schema*, and since it omits a URL the harness
falls back to the conventional env var. Tests inject a fake client so `start()`
never requires a live redis.

RTR-06: NEVER import from hermes_agent.* or engine.* here.

Boot-order: imported after configure_logging() (Pitfall 8). main.py constructs
one QueueConsumer per queue channel and owns its start()/stop() lifecycle.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.metrics import CHANNEL_INBOUND
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)

# Read budget per XREADGROUP and block timeout (ms). BLOCK lets the loop park on
# an empty stream instead of busy-spinning; it returns empty on timeout so the
# loop can re-check the cancellation state and re-issue the read.
_READ_COUNT = 10
_BLOCK_MS = 5000

_REDIS_URL_ENV = "REDIS_URL"
_DEFAULT_REDIS_URL = "redis://localhost:6379"
_CONSUMER_NAME = "c1"


class QueueConsumer:
    """Redis Streams consumer for one queue channel (ackMode:onComplete).

    Mirrors CronScheduler's start/stop + handler-dispatch lifecycle: start()
    creates a single asyncio consume task; stop() cancels + awaits it and closes
    the redis client iff this consumer created it.
    """

    def __init__(
        self,
        channel_cfg: ChannelConfig,
        handler: MessageHandler,
        redis_client: Any = None,
    ) -> None:
        self._cfg = channel_cfg
        self._handler = handler
        self._client = redis_client
        self._owns_client = redis_client is None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        # queue block is guaranteed present for type='queue' by schema coherence (D-04).
        assert channel_cfg.queue is not None
        self._stream: str = channel_cfg.queue.key
        self._group: str = f"ach-{channel_cfg.name}"

        # finding 6: recovery cursor over THIS consumer's ("c1", the one stable
        # identity this harness ever uses) own pending-entries list (PEL) — entries
        # already delivered to us but never acked (a crash/restart before the
        # ack). "0" means "from the start of my PEL". Reset to "0" once a sweep
        # catches up to empty, so a later-stuck entry is still found by the next
        # sweep rather than left behind a cursor that only ever advances.
        self._pending_cursor: str = "0"
        # Event-loop-monotonic deadline for the next full pending sweep once one
        # has caught up to empty — bounds poison-message retries even under
        # constant new traffic (a fresh "0" sweep every iteration would otherwise
        # busy-loop reading an empty PEL).
        self._next_pending_sweep: float = 0.0

    async def start(self) -> None:
        """Connect (if needed), ensure the consumer group exists, start the loop."""
        if self._client is None:
            import redis.asyncio as redis_asyncio

            url = os.environ.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL)
            # redis.asyncio.from_url has no return annotation upstream, so --strict
            # flags it as an untyped call. Route it through a typed factory alias.
            from_url = cast(Callable[..., Any], redis_asyncio.from_url)
            self._client = from_url(url, decode_responses=True)

        await self._ensure_group()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel + await the consume task; close the client iff we created it."""
        self._stopping = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._owns_client and self._client is not None:
            try:
                await self._client.aclose()
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "queue: redis client close error",
                    channel=self._cfg.name,
                    error=str(exc),
                )

    async def _ensure_group(self) -> None:
        """Create the consumer group with MKSTREAM; ignore BUSYGROUP (already exists)."""
        try:
            await self._client.xgroup_create(
                name=self._stream, groupname=self._group, id="0", mkstream=True
            )
        except Exception as exc:  # noqa: BLE001
            # BUSYGROUP — group already exists — is the only benign case.
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
        """Loop calling _consume_once() until cancelled (clean CancelledError exit)."""
        while not self._stopping:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                # Never let a single iteration error kill the loop; log + continue,
                # but bounded — a redis outage must not spin this into a hot retry
                # loop. Unacked messages stay pending and will be redelivered.
                log.warning(
                    "queue: consume iteration error — continuing",
                    channel=self._cfg.name,
                    error=str(exc),
                )
                await asyncio.sleep(_BLOCK_MS / 1000)

    async def _consume_once(self) -> None:
        """One recovery pass + one new-message pass (finding 6).

        Single-consumer recovery: this harness always reads/acks as the one
        stable identity "c1" (no consumer renaming, no XCLAIM/XAUTOCLAIM of
        other consumers' entries — see module docstring). A crash or restart
        between XREADGROUP and XACK leaves a message in "c1"'s own
        pending-entries list (PEL); the OLD code only ever read new (">")
        messages, so a pending entry was never revisited. Each call here does
        AT MOST one pending-recovery batch, then one new-message batch, so a
        large backlog of either kind can't starve the other. Exposed for
        deterministic testing — _run() loops over it.
        """
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
        """Read up to _READ_COUNT of "c1"'s own already-delivered-but-unacked
        entries, starting at the current pending cursor. Never BLOCKs — a
        pending (non-">") XREADGROUP read never blocks in redis regardless.

        Advances the cursor past every returned id, including ones whose
        stream entry no longer exists (deleted/trimmed while still pending —
        redis returns a null/empty payload for those): there is nothing to
        process, so they are acked directly without invoking the engine,
        clearing the stale pending id rather than looping on it forever.

        On reaching the end of the current PEL (an empty read), the cursor
        resets to "0" and the next sweep is deferred by _BLOCK_MS/1000 — this
        bounds a poison entry's retry rate to the same cadence as new-message
        polling instead of a fresh "0" sweep re-scanning an empty PEL on
        every single iteration.
        """
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
        """Ack a pending id whose stream entry was deleted/trimmed — nothing to
        process, so this clears it without dispatching to the handler."""
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
        """Dispatch one stream message; ack per onComplete semantics.

        On handler raise: do NOT ack (stays pending for redelivery). The raise
        is caught here so one bad message never kills the loop.
        """
        msg_id_str = str(message_id)
        payload: dict[str, Any] = dict(fields or {})

        event = MessageEvent(
            idempotency_key=msg_id_str,  # CONTRACT §6.1: the redis message id, never empty
            session_key=self._cfg.name,
            channel_name=self._cfg.name,
            payload=payload,
            delivery_context={},
            source_trait="async_no_retry",
        )

        CHANNEL_INBOUND.labels(channel=self._cfg.name, type="queue").inc()

        try:
            result = await self._handler.handle(event)
        except Exception as exc:  # noqa: BLE001
            # Do NOT ack — message stays pending for redelivery (onComplete).
            log.warning(
                "queue: handler raised — message left pending (not acked)",
                channel=self._cfg.name,
                message_id=msg_id_str,
                error=str(exc),
            )
            return

        if result == RouterAdmitResult.FULL_QUEUE:
            # async_no_retry parity with cron drop-on-full (RTR-05): ack+drop, never silent.
            log.warning(
                "queue: message dropped — queue full (ack+drop, async_no_retry)",
                channel=self._cfg.name,
                message_id=msg_id_str,
            )
        elif result == RouterAdmitResult.DUPLICATE:
            log.warning(
                "queue: message deduplicated (acking)",
                channel=self._cfg.name,
                message_id=msg_id_str,
            )

        # onComplete: ack ONLY after handle() returned (ACCEPTED/DUPLICATE = processed;
        # FULL_QUEUE = ack+drop). The raise path above already returned without acking.
        # A failed xack must not abort the consume batch: log and continue. The message
        # stays pending and is redelivered (at-least-once) — dedup absorbs the replay.
        try:
            await self._client.xack(self._stream, self._group, message_id)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "queue: xack failed — message left pending for redelivery",
                channel=self._cfg.name,
                message_id=msg_id_str,
                error=str(exc),
            )
