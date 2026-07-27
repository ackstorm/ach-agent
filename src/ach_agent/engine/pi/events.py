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
)


def pi_text_delta(ev: dict[str, Any]) -> str | None:
    """Return a text delta, unwrapping message_update's nested event."""
    if ev.get("type") != EV_MESSAGE_UPDATE:
        return None
    inner = ev.get(EV_ASSISTANT_INNER) or {}
    if isinstance(inner, dict) and inner.get("type") == EV_INNER_TEXT_DELTA:
        text = inner.get("delta", inner.get("text", ""))
        return str(text) if text else None
    return None


def pi_tool_update(ev: dict[str, Any], session_ref: str) -> OpenCodeToolUpdate | None:
    """Map Pi tool lifecycle events to the shared OpenCodeToolUpdate shape."""
    kind = ev.get("type")
    if kind not in (EV_TOOL_START, EV_TOOL_END):
        return None
    call_id = str(ev.get("toolCallId", ev.get("callId", "")) or "")
    tool_name = str(ev.get("toolName", "") or "")
    input_value = ev.get("args", ev.get("input"))
    tool_input = input_value if isinstance(input_value, dict) else None
    if kind == EV_TOOL_START:
        state: ToolState = ToolStateRunning(input=tool_input, title=str(ev.get("title", "")))
    elif ev.get("isError") or ev.get("error"):
        error = ev.get("error") or ev.get("result", "")
        state = ToolStateError(error=str(error), input=tool_input)
    else:
        result = ev.get("result", ev.get("output", ""))
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
    """Map Pi's assistant message usage to the shared usage shape.

    Pi 0.79 nests usage under ``event.message.usage`` and uses the short
    ``input``/``output`` token names. Keep the older top-level vocabulary as a
    compatibility fallback for scripted/older RPC producers.
    """
    message = ev.get("message")
    message_doc = message if isinstance(message, dict) else {}
    usage = message_doc.get("usage")
    if not isinstance(usage, dict):
        usage = ev.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    cost_total = cost.get("total", 0) if isinstance(cost, dict) else usage.get("costUsd", 0.0)
    message_id = message_doc.get("id", ev.get("messageId", ""))
    input_tokens = usage.get("input", usage.get("inputTokens", 0))
    output_tokens = usage.get("output", usage.get("outputTokens", 0))
    cache_read = usage.get("cacheRead", usage.get("cacheReadTokens", 0))
    cache_write = usage.get("cacheWrite", usage.get("cacheWriteTokens", 0))
    return OpenCodeUsage(
        session_id=session_ref,
        message_id=str(message_id),
        input_tokens=int(input_tokens or 0),
        output_tokens=int(output_tokens or 0),
        cache_read=int(cache_read or 0),
        cache_write=int(cache_write or 0),
        cost=float(cost_total or 0.0),
        duration_ms=int(usage.get("durationMs", ev.get("durationMs", 0)) or 0),
    )


def is_settled(ev: dict[str, Any]) -> bool:
    return ev.get("type") == EV_AGENT_SETTLED
