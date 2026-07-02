import fakeredis.aioredis
import pytest

from app.reader import parse_entry, read_coverage_start, read_recent, read_window


def test_parse_entry_v1_typed():
    fields = {
        "v": "1",
        "ts": "1700000000000",
        "session_key": "k",
        "channel": "webhook",
        "source": "gitlab",
        "model": "claude-opus-4-8",
        "provider": "unknown",
        "task": "Review !7",
        "input_tokens": "1200",
        "output_tokens": "300",
        "cache_read": "5",
        "cache_write": "6",
        "cost": "0.12",
        "turns": "4",
        "duration_ms": "4000",
        "tokens_per_s": "75.0",
        "status": "completed",
        "retry": "false",
    }
    e = parse_entry("1700000000000-0", fields)
    assert e["model"] == "claude-opus-4-8"
    assert e["cost"] == 0.12
    assert e["output_tokens"] == 300
    assert e["retry"] is False
    assert e["ts_ms"] == 1700000000000


def test_parse_entry_missing_fields_get_defaults():
    e = parse_entry("42-0", {"model": "glm-5-2"})
    assert e["model"] == "glm-5-2"
    assert e["cost"] == 0.0
    assert e["input_tokens"] == 0
    assert e["status"] == "unknown"
    assert e["ts_ms"] == 42  # derived from the stream id


def test_parse_entry_ignores_unknown_fields():
    e = parse_entry("42-0", {"model": "m", "future_field": "x"})
    assert "future_field" not in e


@pytest.mark.asyncio
async def test_read_window_and_recent_and_coverage():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    await client.xadd("ach:sessions", {"v": "1", "model": "a", "cost": "0.1"}, id="100-0")
    await client.xadd("ach:sessions", {"v": "1", "model": "b", "cost": "0.2"}, id="200-0")
    await client.xadd("ach:sessions", {"v": "1", "model": "c", "cost": "0.3"}, id="300-0")

    win = await read_window(client, 150, 250)
    assert [e["model"] for e in win] == ["b"]

    recent = await read_recent(client, 2)
    assert [e["model"] for e in recent] == ["c", "b"]  # newest first

    assert await read_coverage_start(client) == 100
