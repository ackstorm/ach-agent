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
        "v", "ts", "session_key", "channel", "source", "model", "provider", "task",
        "input_tokens", "output_tokens", "cache_read", "cache_write", "cost", "turns",
        "duration_ms", "tokens_per_s", "status", "retry",
    ):
        assert key in entry, key
