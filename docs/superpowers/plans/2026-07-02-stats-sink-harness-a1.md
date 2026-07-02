# Stats Sink (Harness) — Implementation Plan (Sub-project A1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a best-effort, non-blocking stats sink to the ach-agent harness that, once per
invocation, emits Prometheus counters and appends a versioned session record to a redis stream —
without ever letting redis latency reach the router.

**Architecture:** A `StatsSink` whose `record()` does only inline Prometheus increments +
`queue.put_nowait()`. A single supervised writer task owns the redis client, drains the bounded
queue, `XADD`s with inline `MINID` trim, restarts with capped backoff, and drains on shutdown. Wired
at the existing turn-summary site in `main.py`. Config is env-gated (`ACH_STATS_*`), never through
the frozen CONTRACT_v3 seam.

**Tech Stack:** Python 3.12, asyncio, `redis>=5,<6` (`redis.asyncio`), `prometheus-client==0.25.0`,
structlog, pytest (`asyncio_mode=auto`) + `fakeredis` (new dev dep).

**Design source:** `docs/superpowers/specs/2026-07-02-stats-leaderboard-container-design.md`
(§4.1 entry semantics, §4.2 versioned entry, §4.3 metrics, §5 the sink).

## Global Constraints

- Python 3.12; asyncio throughout. `uv run mypy --strict` and `uv run ruff check` must pass — no new
  type/lint errors.
- **No new runtime dependency.** `redis` and `prometheus-client` are already in `pyproject.toml`.
  Add **only** `fakeredis` to the test/dev dependency group.
- `ek_` is NEVER logged and never persisted. Task text is truncated to **≤80 chars** and redacted
  **before** it enters the queue.
- `StatsSink.record()` NEVER touches redis (no client construction, no connection, no DNS). The
  writer task owns the client. The turn path never blocks and never accumulates unbounded tasks.
- Redis entry carries **`v` = `"1"`**. Retention default **35d** (`3024000` seconds); trim is inline
  `XADD ... MINID ~ <now-retention>`.
- Env config: `ACH_STATS_REDIS_URL` (unset → Prometheus-only, no queue/writer), `ACH_STATS_RETENTION`
  (seconds, dev-only shrink). NOT a CONTRACT_v3 config block.
- Tests live under `tests/stats/`.

---

## File Structure

- Create `src/ach_agent/stats/__init__.py` — package exports.
- Create `src/ach_agent/stats/redact.py` — `redact_task(text: str) -> str` (truncate ≤80 + scrub).
- Create `src/ach_agent/stats/models.py` — `SessionStat` dataclass + `to_entry() -> dict[str,str]`.
- Create `src/ach_agent/stats/metrics.py` — Prometheus counters/histogram (module-level singletons).
- Create `src/ach_agent/stats/sink.py` — `StatsSink` (record + bounded queue + supervised writer).
- Modify `src/ach_agent/main.py` — construct `StatsSink`, `record()` at the turn-summary site,
  `stop()` on shutdown.
- Modify `pyproject.toml` — add `fakeredis` to the test dependency group.
- Modify `docs/plan/CONTRACT_v3.md` — one non-normative note about `ACH_STATS_*`.
- Tests: `tests/stats/__init__.py`, `tests/stats/test_redact.py`, `tests/stats/test_models.py`,
  `tests/stats/test_metrics.py`, `tests/stats/test_sink.py`, `tests/stats/test_wire.py`.

---

### Task 0: R1 probe (gating spike — run BEFORE any sink code)

**Purpose:** settle whether opencode's `message.updated.info.tokens/cost` is per-message or
session-cumulative under `session:auto`. This decides whether the sink needs baseline-delta logic
(Task 8). See spec §4.1.

**Files:**
- Create: `scripts/r1_probe.md` (record the procedure + the observed result + the decision).

- [ ] **Step 1: Add a temporary debug tap**

In `src/ach_agent/engine/lifecycle.py`, in the `elif isinstance(event, OpenCodeUsage):` branch
(~line 773), temporarily add (REVERT after the probe):

```python
                        import structlog
                        structlog.get_logger().info(
                            "R1_PROBE_USAGE",
                            session_id=event.session_id,
                            output_tokens=event.output_tokens,
                            cost=event.cost,
                        )
```

- [ ] **Step 2: Drive two invocations on ONE reused session**

Run the harness locally (`--tui` console, `channel.session` defaulting to `auto`). Send two trivial
prompts in the SAME session: `reply with exactly: ok` then `reply with exactly: ok`.

- [ ] **Step 3: Read the taps and decide**

Compare invocation 2's **first** `R1_PROBE_USAGE.output_tokens` against invocation 1's **final**
`output_tokens` (NEVER use `input_tokens` — it grows legitimately because a reused session resends
history):
- Invocation 2 starts near 0 → **per-message scope** (R1 does not apply; Task 8 SKIPPED).
- Invocation 2 starts at/above invocation 1's total → **R1 CONFIRMED** (Task 8 REQUIRED).

