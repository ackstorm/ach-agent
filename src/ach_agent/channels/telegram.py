# SPDX-License-Identifier: Apache-2.0
"""Telegram channel adapter — Hermes TelegramAdapter shim (CHN-04, D-03/D-05/D-06/D-07).

Locked decisions:
  - Hermes TelegramAdapter instantiated + wrapped; do NOT reimplement PTB polling (D-04).
  - set_message_handler() callback translates Hermes MessageEvent → ach_agent.MessageEvent (D-07).
  - session_key = chat_id + message_thread_id; fallback = chat_id (D-03).
  - idempotency_key: derive_telegram_idempotency_key (already in dedup.py — reuse).
  - source_trait = "async_no_retry"; FULL_QUEUE → drop+log+metric (D-05, RTR-05).
  - A′ gate: drop + COLD_START_DROPS before first engine warmup (D-06).

RTR-06: Hermes imports ONLY inside this file — never in seam.py, router.*, engine.*.
Boot-order: imported after configure_logging().
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import derive_telegram_idempotency_key
from ach_agent.router.metrics import CHANNEL_INBOUND, COLD_START_DROPS
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)


def _make_telegram_shim(
    handler: MessageHandler,
    pool: Any,
    channel_cfg: ChannelConfig,
) -> Any:
    """Build the set_message_handler callback that translates Hermes PTB → ach_agent.

    RTR-06: this closure captures only ach_agent types — no hermes_agent import here.
    """

    async def shim(hermes_event: Any) -> None:  # hermes_event: Hermes MessageEvent
        # A′ gate (D-06) — same pattern as cron.py lines 65–73
        if pool is not None and not pool.engine_has_been_ready_once:
            log.warning(
                "telegram: event dropped — engine not ready (A′ cold-start gate)",
                channel=channel_cfg.name,
            )
            COLD_START_DROPS.labels(channel=channel_cfg.name).inc()
            return None

        # Session key = chat_id + message_thread_id (D-03); fallback = chat_id
        chat_id: str = getattr(hermes_event.source, "chat_id", "") or ""
        thread_id: int | None = getattr(hermes_event.source, "thread_id", None)
        session_key = f"{chat_id}:{thread_id}" if thread_id is not None else chat_id

        # Idempotency key — reuse derive_telegram_idempotency_key (dedup.py)
        idempotency_key = derive_telegram_idempotency_key(
            {"update_id": getattr(hermes_event, "platform_update_id", None)}
        )

        CHANNEL_INBOUND.labels(channel=channel_cfg.name, type="telegram").inc()

        event = MessageEvent(
            idempotency_key=idempotency_key,
            session_key=session_key,
            channel_name=channel_cfg.name,
            payload={
                "text": getattr(hermes_event, "text", ""),
                "chat_id": chat_id,
                "thread_id": thread_id,
            },
            source_trait="async_no_retry",  # D-05: FULL_QUEUE → drop+log (RTR-05)
        )

        result = await handler.handle(event)
        if result == RouterAdmitResult.FULL_QUEUE:
            log.warning(
                "telegram: event dropped — queue full",
                channel=channel_cfg.name,
            )
        elif result == RouterAdmitResult.DUPLICATE:
            log.warning("telegram: event deduplicated", channel=channel_cfg.name)
        return None

    return shim


async def connect_telegram_adapter(
    channel_cfg: ChannelConfig,
    handler: MessageHandler,
    pool: Any = None,
) -> Any:
    """Instantiate + connect Hermes TelegramAdapter; return adapter for drain (D-06).

    RTR-06: Hermes import is INSIDE this function.
    connect() is non-blocking — starts asyncio.Task for PTB polling internally.
    Reads TELEGRAM_BOT_TOKEN from env.
    """
    from gateway.config import PlatformConfig  # RTR-06 fence (via hermes package)
    from gateway.platforms.telegram import TelegramAdapter  # RTR-06 fence

    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    platform_cfg = PlatformConfig(enabled=True, token=bot_token)
    adapter = TelegramAdapter(config=platform_cfg)
    adapter.set_message_handler(_make_telegram_shim(handler, pool, channel_cfg))
    ok = await adapter.connect()
    if not ok:
        log.error("telegram: connect failed", channel=channel_cfg.name)
        raise RuntimeError(f"TelegramAdapter.connect() failed for {channel_cfg.name}")
    log.info("telegram channel connected", channel_name=channel_cfg.name)
    return adapter


async def disconnect_telegram_adapter(adapter: Any) -> None:
    """Graceful drain: stop PTB polling (D-06)."""
    await adapter.disconnect()
