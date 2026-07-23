# SPDX-License-Identifier: Apache-2.0
"""Shared engine-event vocabulary (SP1 §9).

The tool-update / usage / error types BOTH drivers produce into. opencode's SSE parser
(engine/opencode/events.py) and Pi's JSONL mapper (engine/pi/events.py) construct these,
so the harness's on_tool sink and stats mapping stay identical across engines. Field names
keep the OpenCode* prefix (surgical — renaming ripples through channels + the debug console);
Pi fills them best-effort (§5.3)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


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
class OpenCodeUsage:
    """Assistant message.updated — cumulative token/cost/duration for the turn.

    opencode emits one message.updated per assistant message as it completes; the values
    are cumulative for that message, so the consumer keeps the latest. Ported from
    ackbot-process opencode_events.py (ach-agent had dropped this parse as "not needed").
    """

    session_id: str
    message_id: str
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost: float
    duration_ms: int