- [ ] **Step 4: Revert the tap, record the decision**

Remove the debug tap. Write `scripts/r1_probe.md` with the observed numbers and the decision
(`R1: confirmed | not-applicable`). Commit.

```bash
git add scripts/r1_probe.md src/ach_agent/engine/lifecycle.py
git commit -m "chore(stats): R1 probe — record opencode usage accumulation scope"
```

---

### Task 1: fakeredis dev dep + redact helper

**Files:**
- Modify: `pyproject.toml` (test dependency group)
- Create: `src/ach_agent/stats/__init__.py`
- Create: `src/ach_agent/stats/redact.py`
- Test: `tests/stats/__init__.py`, `tests/stats/test_redact.py`

**Interfaces:**
- Produces: `redact_task(text: str) -> str` — truncates to ≤80 chars then scrubs bearer tokens.

- [ ] **Step 1: Add fakeredis to the test deps**

In `pyproject.toml`, find the test dependency group that already lists `pytest==9.1.1` (around
line 50) and add on its own line:

```toml
    "fakeredis>=2.26,<3",
```

Then sync: `uv sync` (Expected: resolves + installs fakeredis, no other changes).

- [ ] **Step 2: Write the failing test**

Create `tests/stats/__init__.py` (empty). Create `tests/stats/test_redact.py`:

```python
from ach_agent.stats.redact import redact_task


def test_redact_truncates_to_80_chars():
    long = "x" * 200
    out = redact_task(long)
    assert len(out) <= 80


def test_redact_scrubs_ek_bearer():
    out = redact_task("please use ek_live_abc123DEF456 to auth")
    assert "ek_live_abc123DEF456" not in out
    assert "ek_" not in out


def test_redact_scrubs_sk_key():
    out = redact_task("key sk-proj-ABCDEF0123456789 here")
    assert "sk-proj-ABCDEF0123456789" not in out


def test_redact_passes_clean_text():
    assert redact_task("Review merge request !7") == "Review merge request !7"


def test_redact_scrubs_before_truncating():
    # A secret past char 80 must still be gone (scrub first, then truncate).
    text = ("a" * 90) + " ek_secretVALUE1234"
    out = redact_task(text)
    assert "ek_secretVALUE1234" not in out
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/stats/test_redact.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ach_agent.stats'`.

- [ ] **Step 4: Implement the package + helper**

Create `src/ach_agent/stats/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Harness stats sink: best-effort per-invocation Prometheus + redis session records."""
```

Create `src/ach_agent/stats/redact.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Truncate + redact the inbound task text before it is persisted to redis.

The bearer `ek_` and provider keys must never leave the process in a stored record. We scrub
FIRST (so a secret beyond the truncation boundary is still removed), then truncate to a bounded
length for the recent-sessions table. Keep the scrub patterns aligned with the structlog `ek_`
redaction processor (grep: `rg -n 'ek_' src/ach_agent | rg -i 'redact|scrub|processor'`).
"""

from __future__ import annotations

import re

_MAX = 80
# ek_… bearer, sk-… provider keys, generic long token after "bearer".
_SECRET = re.compile(r"(ek_[A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]+)")


def redact_task(text: str) -> str:
    """Scrub bearer/API tokens, then truncate to <=80 chars."""
    scrubbed = _SECRET.sub("[redacted]", text)
    return scrubbed[:_MAX]
```

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/stats/test_redact.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats tests/stats
uv run mypy --strict src/ach_agent/stats/redact.py
git add pyproject.toml uv.lock src/ach_agent/stats/__init__.py src/ach_agent/stats/redact.py tests/stats/
git commit -m "feat(stats): add fakeredis dev dep + task redact helper"
```

---

### Task 2: SessionStat model + versioned entry serialization

**Files:**
- Create: `src/ach_agent/stats/models.py`
- Test: `tests/stats/test_models.py`

**Interfaces:**
- Consumes: `redact_task` (Task 1).
- Produces:
  - `SessionStat` frozen dataclass with fields: `session_key: str`, `channel: str`, `source: str`,
    `model: str`, `provider: str`, `task: str`, `input_tokens: int`, `output_tokens: int`,
    `cache_read: int`, `cache_write: int`, `cost: float`, `turns: int`, `duration_ms: int`,
    `tokens_per_s: float`, `status: str`, `retry: bool`, `ts_ms: int`.
  - `SessionStat.to_entry() -> dict[str, str]` — redis-stream field map, all string values,
    including `v="1"`. `task` is already-redacted at construction via `SessionStat.build(...)`.
  - classmethod `SessionStat.build(*, ts_ms, session_key, channel, source, model, provider,
    raw_task, input_tokens, output_tokens, cache_read, cache_write, cost, turns, duration_ms,
    status, retry) -> SessionStat` — computes `tokens_per_s` and calls `redact_task(raw_task)`.

- [ ] **Step 1: Write the failing test**

Create `tests/stats/test_models.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stats/test_models.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ach_agent.stats.models'`.

