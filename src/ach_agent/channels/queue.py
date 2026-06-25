# SPDX-License-Identifier: Apache-2.0
"""Queue channel adapter — redis Streams consumer (CHN, ackMode:onComplete).

Locked decisions:
  - Consume model: redis Streams with a consumer group (XREADGROUP + XACK).
    The consumer group gives us at-least-once delivery + message-id idempotency:
    each message id is unique and stable across redeliveries, so it is the
    natural idempotency_key (CONTRACT §6.1) and feeds the router's dedup directly.
  - ackMode:onComplete — XACK is called ONLY AFTER handler.handle() returns
    (ACCEPTED or DUPLICATE = processed). If handle() raises, the message is NOT
    acked and stays pending for redelivery. On FULL_QUEUE (async_no_retry source
    trait) we ack+drop, mirroring cron's drop-on-full semantics (RTR-05) — the
    message is consumed, never redelivered, and the drop is logged loudly.

REDIS URL DEVIATION (as-built): CONTRACT_v3 §2 `queue` block carries only
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


def _decode(value: Any) -> Any:
    """Best-effort decode of redis bytes → str (ids and field keys/values)."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


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
        pool: Any = None,  # EnginePool — accepted for symmetry with CronScheduler; unused
        redis_client: Any = None,
    ) -> None:
        self._cfg = channel_cfg
        self._handler = handler
        self._pool = pool
        self._client = redis_client
        self._owns_client = redis_client is None
        self._task: asyncio.Task[None] | None = None
        self._stopping = False

        # queue block is guaranteed present for type='queue' by schema coherence (D-04).
        assert channel_cfg.queue is not None
        self._stream: str = channel_cfg.queue.key
        self._group: str = f"ach-{channel_cfg.name}"

    async def start(self) -> None:
        """Connect (if needed), ensure the consumer group exists, start the loop."""
        if self._client is None:
            import redis.asyncio as redis_asyncio

            url = os.environ.get(_REDIS_URL_ENV, _DEFAULT_REDIS_URL)
            # redis.asyncio.from_url has no return annotation upstream, so --strict
            # flags it as an untyped call. Route it through a typed factory alias.
            from_url = cast(Callable[[str], Any], redis_asyncio.from_url)
            self._client = from_url(url)

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
                # Never let a single iteration error kill the loop; log + continue.
                # Unacked messages stay pending and will be redelivered.
                log.warning(
                    "queue: consume iteration error — continuing",
                    channel=self._cfg.name,
                    error=str(exc),
                )

    async def _consume_once(self) -> None:
        """One XREADGROUP → dispatch → XACK cycle for the channel's stream.

        Reads up to _READ_COUNT new messages (">"), dispatches each through the
        handler, and acks per onComplete semantics. Exposed for deterministic
        testing — _run() loops over it.
        """
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

    async def _handle_message(self, message_id: Any, fields: Any) -> None:
        """Dispatch one stream message; ack per onComplete semantics.

        On handler raise: do NOT ack (stays pending for redelivery). The raise
        is caught here so one bad message never kills the loop.
        """
        msg_id_str = str(_decode(message_id))
        payload: dict[str, Any] = {str(_decode(k)): _decode(v) for k, v in (fields or {}).items()}

        event = MessageEvent(
            idempotency_key=msg_id_str,  # CONTRACT §6.1: the redis message id, never empty
            session_key=self._cfg.name,
            channel_name=self._cfg.name,
            payload=payload,
            delivery_context={},
            source_trait="async_no_retry",
        )

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
        await self._client.xack(self._stream, self._group, message_id)
