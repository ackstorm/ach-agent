# Phase 3 — Pi stats-parity verification test

**Goal:** lock in that a Pi turn produces a **fully-populated** `SessionStat` — non-zero `cost`
**and** non-zero `tokens_per_s` — so "beyond parity" (no silent `cost=0`/`tps=0` for Pi) can't
regress. D-3 is already-parity (Pi maps engine-reported cost exactly like opencode,
`engine/pi/events.py:95-109`), so this is **a test, not a mapping change.**

**Files:**
- Create: `tests/stats/test_pi_turn_stat_parity.py`

**Interfaces:**
- Consumes: `ach_agent.engine.pi.events.pi_usage(ev, session_ref) -> OpenCodeUsage | None`
  (returns `.input_tokens/.output_tokens/.cost/.duration_ms`); `ach_agent.stats.models.SessionStat.build(...)` (computes `tokens_per_s = output_tokens / (duration_ms/1000)`, `models.py:57`).
- Produces: nothing (regression gate).

**Established facts:** `test_events.py:56` already asserts `pi_usage` maps `cost==0.3` and tokens.
The net-new coverage here is the **end-to-end** step none of the existing tests cover: that a Pi
usage carrying `durationMs` yields `SessionStat.tokens_per_s > 0` **and** `cost > 0` together.

---

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run it to confirm it passes (parity already holds)**

Run: `uv run pytest tests/stats/test_pi_turn_stat_parity.py -v`
Expected: **PASS** (D-3 is already-parity). If it FAILS, the Pi mapping regressed — fix
`engine/pi/events.py` `pi_usage` (not the test) to preserve cost/duration.

- [ ] **Step 3: Lint**

Run: `uv run ruff check tests/stats/test_pi_turn_stat_parity.py && uv run mypy --strict tests/stats/test_pi_turn_stat_parity.py`
Expected: clean.

- [ ] **Step 4: Commit**

```bash
git add tests/stats/test_pi_turn_stat_parity.py
git commit -m "test(pi): pin end-to-end Pi usage -> SessionStat cost + tokens_per_s

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