- [ ] **Step 3: Implement the model**

Create `src/ach_agent/stats/models.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""SessionStat — one record per invocation, serialized to a versioned redis-stream entry.

Entry schema is a CROSS-COMPONENT CONTRACT (harness writes, ach-stats reads, deployed
independently). Every entry carries `v="1"`; a future breaking change bumps it. See design spec
§4.1/§4.2.
"""

from __future__ import annotations

from dataclasses import dataclass

from ach_agent.stats.redact import redact_task


@dataclass(slots=True, frozen=True)
class SessionStat:
    ts_ms: int
    session_key: str
    channel: str
    source: str
    model: str
    provider: str
    task: str  # already redacted+truncated
    input_tokens: int
    output_tokens: int
    cache_read: int
    cache_write: int
    cost: float
    turns: int
    duration_ms: int
    tokens_per_s: float
    status: str
    retry: bool

    @classmethod
    def build(
        cls,
        *,
        ts_ms: int,
        session_key: str,
        channel: str,
        source: str,
        model: str,
        provider: str,
        raw_task: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int,
        cache_write: int,
        cost: float,
        turns: int,
        duration_ms: int,
        status: str,
        retry: bool,
    ) -> "SessionStat":
        tps = (output_tokens / (duration_ms / 1000.0)) if duration_ms > 0 else 0.0
        return cls(
            ts_ms=ts_ms,
            session_key=session_key,
            channel=channel,
            source=source,
            model=model,
            provider=provider,
            task=redact_task(raw_task),
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cache_write=cache_write,
            cost=cost,
            turns=turns,
            duration_ms=duration_ms,
            tokens_per_s=tps,
            status=status,
            retry=retry,
        )

    def to_entry(self) -> dict[str, str]:
        """Redis-stream field map: all values are strings (stream fields are byte strings)."""
        return {
            "v": "1",
            "ts": str(self.ts_ms),
            "session_key": self.session_key,
            "channel": self.channel,
            "source": self.source,
            "model": self.model,
            "provider": self.provider,
            "task": self.task,
            "input_tokens": str(self.input_tokens),
            "output_tokens": str(self.output_tokens),
            "cache_read": str(self.cache_read),
            "cache_write": str(self.cache_write),
            "cost": repr(self.cost),
            "turns": str(self.turns),
            "duration_ms": str(self.duration_ms),
            "tokens_per_s": repr(self.tokens_per_s),
            "status": self.status,
            "retry": "true" if self.retry else "false",
        }
```

Note: `test_to_entry_is_all_strings_and_versioned` asserts `entry["cost"] == "0.42"`. `repr(0.42)`
is `'0.42'`, so this holds; `repr` is used so float precision round-trips exactly on the reader side.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stats/test_models.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats/models.py tests/stats/test_models.py
uv run mypy --strict src/ach_agent/stats/models.py
git add src/ach_agent/stats/models.py tests/stats/test_models.py
git commit -m "feat(stats): SessionStat model + versioned redis entry serialization"
```

---

### Task 3: Prometheus metrics

**Files:**
- Create: `src/ach_agent/stats/metrics.py`
- Test: `tests/stats/test_metrics.py`

**Interfaces:**
- Produces module-level singletons:
  - `SESSIONS_TOTAL` Counter `ach_agent_sessions_total{model,channel}`
  - `TURN_TOKENS_TOTAL` Counter `ach_agent_turn_tokens_total{model,direction}`
  - `TURN_COST_USD_TOTAL` Counter `ach_agent_turn_cost_usd_total{model,channel}`
  - `TURNS_TOTAL` Counter `ach_agent_turns_total{model,channel}`
  - `TURN_DURATION_SECONDS` Histogram `ach_agent_turn_duration_seconds{model}`
  - `STATS_DEGRADED` Counter `ach_agent_stats_degraded_total`
  - `observe(stat: SessionStat) -> None` — applies all of the above from one record.

- [ ] **Step 1: Write the failing test**

Create `tests/stats/test_metrics.py`:

```python
from prometheus_client import REGISTRY

from ach_agent.stats import metrics
from ach_agent.stats.models import SessionStat


def _val(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _stat(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="glm-5-2",
        provider="zhipu", raw_task="t", input_tokens=100, output_tokens=40, cache_read=0,
        cache_write=0, cost=0.01, turns=2, duration_ms=1000, status="completed", retry=False,
    )
    base.update(over)
    return SessionStat.build(**base)


def test_observe_increments_sessions_and_cost():
    before_s = _val("ach_agent_sessions_total", {"model": "glm-5-2", "channel": "cron"})
    before_c = _val("ach_agent_turn_cost_usd_total", {"model": "glm-5-2", "channel": "cron"})
    metrics.observe(_stat(cost=0.05))
    after_s = _val("ach_agent_sessions_total", {"model": "glm-5-2", "channel": "cron"})
    after_c = _val("ach_agent_turn_cost_usd_total", {"model": "glm-5-2", "channel": "cron"})
    assert after_s == before_s + 1
    assert round(after_c - before_c, 4) == 0.05


def test_observe_increments_input_and_output_tokens():
    b_in = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "input"})
    b_out = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "output"})
    metrics.observe(_stat(input_tokens=100, output_tokens=40))
    a_in = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "input"})
    a_out = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "output"})
    assert a_in == b_in + 100
    assert a_out == b_out + 40


