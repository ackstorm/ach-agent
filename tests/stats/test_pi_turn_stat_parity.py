# SPDX-License-Identifier: Apache-2.0
"""A Pi turn's usage must reach SessionStat with cost AND tokens_per_s populated."""

from __future__ import annotations

from ach_agent.engine.pi.events import pi_usage
from ach_agent.stats.models import SessionStat


def test_pi_usage_yields_populated_session_stat() -> None:
    ev = {
        "message": {
            "id": "m1",
            "usage": {
                "input": 100,
                "output": 40,
                "cost": {"total": 0.42},
                "durationMs": 2000,
            },
        }
    }
    usage = pi_usage(ev, "sess-1")
    assert usage is not None

    stat = SessionStat.build(
        ts_ms=0,
        session_key="sess-1",
        channel="cron",
        source="cron",
        model="anthropic/claude-sonnet-5",
        provider="unknown",
        raw_task="hi",
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_read=usage.cache_read,
        cache_write=usage.cache_write,
        cost=usage.cost,
        turns=1,
        duration_ms=usage.duration_ms,
        status="completed",
        retry=False,
    )
    # Beyond parity: neither cost nor throughput is silently zero for a Pi turn.
    assert stat.cost == 0.42
    assert stat.tokens_per_s == 20.0  # 40 output / 2.0s
