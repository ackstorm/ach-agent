from ach_agent.stats.models import SessionStat


def _build(**over):
    base = dict(
        ts_ms=1_700_000_000_000,
        session_key="gitlab:git.example.com/group/repo",
        channel="webhook",
        source="gitlab",
        model="claude-opus-4-8",
        provider="anthropic",
        raw_task="Review merge request !7 ek_secret123",
        input_tokens=1000,
        output_tokens=500,
        cache_read=10,
        cache_write=20,
        cost=0.42,
        turns=3,
        duration_ms=5000,
        status="completed",
        retry=False,
    )
    base.update(over)
    return SessionStat.build(**base)


def test_build_redacts_task():
    stat = _build()
    assert "ek_secret123" not in stat.task


def test_build_computes_tokens_per_s():
    stat = _build(output_tokens=1000, duration_ms=2000)
    assert stat.tokens_per_s == 500.0  # 1000 tok / 2.0 s


def test_tokens_per_s_zero_duration_is_zero():
    stat = _build(output_tokens=1000, duration_ms=0)
    assert stat.tokens_per_s == 0.0


def test_to_entry_is_all_strings_and_versioned():
    entry = _build().to_entry()
    assert entry["v"] == "1"
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in entry.items())
    assert entry["model"] == "claude-opus-4-8"
    assert entry["cost"] == "0.42"
    assert entry["retry"] == "false"


def test_to_entry_roundtrip_fields_present():
    entry = _build().to_entry()
    for key in (
        "v",
        "ts",
        "session_key",
        "channel",
        "source",
        "model",
        "provider",
        "task",
        "input_tokens",
        "output_tokens",
        "cache_read",
        "cache_write",
        "cost",
        "turns",
        "duration_ms",
        "tokens_per_s",
        "status",
        "retry",
    ):
        assert key in entry, key


def test_redact_scrubs_without_truncating():
    from ach_agent.stats.models import redact

    text = "x" * 200 + " token ek_abc123DEF-456 tail"
    out = redact(text)
    assert "ek_abc123DEF-456" not in out
    assert "[redacted]" in out
    assert len(out) > 80  # redact() must NOT truncate — that is redact_task's job


def test_build_tool_stat_redacts_error():
    from types import SimpleNamespace

    from ach_agent.stats.sink import build_tool_stat

    update = SimpleNamespace(
        state=SimpleNamespace(
            status="error",
            output="",
            error="upstream 401: header x-ach-key: ek_live_SECRET99 rejected",
            input=None,
        )
    )
    stat = build_tool_stat(
        update,
        session_key="k",
        channel="c",
        source="s",
        model="m",
        tool="t",
        tool_type="mcp",
        duration_ms=1,
        ts_ms=0,
    )
    assert "ek_live_SECRET99" not in stat.error
    assert "[redacted]" in stat.error