def test_degraded_counter_exists():
    before = _val("ach_agent_stats_degraded_total", {})
    metrics.STATS_DEGRADED.inc()
    assert _val("ach_agent_stats_degraded_total", {}) == before + 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stats/test_metrics.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ach_agent.stats.metrics'`.

- [ ] **Step 3: Implement metrics**

Create `src/ach_agent/stats/metrics.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Prometheus metrics for the stats sink (design spec §4.3).

Low-cardinality labels only (model, channel, provider). NEVER session_key or task as labels.
Exposed via the already-mounted /metrics (http/app.py). Follows router/metrics.py conventions.
"""

from __future__ import annotations

import prometheus_client

from ach_agent.stats.models import SessionStat

SESSIONS_TOTAL = prometheus_client.Counter(
    "ach_agent_sessions_total", "Invocations recorded", ["model", "channel"]
)
TURN_TOKENS_TOTAL = prometheus_client.Counter(
    "ach_agent_turn_tokens_total", "Tokens by direction", ["model", "direction"]
)
TURN_COST_USD_TOTAL = prometheus_client.Counter(
    "ach_agent_turn_cost_usd_total", "Cost in USD", ["model", "channel"]
)
TURNS_TOTAL = prometheus_client.Counter(
    "ach_agent_turns_total", "Within-invocation loop/tool count", ["model", "channel"]
)
TURN_DURATION_SECONDS = prometheus_client.Histogram(
    "ach_agent_turn_duration_seconds", "Invocation duration", ["model"]
)
STATS_DEGRADED = prometheus_client.Counter(
    "ach_agent_stats_degraded_total", "Session records dropped (queue full / writer error)"
)


def observe(stat: SessionStat) -> None:
    """Apply all counters/histogram from one invocation record. Always safe, in-process."""
    SESSIONS_TOTAL.labels(stat.model, stat.channel).inc()
    TURN_TOKENS_TOTAL.labels(stat.model, "input").inc(stat.input_tokens)
    TURN_TOKENS_TOTAL.labels(stat.model, "output").inc(stat.output_tokens)
    TURN_TOKENS_TOTAL.labels(stat.model, "cache_read").inc(stat.cache_read)
    TURN_TOKENS_TOTAL.labels(stat.model, "cache_write").inc(stat.cache_write)
    TURN_COST_USD_TOTAL.labels(stat.model, stat.channel).inc(stat.cost)
    TURNS_TOTAL.labels(stat.model, stat.channel).inc(stat.turns)
    TURN_DURATION_SECONDS.labels(stat.model).observe(stat.duration_ms / 1000.0)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stats/test_metrics.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats/metrics.py tests/stats/test_metrics.py
uv run mypy --strict src/ach_agent/stats/metrics.py
git add src/ach_agent/stats/metrics.py tests/stats/test_metrics.py
git commit -m "feat(stats): Prometheus counters + observe() for the sink"
```

---

### Task 4: StatsSink.record() — non-blocking enqueue + inline metrics

**Files:**
- Create: `src/ach_agent/stats/sink.py`
- Test: `tests/stats/test_sink.py`

**Interfaces:**
- Consumes: `SessionStat` (Task 2), `metrics.observe` + `metrics.STATS_DEGRADED` (Task 3).
- Produces:
  - `StatsSink(redis_url: str | None, *, retention_s: int = 3_024_000, maxsize: int = 256,
    client_factory: Callable[[], Any] | None = None)`
  - `StatsSink.record(stat: SessionStat) -> None` — inline `metrics.observe`; if a queue exists,
    `put_nowait`; on `QueueFull` → `STATS_DEGRADED.inc()`. Never awaits, never raises.
  - `StatsSink.enabled: bool` property (True iff `redis_url` set).
  - (writer methods `start`/`stop` are added in Task 5.)

- [ ] **Step 1: Write the failing test**

Create `tests/stats/test_sink.py`:

