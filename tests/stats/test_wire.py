from ach_agent.engine.events import OpenCodeUsage
from ach_agent.stats.sink import build_session_stat


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
