# SPDX-License-Identifier: Apache-2.0
"""TUI channel adapter — stdin/stdout free-form (no terminal contract).

A line-oriented console channel: each line read from stdin becomes a MessageEvent
with source_trait="sync" and a fresh reply_future. The router runs the engine on the
bounded lane; engine_runner resolves the future with the engine's free-form text. The
channel writes that text (+ newline) back to stdout — there is NO terminal contract, so
even an `action:none` reply is printed verbatim.

Mirrors CronScheduler/QueueConsumer lifecycle: start() creates a single asyncio
read-loop task; stop() cancels + awaits it.

Testability: __init__ accepts optional reader/writer. The default reader wraps
sys.stdin; the default writer writes to sys.stdout. start() loops over the reader
calling the exposed _handle_line() helper so tests can drive a single line directly.

RTR-06: NEVER import from hermes_agent.* or engine.* here.

Boot-order: imported after configure_logging() (Pitfall 8). main.py constructs one
TuiChannel per tui channel and owns its start()/stop() lifecycle (like cron).
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig

log = structlog.get_logger(__name__)


def _default_write(text: str) -> None:
    """Default writer: write text + newline to stdout and flush."""
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


class _StdinReader:
    """Async-iterable wrapper over sys.stdin: readline() in a thread executor.

    sys.stdin.readline() is blocking, so it is run in the default executor to avoid
    stalling the event loop. Returns "" on EOF (matches the loop's stop condition).
    """

    async def readline(self) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, sys.stdin.readline)


class TuiChannel:
    """Free-form stdin/stdout channel for one tui channel (no terminal contract)."""

    def __init__(
        self,
        channel_cfg: ChannelConfig,
        handler: MessageHandler,
        pool: Any = None,  # EnginePool — accepted for symmetry with cron/queue; unused
        reader: Any = None,
        writer: Callable[[str], None] | None = None,
    ) -> None:
        self._cfg = channel_cfg
        self._handler = handler
        self._reader = reader if reader is not None else _StdinReader()
        self._writer: Callable[[str], None] = writer if writer is not None else _default_write
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Create the single asyncio read-loop task."""
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel + await the read-loop task."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _run(self) -> None:
        """Read lines until EOF or cancellation, dispatching each via _handle_line."""
        try:
            while True:
                raw = await self._reader.readline()
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                if raw == "":
                    # EOF — stop the loop cleanly.
                    return
                await self._handle_line(raw.rstrip("\n"))
        except asyncio.CancelledError:
            # Clean exit on stop().
            return

    async def _handle_line(self, line: str) -> None:
        """Dispatch one line to the router and write the engine reply to the writer.

        Builds a MessageEvent (source_trait="sync") with a fresh reply_future, routes
        it, then awaits the future to obtain the engine's free-form text and writes it.
        idempotency_key is a ms-timestamp string (never empty — mirrors the webhook
        fallback).
        """
        reply_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        event = MessageEvent(
            idempotency_key=str(int(time.time() * 1000)),
            session_key=self._cfg.name,
            channel_name=self._cfg.name,
            payload={"text": line},
            delivery_context={},
            source_trait="sync",
            reply_future=reply_future,
        )
        await self._handler.handle(event)
        text = await reply_future
        self._writer(text)