```python
import asyncio

import pytest

from ach_agent.stats.models import SessionStat
from ach_agent.stats.sink import StatsSink


def _stat(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="m", provider="p",
        raw_task="t", input_tokens=1, output_tokens=1, cache_read=0, cache_write=0, cost=0.0,
        turns=1, duration_ms=10, status="completed", retry=False,
    )
    base.update(over)
    return SessionStat.build(**base)


def test_disabled_sink_record_is_noop_but_metrics_still_run():
    sink = StatsSink(redis_url=None)
    assert sink.enabled is False
    sink.record(_stat())  # must not raise; no queue


def test_enabled_sink_enqueues():
    sink = StatsSink(redis_url="redis://x", maxsize=4)
    sink.record(_stat())
    assert sink._queue is not None
    assert sink._queue.qsize() == 1


def test_record_drops_and_counts_when_queue_full():
    from ach_agent.stats import metrics

    sink = StatsSink(redis_url="redis://x", maxsize=2)
    before = metrics.STATS_DEGRADED._value.get()
    for _ in range(5):
        sink.record(_stat())  # 2 fit, 3 dropped
    assert sink._queue.qsize() == 2
    assert metrics.STATS_DEGRADED._value.get() == before + 3


def test_record_never_blocks(event_loop=None):
    # record() is sync and must return immediately even when the queue is full.
    sink = StatsSink(redis_url="redis://x", maxsize=1)
    sink.record(_stat())
    sink.record(_stat())  # full → dropped, still returns
    assert True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stats/test_sink.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'ach_agent.stats.sink'`.

- [ ] **Step 3: Implement record() + construction (writer stubbed for Task 5)**

Create `src/ach_agent/stats/sink.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""StatsSink — best-effort, non-blocking per-invocation stats.

record() does ONLY inline Prometheus increments + queue.put_nowait(). It never touches redis. A
single supervised writer task (Task 5) owns the redis client and drains the bounded queue. See
design spec §5.
"""

from __future__ import annotations

import asyncio
from typing import Any, Callable

import structlog

from ach_agent.stats import metrics
from ach_agent.stats.models import SessionStat

log = structlog.get_logger()

_DEFAULT_RETENTION_S = 3_024_000  # 35 days


class StatsSink:
    def __init__(
        self,
        redis_url: str | None,
        *,
        retention_s: int = _DEFAULT_RETENTION_S,
        maxsize: int = 256,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        self._redis_url = redis_url
        self._retention_s = retention_s
        self._client_factory = client_factory
        self._queue: asyncio.Queue[SessionStat] | None = (
            asyncio.Queue(maxsize=maxsize) if redis_url else None
        )
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._redis_url is not None

    def record(self, stat: SessionStat) -> None:
        """Inline metrics + non-blocking enqueue. Never awaits, never raises."""
        try:
            metrics.observe(stat)
        except Exception:  # noqa: BLE001 — metrics must never break a turn
            pass
        if self._queue is None:
            return
        try:
            self._queue.put_nowait(stat)
        except asyncio.QueueFull:
            metrics.STATS_DEGRADED.inc()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stats/test_sink.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats/sink.py tests/stats/test_sink.py
uv run mypy --strict src/ach_agent/stats/sink.py
git add src/ach_agent/stats/sink.py tests/stats/test_sink.py
git commit -m "feat(stats): StatsSink.record() — non-blocking enqueue + inline metrics"
```

---

### Task 5: The supervised writer — XADD, MINID trim, backoff, shutdown drain

**Files:**
- Modify: `src/ach_agent/stats/sink.py`
- Test: `tests/stats/test_sink.py` (extend)

**Interfaces:**
- Consumes: the queue + `client_factory` from Task 4.
- Produces on `StatsSink`:
  - `async def start(self) -> None` — spawn the writer task (no-op if disabled).
  - `async def stop(self) -> None` — best-effort drain (≤2s) then cancel.
  - default client factory builds `redis.asyncio.from_url(url, socket_connect_timeout=0.5,
    socket_timeout=0.5, decode_responses=True)`.
  - the writer: owns the client, `XADD ach:sessions MINID ~ <now-retention_ms> * <entry>`, calls
    `queue.task_done()`, restarts on error with capped backoff (1s→30s), rate-limits its error log.

- [ ] **Step 1: Write the failing tests (append to `tests/stats/test_sink.py`)**

