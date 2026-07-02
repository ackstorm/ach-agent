from __future__ import annotations

from ach_agent.engine.events import OpenCodeToolUpdate, ToolStateCompleted, ToolStateRunning
from ach_agent.main import _log_engine_tool


def test_log_engine_tool_emits_tool_name_and_status(capfd):
    """The default on_tool sink writes one structlog line naming the tool + its state."""
    update = OpenCodeToolUpdate(
        session_id="s1",
        part_id="p1",
        message_id="m1",
        tool_name="mcp-gitlab-ro.gitlab_get_merge_request",
        call_id="c1",
        state=ToolStateRunning(),
    )

    _log_engine_tool(update)

    # Logs go to STDERR (STDOUT carries only the agent reply); check both so the test
    # asserts intent regardless of stream (same pattern as test_cron.py).
    out, err = capfd.readouterr()
    combined = out + err
    assert "engine: tool" in combined
    assert "mcp-gitlab-ro.gitlab_get_merge_request" in combined
    assert "running" in combined


def test_log_engine_tool_includes_title_and_truncated_output(capfd):
    """A completed tool logs its title (action description) and a truncated result."""
    update = OpenCodeToolUpdate(
        session_id="s1",
        part_id="p1",
        message_id="m1",
        tool_name="mcp-gitlab-ro.gitlab_get_merge_request",
        call_id="c1",
        state=ToolStateCompleted(
            title="Get merge request !7",
            output="x" * 500,
        ),
    )

    _log_engine_tool(update)

    combined = capfd.readouterr()
    out = combined.out + combined.err
    assert "engine: tool" in out
    assert "Get merge request !7" in out
    assert "completed" in out
    # output is truncated to 300 chars — the 500-char field must not appear in full
    assert "x" * 500 not in out
    assert "x" * 300 in out


def test_log_engine_tool_cleans_doubled_mcp_prefix_and_omits_empty_fields(capfd):
    """Doubled MCP prefix → server/tool, and empty action/detail are dropped on running."""
    update = OpenCodeToolUpdate(
        session_id="s1",
        part_id="p1",
        message_id="m1",
        tool_name="mcp-gitlab-ro_mcp-gitlab-ro_gitlab_get_merge_request",
        call_id="c1",
        state=ToolStateRunning(),
    )

    _log_engine_tool(update)

    out, err = capfd.readouterr()
    combined = out + err
    assert "mcp-gitlab-ro/gitlab_get_merge_request" in combined
    assert "mcp-gitlab-ro_mcp-gitlab-ro_" not in combined
    # a running tool has no title/output → those keys must not appear at all
    assert "action=" not in combined
    assert "detail=" not in combined


def test_usage_round_trips_through_stats_for_summary(capfd):
    """The accumulator's tool_count + usage are what the summary log reads from stats."""
    from ach_agent.engine.events import OpenCodeUsage, ReplyAccumulator

    acc = ReplyAccumulator()
    acc.add_usage(OpenCodeUsage("s", "m1", 100, 40, 0, 0, 0.0031, 1200))
    stats: dict = {"tool_count": acc.tool_count(), "usage": acc.usage()}

    usage = stats["usage"]
    # This mirrors exactly what _make_engine_runner logs:
    assert stats["tool_count"] == 0
    assert usage.input_tokens == 100
    assert usage.output_tokens == 40
    assert usage.cost == 0.0031
    assert usage.duration_ms == 1200
