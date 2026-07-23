# SPDX-License-Identifier: Apache-2.0
"""Typed SSE event dataclasses and stream consumer for opencode HTTP/SSE.

Provides:
  - parse_opencode_event: maps raw JSON event dict → typed event or None
  - ReplyAccumulator: shared text/tool reducer (prefix-dedup of cumulative snapshots)
  - _consume_events_from_response: shared SSE reader helper
  - EngineError / _SendFailed: terminal-signal exceptions
  The live SSE consumer (subscribe → send-once → consume, with bounded health-gated reconnect
  and mid-invocation liveness) lives in ``lifecycle.consume_sse_after_send`` and reuses these
  helpers.

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

from ach_agent.engine.base.events import (  # noqa: F401
    EngineError,
    InvocationTimeout,
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Exception
# ---------------------------------------------------------------------------


class _SendFailed(Exception):
    """Wraps a send_message failure so the SSE consume loop treats it as terminal.

    A send-POST failure must NEVER trigger a reconnect/re-send — that would start a
    duplicate opencode turn. The loop unwraps `.original` and raises it. A stream-reader
    drop, by contrast, is pushed to the queue as the raw ``aiohttp.ClientError`` and IS
    reconnectable.
    """

    def __init__(self, original: BaseException) -> None:
        self.original = original
        super().__init__(f"send_message failed: {original!r}")


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
    | OpenCodeUsage
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
        # assistant message.updated carries cumulative token/cost info for the turn.
        tokens = info.get("tokens", {})
        cache = tokens.get("cache", {})
        time_data = info.get("time", {})
        created = time_data.get("created", 0)
        completed = time_data.get("completed", 0)
        duration_ms = int(completed - created) if completed and created else 0
        return OpenCodeUsage(
            session_id=props.get("sessionID", info.get("sessionID", "")),
            message_id=info.get("id", ""),
            input_tokens=tokens.get("input", 0),
            output_tokens=tokens.get("output", 0),
            cache_read=cache.get("read", 0),
            cache_write=cache.get("write", 0),
            cost=info.get("cost", 0.0),
            duration_ms=duration_ms,
        )

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
        self._tool_part_ids: set[str] = set()
        self._usage: OpenCodeUsage | None = None

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
        self._tool_part_ids.add(event.part_id)
        key = (event.part_id, event.state.status)
        if self._on_tool is not None and key not in self._tool_seen:
            self._tool_seen.add(key)
            self._on_tool(event)

    def add_usage(self, event: OpenCodeUsage) -> None:
        """Capture the latest (cumulative) assistant usage for the turn."""
        self._usage = event

    def tool_count(self) -> int:
        """Number of distinct tool calls in the turn (by part_id)."""
        return len(self._tool_part_ids)

    def usage(self) -> OpenCodeUsage | None:
        """The latest assistant usage, or None if no message.updated arrived."""
        return self._usage

    def _emit(self, chunk: str) -> None:
        self._chunks.append(chunk)
        if self._on_text is not None:
            self._on_text(chunk)


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
    from ach_agent.engine.client import OpenCodeClient as _OCC

    try:
        async for event in _OCC.iter_sse_events(resp):
            await result_queue.put(event)
    except asyncio.CancelledError:
        pass  # normal exit when cancelled by the live consumer (consume_sse_after_send)
    except Exception as exc:  # noqa: BLE001
        await result_queue.put(exc)  # signal error to the consumer