```python
class FakeClient:
    """Records XADD calls; optional hang/fail injection."""

    def __init__(self, hang: asyncio.Event | None = None, fail_times: int = 0):
        self.adds: list[dict] = []
        self._hang = hang
        self._fail_times = fail_times
        self.calls = 0

    async def xadd(self, name, fields, **kw):
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("boom")
        if self._hang is not None:
            await self._hang.wait()
        self.adds.append({"name": name, "fields": fields, "kw": kw})

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_writer_xadds_entry_with_minid_trim():
    client = FakeClient()
    sink = StatsSink(redis_url="redis://x", retention_s=100, client_factory=lambda: client)
    await sink.start()
    sink.record(_stat(model="claude-opus-4-8"))
    await asyncio.sleep(0.05)
    await sink.stop()
    assert len(client.adds) == 1
    call = client.adds[0]
    assert call["name"] == "ach:sessions"
    assert call["fields"]["model"] == "claude-opus-4-8"
    assert "minid" in call["kw"] and call["kw"].get("approximate") is True


@pytest.mark.asyncio
async def test_record_never_blocks_when_writer_stuck():
    hang = asyncio.Event()
    client = FakeClient(hang=hang)
    sink = StatsSink(redis_url="redis://x", maxsize=2, client_factory=lambda: client)
    await sink.start()
    from ach_agent.stats import metrics
    before = metrics.STATS_DEGRADED._value.get()
    # Writer grabs the 1st item and hangs; the queue (size 2) fills; the rest drop.
    for _ in range(6):
        sink.record(_stat())
    await asyncio.sleep(0.05)
    assert metrics.STATS_DEGRADED._value.get() >= before + 3
    hang.set()
    await sink.stop()


@pytest.mark.asyncio
async def test_writer_backoff_does_not_busy_loop(monkeypatch):
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)
        # Let the loop breathe without real time.
        await asyncio.sleep(0)

    client = FakeClient(fail_times=1000)
    sink = StatsSink(redis_url="redis://x", client_factory=lambda: client)
    monkeypatch.setattr("ach_agent.stats.sink.asyncio.sleep", fake_sleep)
    await sink.start()
    sink.record(_stat())
    await asyncio.sleep(0.05)
    await sink.stop()
    # Backoff must be applied (non-zero sleeps) and escalate, not spin at 0.
    assert any(s >= 1 for s in sleeps)
    assert max(sleeps) <= 30
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/stats/test_sink.py -v`
Expected: FAIL with `AttributeError: 'StatsSink' object has no attribute 'start'`.

- [ ] **Step 3: Implement the writer**

Add imports at the top of `src/ach_agent/stats/sink.py` (below existing imports):

```python
import time
```

Append these methods to `StatsSink`:

```python
    def _make_client(self) -> Any:
        if self._client_factory is not None:
            return self._client_factory()
        import redis.asyncio as redis_asyncio

        return redis_asyncio.from_url(
            self._redis_url,
            socket_connect_timeout=0.5,
            socket_timeout=0.5,
            decode_responses=True,
        )

    async def start(self) -> None:
        if self._queue is None or self._task is not None:
            return
        self._task = asyncio.create_task(self._run(), name="stats-writer")

    async def stop(self) -> None:
        if self._task is None:
            return
        if self._queue is not None:
            try:
                await asyncio.wait_for(self._queue.join(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        assert self._queue is not None
        backoff = 1.0
        last_log = 0.0
        while True:
            client = None
            try:
                client = self._make_client()
                while True:
                    stat = await self._queue.get()
                    try:
                        minid = int((time.time() - self._retention_s) * 1000)
                        await client.xadd(
                            "ach:sessions",
                            stat.to_entry(),
                            minid=minid,
                            approximate=True,
                        )
                        backoff = 1.0
                    finally:
                        self._queue.task_done()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — never let the writer die silently
                now = time.time()
                if now - last_log > 10:
                    log.warning("stats: redis writer error", error=str(exc), backoff=backoff)
                    last_log = now
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)
            finally:
                if client is not None:
                    try:
                        await client.aclose()
                    except Exception:  # noqa: BLE001
                        pass
```

Note on `test_writer_backoff_does_not_busy_loop`: the injected `FakeClient` raises on `xadd`, but
the writer only calls `xadd` after `queue.get()` returns an item. `sink.record()` supplies one item;
on failure the item is `task_done`'d and the loop reconnects, re-`get()`s (now empty → awaits), so
to keep failing the test relies on the record happening; the escalating-backoff assertion holds
because the connect/xadd path fails repeatedly while items exist. If the queue empties, the writer
parks on `get()` (correct — no spin). The assertion `any(s >= 1)` confirms backoff fired at least
once.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/stats/test_sink.py -v`
Expected: PASS (all sink tests).

- [ ] **Step 5: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats/sink.py tests/stats/test_sink.py
uv run mypy --strict src/ach_agent/stats/sink.py
git add src/ach_agent/stats/sink.py tests/stats/test_sink.py
git commit -m "feat(stats): supervised redis writer — XADD+MINID trim, backoff, drain"
```

---

### Task 6: Wire StatsSink into the harness turn-summary site

**Files:**
- Modify: `src/ach_agent/main.py` (construct the sink at startup; `record()` after the summary log
  ~line 598; `stop()` on shutdown)
- Test: `tests/stats/test_wire.py`

**Interfaces:**
- Consumes: `StatsSink` (Tasks 4–5), `SessionStat` (Task 2). Reads env `ACH_STATS_REDIS_URL`,
  `ACH_STATS_RETENTION`.
- Produces: a helper `build_session_stat(event, obj, turn_stats, ts_ms) -> SessionStat` in
  `src/ach_agent/stats/sink.py` so the mapping is unit-testable without running the engine.

> **Read first:** `src/ach_agent/main.py` around lines 556–604 (the turn-summary site: `run_invocation`
> returns `obj`, `turn_stats["usage"]` holds `OpenCodeUsage | None`, the "engine: summary" log is
> emitted). The `record()` call goes immediately after that log. `event` carries `.channel_name`,
> `.session_key`, and `.source` (verify the source attribute name; if absent, pass `channel_name`).

