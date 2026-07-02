from __future__ import annotations

from ach_agent.engine.events import OpenCodeToolUpdate, ToolStateRunning
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
