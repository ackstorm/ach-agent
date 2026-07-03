from ach_agent.engine.events import (
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolStateCompleted,
    ToolStateError,
)
from ach_agent.stats.sink import build_session_stat, build_tool_stat


class _Event:
    channel_name = "webhook"
    session_key = "gitlab:git.example.com/g/r"
    source = "gitlab"


def test_build_session_stat_maps_usage_and_meta():
    usage = OpenCodeUsage(
        session_id="s",
        message_id="m",
        input_tokens=1200,
        output_tokens=300,
        cache_read=5,
        cache_write=6,
        cost=0.12,
        duration_ms=4000,
    )
    obj = {"text": "done", "action": "reply"}
    turn_stats = {"usage": usage, "tool_count": 4, "aborted": False}
    stat = build_session_stat(
        _Event(), obj, turn_stats, model="claude-opus-4-8", ts_ms=1_700_000_000_000
    )
    assert stat.model == "claude-opus-4-8"
    assert stat.channel == "webhook"
    assert stat.source == "gitlab"
    assert stat.input_tokens == 1200
    assert stat.output_tokens == 300
    assert stat.cost == 0.12
    assert stat.turns == 4
    assert stat.status == "completed"


def test_build_session_stat_handles_missing_usage_and_aborted():
    obj = {"text": "", "action": "none"}
    turn_stats = {"usage": None, "tool_count": 0, "aborted": True}
    stat = build_session_stat(_Event(), obj, turn_stats, model="glm-5-2", ts_ms=1)
    assert stat.input_tokens == 0
    assert stat.cost == 0.0
    assert stat.status == "aborted"


def _tool_update(state):
    return OpenCodeToolUpdate(
        session_id="s",
        part_id="p",
        message_id="m",
        tool_name="mcp-gitlab-ro_mcp-gitlab-ro_gitlab_get_merge_request",
        call_id="c1",
        state=state,
    )


def test_build_tool_stat_completed_maps_fields_and_sizes():
    upd = _tool_update(ToolStateCompleted(output="ok" * 10, input={"id": 5}))
    stat = build_tool_stat(
        upd,
        session_key="k",
        channel="webhook",
        source="gitlab",
        model="claude-opus-4-8",
        tool="mcp-gitlab-ro/gitlab_get_merge_request",
        tool_type="mcp",
        duration_ms=1500,
        ts_ms=1,
    )
    assert stat.status == "completed"
    assert stat.tool_type == "mcp"
    assert stat.duration_ms == 1500
    assert stat.output_size == 20
    assert stat.input_size == len("{'id': 5}")
    assert stat.error == ""
    assert stat.to_entry()["duration_ms"] == "1500"


def test_build_tool_stat_error_and_unknown_duration():
    upd = _tool_update(ToolStateError(error="boom"))
    stat = build_tool_stat(
        upd,
        session_key="k",
        channel="webhook",
        source="gitlab",
        model="m",
        tool="bash",
        tool_type="builtin",
        duration_ms=None,
        ts_ms=1,
    )
    assert stat.status == "error"
    assert stat.error == "boom"
    assert stat.output_size == 4  # falls back to error length
    assert stat.duration_ms is None
    assert stat.to_entry()["duration_ms"] == ""  # unknown → empty, not "0"