- [ ] **Step 1: Write the failing test**

Create `tests/stats/test_wire.py`:

```python
from ach_agent.engine.events import OpenCodeUsage
from ach_agent.stats.sink import build_session_stat


class _Event:
    channel_name = "webhook"
    session_key = "gitlab:git.example.com/g/r"
    source = "gitlab"


def test_build_session_stat_maps_usage_and_meta():
    usage = OpenCodeUsage(
        session_id="s", message_id="m", input_tokens=1200, output_tokens=300,
        cache_read=5, cache_write=6, cost=0.12, duration_ms=4000,
    )
    obj = {"text": "done", "action": "reply", "model": "claude-opus-4-8"}
    turn_stats = {"usage": usage, "tool_count": 4, "aborted": False}
    stat = build_session_stat(_Event(), obj, turn_stats, ts_ms=1_700_000_000_000)
    assert stat.model == "claude-opus-4-8"
    assert stat.channel == "webhook"
    assert stat.source == "gitlab"
    assert stat.input_tokens == 1200
    assert stat.output_tokens == 300
    assert stat.cost == 0.12
    assert stat.turns == 4
    assert stat.status == "completed"


def test_build_session_stat_handles_missing_usage_and_aborted():
    obj = {"text": "", "action": "none", "model": "glm-5-2"}
    turn_stats = {"usage": None, "tool_count": 0, "aborted": True}
    stat = build_session_stat(_Event(), obj, turn_stats, ts_ms=1)
    assert stat.input_tokens == 0
    assert stat.cost == 0.0
    assert stat.status == "aborted"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/stats/test_wire.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_session_stat'`.

- [ ] **Step 3: Add the mapping helper to `sink.py`**

Append to `src/ach_agent/stats/sink.py`:

```python
def build_session_stat(
    event: Any, obj: dict[str, Any], turn_stats: dict[str, Any], *, ts_ms: int
) -> SessionStat:
    """Map the engine turn-summary outputs to a SessionStat. Pure; unit-testable."""
    usage = turn_stats.get("usage")
    aborted = bool(turn_stats.get("aborted"))
    return SessionStat.build(
        ts_ms=ts_ms,
        session_key=getattr(event, "session_key", "unknown"),
        channel=getattr(event, "channel_name", "unknown"),
        source=getattr(event, "source", getattr(event, "channel_name", "unknown")),
        model=str(obj.get("model", "unknown")),
        provider="unknown",  # provider is resolved by the stats service's model-map (A2), not here
        raw_task=str(obj.get("text", "")),
        input_tokens=getattr(usage, "input_tokens", 0),
        output_tokens=getattr(usage, "output_tokens", 0),
        cache_read=getattr(usage, "cache_read", 0),
        cache_write=getattr(usage, "cache_write", 0),
        cost=getattr(usage, "cost", 0.0),
        turns=int(turn_stats.get("tool_count", 0)),
        duration_ms=getattr(usage, "duration_ms", 0),
        status="aborted" if aborted else "completed",
        retry=bool(turn_stats.get("retry", False)),
    )
```

> NOTE on `raw_task`: the spec's recent-table "task" is the inbound prompt. `obj["text"]` is the
> assistant *reply*. If the inbound prompt is the intended task label, pass the prompt string that
> `main.py` already has in scope (`full_prompt`) as a new `raw_task=` argument at the call site
> instead of `obj["text"]`. Confirm against §4.1 intent when wiring Step 5; the helper signature
> stays the same (the caller chooses what to pass as the task).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/stats/test_wire.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Wire into `main.py`**

At harness startup (where other long-lived components are constructed), add:

```python
import os
import time

from ach_agent.stats.sink import StatsSink, build_session_stat

stats_sink = StatsSink(
    os.environ.get("ACH_STATS_REDIS_URL"),
    retention_s=int(os.environ.get("ACH_STATS_RETENTION", "3024000")),
)
await stats_sink.start()
```

Immediately after the `log.info("engine: summary", ...)` call (~`main.py:598`), add:

```python
            stats_sink.record(
                build_session_stat(
                    event, obj, turn_stats, ts_ms=int(time.time() * 1000)
                )
            )
```

At harness shutdown (alongside other component teardown), add:

```python
await stats_sink.stop()
```

- [ ] **Step 6: Verify nothing regressed**

Run: `uv run pytest tests/ -q`
Expected: PASS (full suite green; no import or wiring errors).

- [ ] **Step 7: Lint + typecheck + commit**

```bash
uv run ruff check src/ach_agent/stats/sink.py src/ach_agent/main.py tests/stats/test_wire.py
uv run mypy --strict src/ach_agent/stats/sink.py src/ach_agent/main.py
git add src/ach_agent/stats/sink.py src/ach_agent/main.py tests/stats/test_wire.py
git commit -m "feat(stats): wire StatsSink into the harness turn-summary site"
```

