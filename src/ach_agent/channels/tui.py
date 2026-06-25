# SPDX-License-Identifier: Apache-2.0
"""TUI console runner — the `--tui` launch modifier (NOT a channel).

`--tui` is a launch modifier, not a channel: when set, the harness boots fully
(config, hydration, localhost proxies, opencode) but IGNORES the configured
channels and opens a console REPL instead. Each line you type/paste IS the prompt
sent straight to the agent — so you can simulate a cron tick by pasting its prompt,
a webhook/hook by pasting that instruction, etc.

Each line becomes a MessageEvent (source_trait="sync") with a fresh reply_future on a
single stable session (conversational continuity), routed through the bounded lane;
engine_runner resolves the future with the engine's free-form text, which is printed
verbatim — there is NO terminal contract.

Testability: run_tui_console accepts optional reader/writer. The default reader wraps
sys.stdin (blocking readline in a thread executor); the default writer writes to stdout.

RTR-06: NEVER import from hermes_agent.* or engine.* here.
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

log = structlog.get_logger(__name__)

# Stable session for the whole console session → all lines share one FIFO lane
# (serialized) and one logical session for continuity.
_CONSOLE_SESSION_KEY = "tui-console"


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


async def _handle_line(
    handler: MessageHandler,
    line: str,
    writer: Callable[[str], None],
    session_key: str,
) -> None:
    """Dispatch one console line to the router and write the engine reply.

    Builds a MessageEvent (source_trait="sync") with a fresh reply_future, routes it,
    then awaits the future for the engine's free-form text. The line IS the prompt
    (payload['text']); build_engine_prompt returns it verbatim. idempotency_key is a
    ms-timestamp string (unique per line, never empty).
    """
    reply_future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
    event = MessageEvent(
        idempotency_key=str(int(time.time() * 1000)),
        session_key=session_key,
        channel_name="tui-console",
        payload={"text": line},
        # free_form: console replies are raw text — no terminal contract / repair turn.
        delivery_context={"free_form": True},
        source_trait="sync",
        reply_future=reply_future,
    )
    await handler.handle(event)
    text = await reply_future
    writer(text)


async def run_tui_console(
    handler: MessageHandler,
    *,
    reader: Any = None,
    writer: Callable[[str], None] | None = None,
    session_key: str = _CONSOLE_SESSION_KEY,
) -> None:
    """Run the console REPL until EOF (Ctrl-D) or cancellation.

    Reads stdin lines; each non-blank line is routed to the engine and the reply is
    written back. Blank lines are skipped. EOF or CancelledError ends the session.
    """
    rdr = reader if reader is not None else _StdinReader()
    wrt = writer if writer is not None else _default_write
    log.info("tui console started — type a prompt, Ctrl-D to exit")
    try:
        while True:
            raw = await rdr.readline()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", errors="replace")
            if raw == "":
                # EOF — end the session cleanly.
                return
            line = raw.rstrip("\n")
            if not line:
                continue
            await _handle_line(handler, line, wrt, session_key)
    except asyncio.CancelledError:
        return
