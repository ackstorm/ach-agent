from __future__ import annotations

from ach_agent.engine.events import (
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ReplyAccumulator,
    ToolStateRunning,
    parse_opencode_event,
)


def test_parse_assistant_message_updated_returns_usage():
    frame = {
        "type": "message.updated",
        "properties": {
            "sessionID": "s1",
            "info": {
                "id": "m1",
                "role": "assistant",
                "tokens": {"input": 120, "output": 45, "cache": {"read": 10, "write": 5}},
                "cost": 0.0021,
                "time": {"created": 1000, "completed": 3500},
            },
        },
    }

    event = parse_opencode_event(frame)

    assert isinstance(event, OpenCodeUsage)
    assert event.input_tokens == 120
    assert event.output_tokens == 45
    assert event.cache_read == 10
    assert event.cache_write == 5
    assert event.cost == 0.0021
    assert event.duration_ms == 2500


def test_accumulator_counts_distinct_tools_and_captures_latest_usage():
    acc = ReplyAccumulator()
    for pid in ("p1", "p1", "p2"):  # p1 twice (running→completed) counts once
        acc.add_tool(
            OpenCodeToolUpdate(
                session_id="s", part_id=pid, message_id="m",
                tool_name="t", call_id="c", state=ToolStateRunning(),
            )
        )
    acc.add_usage(OpenCodeUsage("s", "m1", 1, 2, 0, 0, 0.001, 100))
    acc.add_usage(OpenCodeUsage("s", "m2", 10, 20, 0, 0, 0.005, 200))  # cumulative, last wins

    assert acc.tool_count() == 2
    assert acc.usage().output_tokens == 20
    assert acc.usage().cost == 0.005