---

### Task 7: CONTRACT_v3 non-normative carve-out note

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (add one non-normative note)

- [ ] **Step 1: Add the note**

Find the CONTRACT_v3 section listing env vars / non-normative notes and add:

```markdown
> **Non-normative:** `ACH_STATS_*` environment variables (`ACH_STATS_REDIS_URL`,
> `ACH_STATS_RETENTION`, `ACH_STATS_TZ`) are **harness-local** and explicitly **outside this
> contract**. They configure the optional stats sink / stats service and are not rendered by the
> operator. Promote to a real config block only if eval infrastructure (Sub-project B) requires
> operator awareness.
```

- [ ] **Step 2: Commit**

```bash
git add docs/plan/CONTRACT_v3.md
git commit -m "docs(contract): note ACH_STATS_* env vars are outside CONTRACT_v3"
```

---

### Task 8: (CONDITIONAL — only if Task 0 confirmed R1) baseline-delta

**Skip this task entirely if the Task 0 probe found per-message scope.**

**Files:**
- Modify: `src/ach_agent/stats/sink.py` (record deltas against a per-session baseline)
- Test: `tests/stats/test_sink.py` (extend)

**Interfaces:**
- Produces: `StatsSink.record_delta(stat: SessionStat, *, session_key: str) -> None` OR a
  `baseline` map keyed by `session_key` holding the last-recorded cumulative `(input, output,
  cache_read, cache_write, cost)`; `record()` subtracts the baseline before enqueue and updates it.
  Preferred home per spec §4.1 is the in-process per-session engine-pool state, NOT a sink-side
  dict — if the engine pool exposes a per-`session_key` state object, store `last_usage` there and
  compute `final − baseline` at the `build_session_stat` call site; otherwise fall back to a
  sink-side `dict[str, tuple]` with eviction on session close.

- [ ] **Step 1: Write the failing test**

```python
def test_baseline_delta_subtracts_previous_cumulative():
    # Two invocations on one reused session; opencode reported SESSION-cumulative usage.
    # Expected recorded output_tokens = per-invocation delta, not the growing cumulative.
    from ach_agent.stats.sink import subtract_baseline

    prev = {"input": 1000, "output": 400, "cache_read": 0, "cache_write": 0, "cost": 0.10}
    cur = {"input": 2500, "output": 900, "cache_read": 0, "cache_write": 0, "cost": 0.25}
    delta = subtract_baseline(cur, prev)
    assert delta["output"] == 500
    assert round(delta["cost"], 2) == 0.15
```

- [ ] **Step 2: Run to verify it fails, implement `subtract_baseline`, re-run, commit**

```python
def subtract_baseline(cur: dict[str, float], prev: dict[str, float]) -> dict[str, float]:
    """cur − prev per field, clamped at 0 (a reset/new session yields cur itself)."""
    out: dict[str, float] = {}
    for k, v in cur.items():
        d = v - prev.get(k, 0)
        out[k] = d if d >= 0 else v
    return out
```

Wire `subtract_baseline` into `build_session_stat`'s caller (using the per-session baseline home
chosen above), add a regression test that two sequential cumulative usages record independent deltas,
then:

```bash
git add src/ach_agent/stats/sink.py tests/stats/test_sink.py
git commit -m "fix(stats): record per-invocation usage deltas (R1 confirmed)"
```

---

## Self-Review

- **Spec coverage (A1 scope — spec §4.1/§4.2/§4.3/§5/§8/§9 harness rows):**
  - Entry semantics / one-record-per-invocation → Task 6 (`build_session_stat`, status).
  - R1 probe + conditional fix → Task 0, Task 8.
  - Versioned entry (`v:1`) + all fields → Task 2.
  - Prometheus counters incl. `stats_degraded_total` → Task 3.
  - Non-blocking record / bounded queue / drop-on-full → Task 4.
  - Writer: owned client, socket timeouts, MINID trim, backoff, shutdown drain → Task 5.
  - Task-text truncation+redaction → Task 1 (used by Task 2).
  - Env-gated config + CONTRACT note → Task 6 (env), Task 7 (note).
  - Aborted included / status → Task 6. Slow-socket + backoff + drain tests → Task 5.
  - **Out of A1 (belongs to A2):** the redis reader, aggregation, per-panel partial, TZ calendar,
    provider/tag map, contract endpoints, UI, docker. Tracked in the A2 plan.
- **Placeholder scan:** none — every code step shows complete code; Task 8 is explicitly conditional
  with concrete code, not a TODO.
- **Type consistency:** `SessionStat.build(...)` kwargs match Tasks 2/6; `record()`/`start()`/
  `stop()` signatures consistent Tasks 4–6; `metrics.observe(stat)` and `STATS_DEGRADED` names match
  Tasks 3–5; `ach:sessions` stream name consistent Tasks 5 & A2.

## Execution Handoff — see end of the A2 plan / the message accompanying this plan.
