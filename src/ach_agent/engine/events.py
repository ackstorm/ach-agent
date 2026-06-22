# SPDX-License-Identifier: Apache-2.0
"""Typed SSE event dataclasses and stream consumer for opencode HTTP/SSE.

Provides:
  - parse_opencode_event: maps raw JSON event dict → typed event or None
  - consume_sse_to_completion: consumes GET /event stream until terminal event
  - EngineError: raised when session.error is the terminal event

Constraint: No router or Hermes imports (D-08, RTR-06).

Implementation note on SSE termination:
  opencode keeps GET /event open indefinitely after session.idle (heartbeats continue).
  To break cleanly without hanging, the SSE iter task runs in a separate asyncio.Task
  that is cancelled when a terminal event is detected. This avoids the aiohttp content
  reader hanging issue where `async for chunk in response.content` never gets an EOF.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.client import OpenCodeClient

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class EngineError(Exception):
    """Raised when opencode emits a session.error terminal event."""

    def __init__(self, error_type: str, message: str) -> None:
        self.error_type = error_type
        self.message = message
        super().__init__(f"[{error_type}] {message}")


class InvocationTimeout(Exception):
    """Raised when run_invocation exceeds maxInvocationSeconds watchdog.

    The subprocess has been process-group killed, the watchdog metric incremented,
    and the on_kill seam called before this exception is raised (ENG-07, D-03).
    """

    def __init__(self, max_seconds: int) -> None:
        self.max_seconds = max_seconds
        super().__init__(
            f"Invocation exceeded maxInvocationSeconds={max_seconds}s — subprocess killed"
        )


# ---------------------------------------------------------------------------
# Typed event dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class OpenCodeTextUpdate:
    """Text delta from message.part.updated (part.type == 'text')."""

    session_id: str
    part_id: str
    message_id: str
    text: str


@dataclass(slots=True, frozen=True)
class OpenCodeUserMessage:
    """User message echo from message.updated (role == 'user').

    Used to build the set of user message IDs so text accumulation can
    filter out the echo of the user prompt from assistant text deltas.
    """

    session_id: str
    message_id: str


@dataclass(slots=True, frozen=True)
class OpenCodeSessionIdle:
    """Terminal success event — session finished processing."""

    session_id: str


@dataclass(slots=True, frozen=True)
class OpenCodeSessionError:
    """Terminal failure event — session error."""

    session_id: str
    error_type: str
    message: str


# Union of all handled event types
OpenCodeEvent = (
    OpenCodeTextUpdate | OpenCodeUserMessage | OpenCodeSessionIdle | OpenCodeSessionError
)


# ---------------------------------------------------------------------------
# Event parser
# ---------------------------------------------------------------------------


def parse_opencode_event(data: dict[str, Any]) -> OpenCodeEvent | None:
    """Parse a raw opencode SSE event dict into a typed event.

    Returns None for:
      - Unrecognised event types (server.connected, session.updated, ...)
      - session.status with type 'retry' (transient — NOT terminal)
      - message.part.updated with part.type != 'text' (tool, snapshot, ...)
      - message.updated with role == 'assistant' (token/cost info — not needed here)
    """
    event_type = data.get("type", "")
    props = data.get("properties", {})

    if event_type == "message.part.updated":
        part = props.get("part", {})
        part_type = part.get("type", "")
        if part_type != "text":
            return None
        session_id = props.get("sessionID", part.get("sessionID", ""))
        return OpenCodeTextUpdate(
            session_id=session_id,
            part_id=part.get("id", ""),
            message_id=part.get("messageID", ""),
            text=part.get("text", ""),
        )

    if event_type == "message.updated":
        info = props.get("info", {})
        role = info.get("role", "")
        if role == "user":
            # Emit user message ID so consumer can filter text echoes
            return OpenCodeUserMessage(
                session_id=props.get("sessionID", info.get("sessionID", "")),
                message_id=info.get("id", ""),
            )
        # assistant message.updated carries token/cost info — not needed here
        return None

    if event_type == "session.idle":
        return OpenCodeSessionIdle(
            session_id=props.get("sessionID", ""),
        )

    if event_type == "session.error":
        error = props.get("error", {})
        return OpenCodeSessionError(
            session_id=props.get("sessionID", ""),
            error_type=error.get("type", error.get("name", "unknown")),
            message=error.get("message", str(error)),
        )

    # session.status (including type='retry') — transient, do NOT terminate
    # server.connected, session.updated, session.diff, session.next.* — ignored
    # message.part.delta — streaming delta (full text in final message.part.updated)
    return None


# ---------------------------------------------------------------------------
# SSE iteration helper (split from consume_sse_to_completion for testability)
# ---------------------------------------------------------------------------


def _iter_sse_events_from_client(
    client: OpenCodeClient,
    resp: Any,
) -> Any:
    """Thin wrapper around OpenCodeClient.iter_sse_events.

    Returns an async generator that yields OpenCodeEvent objects.
    Separated so tests can patch this without touching client internals.
    """
    from ach_agent.engine.client import OpenCodeClient as _OCC

    return _OCC.iter_sse_events(resp)


# ---------------------------------------------------------------------------
# SSE consumer
# ---------------------------------------------------------------------------


async def _consume_events_from_response(
    client: OpenCodeClient,
    resp: Any,
    result_queue: asyncio.Queue,  # type: ignore[type-arg]
) -> None:
    """Internal coroutine: consume events from resp and put terminal signals on queue.

    Designed to run as an asyncio.Task so it can be cancelled when a terminal
    event is detected. Cancellation cleanly stops the aiohttp content reader.
    """
    try:
        async for event in _iter_sse_events_from_client(client, resp):
            await result_queue.put(event)
    except asyncio.CancelledError:
        pass  # normal exit when cancelled by consume_sse_to_completion
    except Exception as exc:  # noqa: BLE001
        await result_queue.put(exc)  # signal error to the consumer


async def consume_sse_to_completion(
    client: OpenCodeClient,
    session_id: str,
    max_reconnects: int = 3,
) -> str:
    """Consume GET /event SSE stream until session.idle or session.error.

    Returns accumulated text from assistant message.part.updated deltas,
    filtering out any text belonging to user message echo IDs.

    Design: runs the SSE iterator in a separate asyncio.Task so it can be
    cancelled immediately when a terminal event is detected. This avoids
    hanging when opencode keeps the SSE connection open after session.idle
    (it sends heartbeat events indefinitely).

    Raises:
        EngineError: if session.error is the terminal event.
    """
    accumulated: list[str] = []
    user_message_ids: set[str] = set()

    for attempt in range(max_reconnects + 1):
        resp = await client.subscribe_events()
        result_queue: asyncio.Queue = asyncio.Queue()  # type: ignore[type-arg]
        task = asyncio.create_task(_consume_events_from_response(client, resp, result_queue))

        terminal_error: EngineError | None = None
        terminal_idle = False

        try:
            while True:
                try:
                    item = await asyncio.wait_for(result_queue.get(), timeout=300.0)
                except TimeoutError:
                    # 5-minute per-event timeout — something is very wrong
                    raise EngineError("sse_timeout", "SSE stream stalled for 300s")

                if isinstance(item, Exception):
                    # Error from the consumer task
                    import aiohttp

                    if isinstance(item, aiohttp.ClientError) and attempt < max_reconnects:
                        healthy = await client.check_health()
                        if healthy:
                            log.warning(
                                "SSE connection dropped, reconnecting",
                                attempt=attempt + 1,
                                max_reconnects=max_reconnects,
                            )
                            break  # break to reconnect
                    raise item

                event = item
                if isinstance(event, OpenCodeUserMessage):
                    user_message_ids.add(event.message_id)
                elif isinstance(event, OpenCodeTextUpdate):
                    if event.message_id not in user_message_ids:
                        accumulated.append(event.text)
                elif isinstance(event, OpenCodeSessionIdle):
                    log.debug(
                        "session.idle received",
                        session_id=session_id,
                        text_chunks=len(accumulated),
                    )
                    terminal_idle = True
                    break
                elif isinstance(event, OpenCodeSessionError):
                    log.warning(
                        "session.error received",
                        session_id=session_id,
                        error_type=event.error_type,
                        message=event.message,
                    )
                    terminal_error = EngineError(event.error_type, event.message)
                    break
        finally:
            # Cancel the SSE reader task to stop the aiohttp content reader
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
            except (TimeoutError, asyncio.CancelledError):
                pass
            # Release the HTTP response
            try:
                await resp.release()
            except Exception:  # noqa: BLE001
                pass

        if terminal_error is not None:
            raise terminal_error
        if terminal_idle:
            return "".join(accumulated)
        # No terminal event; attempt reconnect
        if attempt >= max_reconnects:
            break

    raise EngineError(
        "sse_exhausted",
        f"SSE stream disconnected after {max_reconnects} reconnect attempts",
    )
