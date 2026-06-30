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
from collections.abc import Callable
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
    """Cumulative text snapshot from message.part.updated (part.type == 'text').

    Carries the FULL text of the part so far (a growing, append-only snapshot — NOT a
    delta). opencode emits it periodically during generation and once finalized
    (time.end set); the consumer streams only the new suffix per part_id. This is the
    sole text path — message.part.delta events are intentionally ignored (they only buy
    token-granularity realtime, which the ACH forwarder buffers away anyway).
    """

    session_id: str
    part_id: str
    message_id: str
    text: str


# Tool-part state — channel-agnostic renderer can show "running a tool", its result,
# or its error. Parsed in full even though the tui currently renders only
# `running`/`error`; getting the event right is the point.
@dataclass(slots=True, frozen=True)
class ToolStateRunning:
    input: dict[str, Any] | None = None
    title: str = ""  # opencode's human-readable description of the call, when present
    status: str = "running"


@dataclass(slots=True, frozen=True)
class ToolStateCompleted:
    output: str = ""
    input: dict[str, Any] | None = None
    title: str = ""
    status: str = "completed"


@dataclass(slots=True, frozen=True)
class ToolStateError:
    error: str = ""
    input: dict[str, Any] | None = None
    status: str = "error"


ToolState = ToolStateRunning | ToolStateCompleted | ToolStateError


@dataclass(slots=True, frozen=True)
class OpenCodeToolUpdate:
    """Tool-part lifecycle from message.part.updated (part.type == 'tool').

    opencode emits these as a tool moves pending → running → completed/error. The
    `pending` state carries nothing renderable and is dropped at parse time (state=None).
    `tool_name` is the RAW opencode id (e.g. mcp-…_auth_wait); display-cleaning is a
    per-channel concern. `part_id` is the stable key for de-duping repeat updates.
    """

    session_id: str
    part_id: str
    message_id: str
    tool_name: str
    call_id: str
    state: ToolState


@dataclass(slots=True, frozen=True)
class OpenCodeUserMessage:
    """User message echo from message.updated (role == 'user').

    Used to build the set of user message IDs so text accumulation can
    filter out the echo of the user prompt from assistant text deltas.
    """

    session_id: str
    message_id: str


@dataclass(slots=True, frozen=True)
class OpenCodeStreamReady:
    """First event opencode emits on a fresh GET /event connection (server.connected).

    Signals our subscriber is registered server-side, so the prompt can be sent without
    the turn's early events racing ahead of subscription (an intermittent loss otherwise).
    Carries nothing renderable.
    """


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
    OpenCodeTextUpdate
    | OpenCodeToolUpdate
    | OpenCodeUserMessage
    | OpenCodeStreamReady
    | OpenCodeSessionIdle
    | OpenCodeSessionError
)


# ---------------------------------------------------------------------------
# Event parser
# ---------------------------------------------------------------------------


def _parse_tool_state(state_data: dict[str, Any]) -> ToolState | None:
    """Map a tool part's ``state`` dict to a typed ToolState (None for pending)."""
    status = state_data.get("status", "pending")
    if status == "running":
        return ToolStateRunning(input=state_data.get("input"), title=state_data.get("title", ""))
    if status == "completed":
        return ToolStateCompleted(
            output=state_data.get("output", ""),
            input=state_data.get("input"),
            title=state_data.get("title", ""),
        )
    if status == "error":
        return ToolStateError(error=state_data.get("error", ""), input=state_data.get("input"))
    return None  # pending — nothing renderable yet


def parse_opencode_event(data: dict[str, Any]) -> OpenCodeEvent | None:
    """Parse a raw opencode SSE event dict into a typed event.

    Returns None for:
      - Unrecognised event types (server.connected, session.updated, ...)
      - session.status with type 'retry' (transient — NOT terminal)
      - message.part.updated for non-rendered parts (step-start/finish, snapshot, reasoning)
        and tool parts still in 'pending' state
      - message.updated with role == 'assistant' (token/cost info — not needed here)
    """
    event_type = data.get("type", "")
    props = data.get("properties", {})

    if event_type == "message.part.updated":
        part = props.get("part", {})
        part_type = part.get("type", "")
        session_id = props.get("sessionID", part.get("sessionID", ""))
        if part_type == "text":
            return OpenCodeTextUpdate(
                session_id=session_id,
                part_id=part.get("id", ""),
                message_id=part.get("messageID", ""),
                text=part.get("text", ""),
            )
        if part_type == "tool":
            state = _parse_tool_state(part.get("state", {}))
            if state is None:
                return None  # pending — nothing renderable yet
            return OpenCodeToolUpdate(
                session_id=session_id,
                part_id=part.get("id", ""),
                message_id=part.get("messageID", ""),
                tool_name=part.get("tool", ""),
                call_id=part.get("callID", part.get("call_id", "")),
                state=state,
            )
        return None  # step-start / step-finish / snapshot / reasoning — not rendered

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

    if event_type == "server.connected":
        # First event on a new /event connection — subscription is live server-side.
        return OpenCodeStreamReady()

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
    # session.updated, session.diff, session.next.* — ignored
    # message.part.delta — token-granular stream, intentionally ignored (text comes from
    #   the cumulative message.part.updated snapshots; see OpenCodeTextUpdate)
    return None


