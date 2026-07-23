# SPDX-License-Identifier: Apache-2.0
"""Map Pi JSONL events onto the shared engine event vocabulary."""

from __future__ import annotations

from typing import Any

from ach_agent.engine.base.events import (
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)
from ach_agent.engine.pi.protocol import (
    EV_AGENT_SETTLED,
    EV_ASSISTANT_INNER,
    EV_INNER_TEXT_DELTA,
    EV_MESSAGE_UPDATE,
    EV_TOOL_END,
    EV_TOOL_START,
    F_ARGS,
    F_CALL_ID,
    F_DELTA,
    F_ERROR,
    F_INPUT,
    F_IS_ERROR,
    F_OUTPUT,
    F_RESULT,
    F_TEXT,
    F_TITLE,
    F_TOOL_CALL_ID,
    F_TOOL_NAME,
)


def pi_text_delta(ev: dict[str, Any]) -> str | None:
    """Return a text delta, unwrapping message_update's nested event."""
    if ev.get("type") != EV_MESSAGE_UPDATE:
        return None
    inner = ev.get(EV_ASSISTANT_INNER) or {}
    if isinstance(inner, dict) and inner.get("type") == EV_INNER_TEXT_DELTA:
        text = inner.get(F_DELTA, inner.get(F_TEXT, ""))
        return str(text) if text else None
    return None


def pi_tool_update(ev: dict[str, Any], session_ref: str) -> OpenCodeToolUpdate | None:
    """Map Pi tool lifecycle events to the shared OpenCodeToolUpdate shape."""
    kind = ev.get("type")
    if kind not in (EV_TOOL_START, EV_TOOL_END):
        return None
    call_id = str(ev.get(F_TOOL_CALL_ID, ev.get(F_CALL_ID, "")) or "")
    tool_name = str(ev.get(F_TOOL_NAME, "") or "")
    input_value = ev.get(F_ARGS, ev.get(F_INPUT))
    tool_input = input_value if isinstance(input_value, dict) else None
    if kind == EV_TOOL_START:
        state: ToolState = ToolStateRunning(input=tool_input, title=str(ev.get(F_TITLE, "")))
    elif ev.get(F_IS_ERROR) or ev.get(F_ERROR):
        error = ev.get(F_ERROR) or ev.get(F_RESULT, "")
        state = ToolStateError(error=str(error), input=tool_input)
    else:
        result = ev.get(F_RESULT, ev.get(F_OUTPUT, ""))
        if isinstance(result, dict):
            content = result.get("content", [])
            result = "".join(
                str(item.get("text", "")) for item in content if isinstance(item, dict)
            )
        state = ToolStateCompleted(output=str(result), input=tool_input)
    return OpenCodeToolUpdate(
        session_id=session_ref,
        part_id=call_id,
        message_id="",
        tool_name=tool_name,
        call_id=call_id,
        state=state,
    )


def pi_usage(ev: dict[str, Any], session_ref: str) -> OpenCodeUsage | None:
    """Map a Pi usage event to OpenCodeUsage, when it has a usage block."""
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        return None
    return OpenCodeUsage(
        session_id=session_ref,
        message_id=str(ev.get("messageId", "")),
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        cache_read=int(usage.get("cacheReadTokens", 0) or 0),
        cache_write=int(usage.get("cacheWriteTokens", 0) or 0),
        cost=float(usage.get("costUsd", 0.0) or 0.0),
        duration_ms=int(usage.get("durationMs", 0) or 0),
    )


def is_settled(ev: dict[str, Any]) -> bool:
    return ev.get("type") == EV_AGENT_SETTLED
