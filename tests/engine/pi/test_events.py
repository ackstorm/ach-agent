# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ach_agent.engine.base.events import OpenCodeToolUpdate, ToolStateCompleted, ToolStateRunning
from ach_agent.engine.pi import events as pe
from ach_agent.engine.pi.protocol import (
    EV_AGENT_SETTLED,
    EV_ASSISTANT_INNER,
    EV_INNER_TEXT_DELTA,
    EV_MESSAGE_UPDATE,
    EV_TOOL_END,
    EV_TOOL_START,
)


def test_text_delta_unwraps_nested_assistant_message_event() -> None:
    ev = {
        "type": EV_MESSAGE_UPDATE,
        EV_ASSISTANT_INNER: {"type": EV_INNER_TEXT_DELTA, "text": "hi"},
    }
    assert pe.pi_text_delta(ev) == "hi"


def test_non_text_message_update_returns_none() -> None:
    ev = {
        "type": EV_MESSAGE_UPDATE,
        EV_ASSISTANT_INNER: {"type": "reasoning_delta", "text": "x"},
    }
    assert pe.pi_text_delta(ev) is None


def test_tool_start_maps_to_running_update() -> None:
    ev = {"type": EV_TOOL_START, "toolName": "gitlab_mr", "callId": "c1", "input": {"a": 1}}
    tu = pe.pi_tool_update(ev, "ses_1")
    assert isinstance(tu, OpenCodeToolUpdate)
    assert tu.tool_name == "gitlab_mr" and tu.call_id == "c1"
    assert isinstance(tu.state, ToolStateRunning) and tu.state.status == "running"


def test_tool_end_maps_to_completed_update() -> None:
    ev = {"type": EV_TOOL_END, "toolName": "gitlab_mr", "callId": "c1", "output": "done"}
    tu = pe.pi_tool_update(ev, "ses_1")
    assert isinstance(tu, OpenCodeToolUpdate)
    assert isinstance(tu.state, ToolStateCompleted) and tu.state.output == "done"


def test_is_settled() -> None:
    assert pe.is_settled({"type": EV_AGENT_SETTLED}) is True
    assert pe.is_settled({"type": "agent_end"}) is False