# ---------------------------------------------------------------------------
# Reply accumulator — shared reduction of text/tool part events
# ---------------------------------------------------------------------------


class ReplyAccumulator:
    """Reduces opencode part events into the assistant reply plus live render chrome.

    Text parts arrive as growing, append-only cumulative snapshots, so only the new suffix
    per part_id is emitted — with a blank line between distinct parts so consecutive
    assistant messages don't run together. Tool lifecycle is surfaced once per
    (part_id, status). ``text()`` returns the full reply for the terminal return value.

    ``on_text``/``on_tool`` are optional live sinks (the --tui console wires them to stream
    the reply and show tool progress); both consume paths share this reducer so the
    suffix/separator/dedup logic lives in exactly one place.
    """

    def __init__(
        self,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    ) -> None:
        self._on_text = on_text
        self._on_tool = on_tool
        self._part_text: dict[str, str] = {}
        self._last_text_part_id: str | None = None
        self._tool_seen: set[tuple[str, str]] = set()
        self._chunks: list[str] = []

    def text(self) -> str:
        """The full accumulated reply text."""
        return "".join(self._chunks)

    def add_text(self, part_id: str, full_text: str) -> None:
        """Accumulate/stream the new suffix of a (cumulative) text-part snapshot."""
        cur = self._part_text.get(part_id, "")
        # Append-only → new is a prefix-extension; a non-prefix snapshot (revision) isn't
        # produced by opencode, so fall back to the whole text rather than guessing a diff.
        extra = full_text[len(cur) :] if full_text.startswith(cur) else full_text
        if not extra:
            return
        # First text of a NEW part following another → separate them.
        if part_id not in self._part_text and self._last_text_part_id not in (None, part_id):
            self._emit("\n\n")
        self._part_text[part_id] = full_text
        self._last_text_part_id = part_id
        self._emit(extra)

    def add_tool(self, event: OpenCodeToolUpdate) -> None:
        """Surface a tool-lifecycle update once per (part_id, status). Render-only chrome."""
        key = (event.part_id, event.state.status)
        if self._on_tool is not None and key not in self._tool_seen:
            self._tool_seen.add(key)
            self._on_tool(event)

    def _emit(self, chunk: str) -> None:
        self._chunks.append(chunk)
        if self._on_text is not None:
            self._on_text(chunk)


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

    Returns accumulated assistant text, filtering out any text belonging to user message
    echo IDs. message.part.updated carries the FULL cumulative text per part, so only the
    new suffix is appended per part_id (see OpenCodeTextUpdate) — appending each snapshot
    raw would duplicate the text.

    Design: runs the SSE iterator in a separate asyncio.Task so it can be
    cancelled immediately when a terminal event is detected. This avoids
    hanging when opencode keeps the SSE connection open after session.idle
    (it sends heartbeat events indefinitely).

    Raises:
        EngineError: if session.error is the terminal event.
    """
    user_message_ids: set[str] = set()
    # Shared reducer: append only the new suffix per part_id (persisted across reconnect
    # attempts, since opencode resends the same growing snapshots).
    acc = ReplyAccumulator()

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
                        acc.add_text(event.part_id, event.text)
                elif isinstance(event, OpenCodeSessionIdle):
                    log.debug("session.idle received", session_id=session_id)
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
            return acc.text()
        # No terminal event; attempt reconnect
        if attempt >= max_reconnects:
            break

    raise EngineError(
        "sse_exhausted",
        f"SSE stream disconnected after {max_reconnects} reconnect attempts",
    )
