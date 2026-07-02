# Stats Container (Service + UI) — Implementation Plan (Sub-project A2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A standalone `ach-stats` container that reads the harness's `ach:sessions` redis stream,
aggregates it into a page-ready leaderboard contract, and serves a lifted React dashboard.

**Architecture:** FastAPI service (`src/ach_stats/api`) with a tolerant redis **reader**, a pure
**aggregate** module (rows → contract), and routes that serve both JSON and the built SPA. The UI
(`src/ach_stats/ui`) lifts alitellm-auth's shadcn/TanStack-Query design system and adds a
`Leaderboard.tsx`. Multi-stage Docker builds the UI then serves it from the API.

**Tech Stack:** Python 3.12 + FastAPI + Uvicorn + `redis.asyncio` + Pydantic v2; React + Vite +
TypeScript + Tailwind + shadcn/ui + TanStack Query + Recharts; pytest + `fakeredis`; vitest.

**Design source:** `docs/superpowers/specs/2026-07-02-stats-leaderboard-container-design.md`
(§4.2 reader/entry, §4.4 contract, §6 service+UI, §7 deploy). **Depends on A1** (the entry schema
`v:1` and `ach:sessions` stream). **Donor:** `../alitellm-auth` (`src/api/app/stats.py`,
`src/ui/`).

## Global Constraints

- Python 3.12; `mypy --strict` + `ruff` clean. React strict TS; `eslint` clean; vitest green.
- The stats service is an **independent container** with its **own** `pyproject.toml` — it does NOT
  import `ach_agent.*`. The only shared artifact is the redis entry schema (`v:1`, §4.2), which the
  reader treats as a **tolerant contract**: missing field → documented default; unknown field →
  ignored; dispatch on `v`.
- **UI is a dumb renderer.** The server shapes the full page-ready contract; the UI never computes
  business figures. Preserve the donor's `null`-vs-`0` discipline (an unavailable figure is `null`;
  a genuine zero stays `0`).
- `score` is **nullable** in the leaderboard (A2 always emits `null`; Sub-project B fills it).
  `leaderboard.sorted_by` is explicit (`"spend"` in A2). Null-score rows render **"unrated"**.
- Retention window is 35d; the contract exposes `coverage_start` + per-panel `partial`. Calendar
  month boundaries use `ACH_STATS_TZ` (default `UTC`; deploy sets `Europe/Madrid`).
- Docker multi-stage, explicit `COPY` paths only, `.dockerignore`. Never `COPY . .`.
- Tests: `src/ach_stats/api/tests/` (pytest) and colocated `*.test.tsx` (vitest).

---

## File Structure

- `src/ach_stats/api/pyproject.toml` — independent service deps.
- `src/ach_stats/api/app/__init__.py`
- `src/ach_stats/api/app/model_meta.py` — static model → (provider, tag) map + `resolve()`.
- `src/ach_stats/api/app/reader.py` — tolerant entry parse + `read_window`/`read_recent`/
  `read_coverage_start`.
- `src/ach_stats/api/app/aggregate.py` — pure `build_contract(...)`.
- `src/ach_stats/api/app/main.py` — FastAPI app: `/api/leaderboard`, `/api/sessions`, `/healthz`,
  SPA static mount.
- `src/ach_stats/api/tests/` — `test_model_meta.py`, `test_reader.py`, `test_aggregate.py`,
  `test_routes.py`.
- `src/ach_stats/ui/` — lifted React app (Vite/TS/Tailwind/shadcn) + `routes/Leaderboard.tsx`,
  `hooks/use-leaderboard.ts`, `lib/api-types.ts`.
- `docker/stats.Dockerfile`, `docker/.dockerignore.stats`.
- `docker-compose.dev.yml` — add `redis` + `ach-stats`.

---

### Task 1: Scaffold the FastAPI service (healthz)

**Files:**
- Create: `src/ach_stats/api/pyproject.toml`, `src/ach_stats/api/app/__init__.py`,
  `src/ach_stats/api/app/main.py`
- Test: `src/ach_stats/api/tests/__init__.py`, `src/ach_stats/api/tests/test_routes.py`

**Interfaces:**
- Produces: `create_app() -> FastAPI` with `GET /healthz` → `{"status": "ok"}`.

- [ ] **Step 1: Create the package manifest**

`src/ach_stats/api/pyproject.toml`:

```toml
[project]
name = "ach-stats"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
    "fastapi==0.116.1",
    "uvicorn[standard]==0.34.0",
    "redis>=5,<6",
    "pydantic>=2,<3",
]

[dependency-groups]
dev = ["pytest==9.1.1", "pytest-asyncio==1.4.0", "httpx>=0.28", "fakeredis>=2.26,<3"]

[tool.pytest.ini_options]
asyncio_mode = "auto"

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

- [ ] **Step 2: Write the failing test**

`src/ach_stats/api/tests/__init__.py` (empty). `src/ach_stats/api/tests/test_routes.py`:

```python
from fastapi.testclient import TestClient

from app.main import create_app


def test_healthz():
    client = TestClient(create_app())
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_routes.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app'`.

- [ ] **Step 4: Implement**

`src/ach_stats/api/app/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""ach-stats: reads the harness ach:sessions stream and serves the leaderboard dashboard."""
```

`src/ach_stats/api/app/main.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""FastAPI app: leaderboard/sessions JSON + the built SPA. See design spec §6."""

from __future__ import annotations

from fastapi import FastAPI


def create_app() -> FastAPI:
    app = FastAPI(title="ach-stats")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
```

- [ ] **Step 5: Run to verify it passes, then commit**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_routes.py -v` → PASS.

```bash
git add src/ach_stats/api/pyproject.toml src/ach_stats/api/app tests_placeholder 2>/dev/null; \
git add src/ach_stats/api
git commit -m "feat(stats-svc): scaffold ach-stats FastAPI service (healthz)"
```

---

### Task 2: Model-metadata map (provider + tag)

**Files:**
- Create: `src/ach_stats/api/app/model_meta.py`
- Test: `src/ach_stats/api/tests/test_model_meta.py`

**Interfaces:**
- Produces: `resolve(model: str) -> tuple[str, str | None]` returning `(provider, tag)`; unknown
  model → `("unknown", None)`.

- [ ] **Step 1: Write the failing test**

`src/ach_stats/api/tests/test_model_meta.py`:

```python
from app.model_meta import resolve


def test_known_models():
    assert resolve("claude-opus-4-8") == ("Anthropic", "Frontier")
    assert resolve("claude-sonnet-5") == ("Anthropic", "Balanced")
    assert resolve("glm-5-2") == ("Zhipu AI", "Open Weight")


def test_unknown_model():
    assert resolve("mystery-model-9") == ("unknown", None)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_model_meta.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.model_meta'`.

- [ ] **Step 3: Implement**

`src/ach_stats/api/app/model_meta.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Static model -> (provider, tag) map. provider/tag are metadata, never measured (spec §4.4)."""

from __future__ import annotations

_META: dict[str, tuple[str, str | None]] = {
    "claude-opus-4-8": ("Anthropic", "Frontier"),
    "claude-fable-5": ("Anthropic", "Mythos-tier"),
    "claude-sonnet-5": ("Anthropic", "Balanced"),
    "glm-5-2": ("Zhipu AI", "Open Weight"),
}


def resolve(model: str) -> tuple[str, str | None]:
    return _META.get(model, ("unknown", None))
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `... pytest tests/test_model_meta.py -v` → PASS.

```bash
git add src/ach_stats/api/app/model_meta.py src/ach_stats/api/tests/test_model_meta.py
git commit -m "feat(stats-svc): static model-metadata map (provider/tag)"
```

---

### Task 3: Tolerant redis reader

**Files:**
- Create: `src/ach_stats/api/app/reader.py`
- Test: `src/ach_stats/api/tests/test_reader.py`

**Interfaces:**
- Produces:
  - `parse_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]` — tolerant: missing →
    typed default, unknown → ignored, `v` dispatched (only `"1"` known; else best-effort defaults).
    `ts_ms` derived from `fields["ts"]` if present else from the stream id's ms part.
  - `async def read_window(client, start_ms: int, end_ms: int) -> list[dict]` — `XRANGE`.
  - `async def read_recent(client, n: int) -> list[dict]` — `XREVRANGE COUNT n` (newest first).
  - `async def read_coverage_start(client) -> int | None` — ms ts of the oldest entry, or None.

- [ ] **Step 1: Write the failing test**

`src/ach_stats/api/tests/test_reader.py`:

```python
import fakeredis.aioredis
import pytest

from app.reader import parse_entry, read_coverage_start, read_recent, read_window


def test_parse_entry_v1_typed():
    fields = {
        "v": "1", "ts": "1700000000000", "session_key": "k", "channel": "webhook",
        "source": "gitlab", "model": "claude-opus-4-8", "provider": "unknown", "task": "Review !7",
        "input_tokens": "1200", "output_tokens": "300", "cache_read": "5", "cache_write": "6",
        "cost": "0.12", "turns": "4", "duration_ms": "4000", "tokens_per_s": "75.0",
        "status": "completed", "retry": "false",
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
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_reader.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.reader'`.

- [ ] **Step 3: Implement**

`src/ach_stats/api/app/reader.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Tolerant reader for the ach:sessions stream. The entry schema (v:1) is a cross-component
contract: missing field -> typed default, unknown field -> ignored, dispatch on `v` (spec §4.2)."""

from __future__ import annotations

from typing import Any

_STREAM = "ach:sessions"


def _int(fields: dict[str, str], key: str, default: int = 0) -> int:
    try:
        return int(fields[key])
    except (KeyError, ValueError):
        return default


def _float(fields: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        return float(fields[key])
    except (KeyError, ValueError):
        return default


def parse_entry(entry_id: str, fields: dict[str, str]) -> dict[str, Any]:
    """Typed, tolerant projection of one stream entry. `v` dispatch is forward-compatible."""
    ts_ms = _int(fields, "ts", default=int(entry_id.split("-")[0]))
    return {
        "v": fields.get("v", "1"),
        "ts_ms": ts_ms,
        "session_key": fields.get("session_key", "unknown"),
        "channel": fields.get("channel", "unknown"),
        "source": fields.get("source", "unknown"),
        "model": fields.get("model", "unknown"),
        "task": fields.get("task", ""),
        "input_tokens": _int(fields, "input_tokens"),
        "output_tokens": _int(fields, "output_tokens"),
        "cache_read": _int(fields, "cache_read"),
        "cache_write": _int(fields, "cache_write"),
        "cost": _float(fields, "cost"),
        "turns": _int(fields, "turns"),
        "duration_ms": _int(fields, "duration_ms"),
        "tokens_per_s": _float(fields, "tokens_per_s"),
        "status": fields.get("status", "unknown"),
        "retry": fields.get("retry", "false") == "true",
    }


async def read_window(client: Any, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
    raw = await client.xrange(_STREAM, min=f"{start_ms}", max=f"{end_ms}")
    return [parse_entry(eid, fields) for eid, fields in raw]


async def read_recent(client: Any, n: int) -> list[dict[str, Any]]:
    raw = await client.xrevrange(_STREAM, count=n)
    return [parse_entry(eid, fields) for eid, fields in raw]


async def read_coverage_start(client: Any) -> int | None:
    raw = await client.xrange(_STREAM, count=1)
    if not raw:
        return None
    return parse_entry(raw[0][0], raw[0][1])["ts_ms"]
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `... pytest tests/test_reader.py -v` → PASS (4 passed).

```bash
git add src/ach_stats/api/app/reader.py src/ach_stats/api/tests/test_reader.py
git commit -m "feat(stats-svc): tolerant redis reader (v-dispatch, window/recent/coverage)"
```

---

### Task 4: Aggregate — totals + leaderboard rows

**Files:**
- Create: `src/ach_stats/api/app/aggregate.py`
- Test: `src/ach_stats/api/tests/test_aggregate.py`

**Interfaces:**
- Consumes: `model_meta.resolve` (Task 2), parsed rows (Task 3).
- Produces: `build_leaderboard(rows: list[dict]) -> dict` → `{"sorted_by": "spend", "rows": [...]}`
  where each row = `{rank, model, provider, score, speed_tok_s, cost_per_mtok, spend, sessions,
  tag}`; `score` always `None`; rows sorted by `spend` desc; `rank` is 1-based.
- Produces: `build_totals(rows) -> dict` → `{sessions, tokens, spend, avg_cost_per_session,
  aborted}` (aborted counted separately but INCLUDED in sessions/spend).

- [ ] **Step 1: Write the failing test**

`src/ach_stats/api/tests/test_aggregate.py`:

```python
from app.aggregate import build_leaderboard, build_totals


def _row(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="glm-5-2", task="t",
        input_tokens=100, output_tokens=50, cache_read=0, cache_write=0, cost=0.01, turns=1,
        duration_ms=1000, tokens_per_s=50.0, status="completed", retry=False,
    )
    base.update(over)
    return base


def test_totals_sum_and_count_include_aborted():
    rows = [
        _row(cost=0.10, input_tokens=100, output_tokens=50),
        _row(cost=0.20, input_tokens=200, output_tokens=100, status="aborted"),
    ]
    t = build_totals(rows)
    assert t["sessions"] == 2
    assert round(t["spend"], 2) == 0.30
    assert t["tokens"] == 450  # (100+50)+(200+100)
    assert t["aborted"] == 1
    assert round(t["avg_cost_per_session"], 3) == 0.15


def test_totals_empty_guards_denominator():
    t = build_totals([])
    assert t["sessions"] == 0
    assert t["avg_cost_per_session"] is None  # null, not 0/0


def test_leaderboard_groups_by_model_sorted_by_spend_desc():
    rows = [
        _row(model="glm-5-2", cost=0.10, output_tokens=100, duration_ms=1000),
        _row(model="claude-opus-4-8", cost=0.90, output_tokens=100, duration_ms=1000),
        _row(model="glm-5-2", cost=0.10, output_tokens=100, duration_ms=1000),
    ]
    lb = build_leaderboard(rows)
    assert lb["sorted_by"] == "spend"
    assert lb["rows"][0]["model"] == "claude-opus-4-8"
    assert lb["rows"][0]["rank"] == 1
    assert lb["rows"][1]["model"] == "glm-5-2"
    assert lb["rows"][1]["sessions"] == 2
    assert lb["rows"][1]["spend"] == 0.20
    assert lb["rows"][0]["provider"] == "Anthropic"
    assert lb["rows"][0]["tag"] == "Frontier"
    assert lb["rows"][0]["score"] is None  # eval seam, filled by B


def test_leaderboard_derived_fields():
    rows = [_row(model="glm-5-2", cost=0.50, output_tokens=1000, duration_ms=2000,
                 input_tokens=1000)]
    r = build_leaderboard(rows)["rows"][0]
    assert r["speed_tok_s"] == 500.0            # 1000 tok / 2 s
    assert r["cost_per_mtok"] == 250.0          # 0.50 / (2000 tokens / 1e6)
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_aggregate.py -v`
Expected: FAIL `ModuleNotFoundError: No module named 'app.aggregate'`.

- [ ] **Step 3: Implement**

`src/ach_stats/api/app/aggregate.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Pure aggregation: parsed rows -> page-ready leaderboard contract (spec §4.4).

null-vs-0 discipline: an UNAVAILABLE figure (0 denominator) is None; a genuine zero stays 0.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from app.model_meta import resolve


def _safe_div(n: float, d: float) -> float | None:
    return (n / d) if d else None


def build_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sessions = len(rows)
    spend = sum(r["cost"] for r in rows)
    tokens = sum(r["input_tokens"] + r["output_tokens"] for r in rows)
    aborted = sum(1 for r in rows if r["status"] == "aborted")
    return {
        "sessions": sessions,
        "tokens": tokens,
        "spend": spend,
        "aborted": aborted,
        "avg_cost_per_session": _safe_div(spend, sessions),
    }


def build_leaderboard(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_model: dict[str, dict[str, float]] = defaultdict(
        lambda: {"spend": 0.0, "sessions": 0.0, "output_tokens": 0.0, "duration_ms": 0.0,
                 "total_tokens": 0.0}
    )
    for r in rows:
        m = by_model[r["model"]]
        m["spend"] += r["cost"]
        m["sessions"] += 1
        m["output_tokens"] += r["output_tokens"]
        m["duration_ms"] += r["duration_ms"]
        m["total_tokens"] += r["input_tokens"] + r["output_tokens"]

    out: list[dict[str, Any]] = []
    for model, m in by_model.items():
        provider, tag = resolve(model)
        speed = _safe_div(m["output_tokens"], m["duration_ms"] / 1000.0)
        cost_per_mtok = _safe_div(m["spend"], m["total_tokens"] / 1_000_000.0)
        out.append({
            "rank": 0,  # assigned after sort
            "model": model,
            "provider": provider,
            "tag": tag,
            "score": None,  # eval seam — Sub-project B fills this
            "speed_tok_s": speed,
            "cost_per_mtok": cost_per_mtok,
            "spend": m["spend"],
            "sessions": int(m["sessions"]),
        })

    out.sort(key=lambda r: r["spend"], reverse=True)
    for i, r in enumerate(out, start=1):
        r["rank"] = i
    return {"sorted_by": "spend", "rows": out}
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `... pytest tests/test_aggregate.py -v` → PASS (4 passed).

```bash
git add src/ach_stats/api/app/aggregate.py src/ach_stats/api/tests/test_aggregate.py
git commit -m "feat(stats-svc): aggregate totals + spend-ranked leaderboard rows"
```

---

### Task 5: Aggregate — windows, per-panel partial, TZ calendar, series, contract

**Files:**
- Modify: `src/ach_stats/api/app/aggregate.py`
- Test: `src/ach_stats/api/tests/test_aggregate.py` (extend)

**Interfaces:**
- Produces: `build_contract(*, window_rows, recent_rows, coverage_start_ms, now_ms, tz,
  range_start_ms, range_end_ms) -> dict` → the full §4.4 contract:
  `range{start,end,days,coverage_start,tz}`, `totals{...,partial}`, `leaderboard{...}`,
  `cost_per_session[]`, `sessions_this_month{rows,partial}`, `series[]`, `recent[]`.
- Produces: `month_start_ms(now_ms, tz) -> int` — first instant of the current calendar month in
  `tz`, as epoch ms.

- [ ] **Step 1: Write the failing test (append)**

```python
from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from app.aggregate import build_contract, month_start_ms


def test_month_start_respects_tz():
    # 2026-03-01 00:30 UTC is still Feb in... no — pick a clear case:
    # 2026-03-01 00:30 Madrid (UTC+1) == 2026-02-28 23:30 UTC. month_start in Madrid = Mar 1 00:00 CET.
    now = int(datetime(2026, 3, 1, 0, 30, tzinfo=ZoneInfo("Europe/Madrid")).timestamp() * 1000)
    ms = month_start_ms(now, "Europe/Madrid")
    got = datetime.fromtimestamp(ms / 1000, tz=ZoneInfo("Europe/Madrid"))
    assert (got.year, got.month, got.day, got.hour) == (2026, 3, 1, 0)


def test_contract_partial_flags_when_coverage_after_window_start():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[_row(cost=0.1)], recent_rows=[_row(cost=0.1)],
        coverage_start_ms=1_999_999_999_999,  # later than range_start -> partial
        now_ms=now, tz="UTC",
        range_start_ms=1_000_000_000_000, range_end_ms=now,
    )
    assert contract["totals"]["partial"] is True
    assert contract["range"]["coverage_start"] == 1_999_999_999_999
    assert contract["range"]["tz"] == "UTC"


def test_contract_not_partial_when_full_coverage():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[_row(cost=0.1)], recent_rows=[_row(cost=0.1)],
        coverage_start_ms=500_000_000_000,  # earlier than range_start -> complete
        now_ms=now, tz="UTC",
        range_start_ms=1_000_000_000_000, range_end_ms=now,
    )
    assert contract["totals"]["partial"] is False


def test_contract_recent_shape():
    now = 2_000_000_000_000
    contract = build_contract(
        window_rows=[], recent_rows=[_row(task="Review !7", status="aborted", retry=True)],
        coverage_start_ms=None, now_ms=now, tz="UTC",
        range_start_ms=1_000_000_000_000, range_end_ms=now,
    )
    rec = contract["recent"][0]
    assert rec["task"] == "Review !7"
    assert rec["status"] == "aborted"
    assert rec["retry"] is True
```

- [ ] **Step 2: Run to verify it fails**

Run: `... pytest tests/test_aggregate.py -k contract -v`
Expected: FAIL `ImportError: cannot import name 'build_contract'`.

- [ ] **Step 3: Implement (append to `aggregate.py`)**

```python
from datetime import datetime
from zoneinfo import ZoneInfo


def month_start_ms(now_ms: int, tz: str) -> int:
    zone = ZoneInfo(tz)
    now = datetime.fromtimestamp(now_ms / 1000, tz=zone)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return int(start.timestamp() * 1000)


def _day_key(ts_ms: int, tz: str) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz)).strftime("%Y-%m-%d")


def build_contract(
    *,
    window_rows: list[dict[str, Any]],
    recent_rows: list[dict[str, Any]],
    coverage_start_ms: int | None,
    now_ms: int,
    tz: str,
    range_start_ms: int,
    range_end_ms: int,
) -> dict[str, Any]:
    partial = coverage_start_ms is not None and coverage_start_ms > range_start_ms

    totals = build_totals(window_rows)
    totals["partial"] = partial

    leaderboard = build_leaderboard(window_rows)

    # cost per session by model (avg cost per invocation).
    cps: list[dict[str, Any]] = []
    for row in leaderboard["rows"]:
        avg = _safe_div(row["spend"], row["sessions"])
        cps.append({"model": row["model"], "avg": avg})

    # calendar month-to-date, in tz.
    m_start = month_start_ms(now_ms, tz)
    month_rows = [r for r in window_rows if r["ts_ms"] >= m_start]
    month_counts: dict[str, int] = defaultdict(int)
    for r in month_rows:
        month_counts[r["model"]] += 1
    sessions_this_month = {
        "rows": [{"model": m, "count": c} for m, c in
                 sorted(month_counts.items(), key=lambda kv: kv[1], reverse=True)],
        "partial": coverage_start_ms is not None and coverage_start_ms > m_start,
    }

    # daily series.
    day_acc: dict[str, dict[str, float]] = defaultdict(
        lambda: {"spend": 0.0, "sessions": 0.0, "tokens": 0.0}
    )
    for r in window_rows:
        d = day_acc[_day_key(r["ts_ms"], tz)]
        d["spend"] += r["cost"]
        d["sessions"] += 1
        d["tokens"] += r["input_tokens"] + r["output_tokens"]
    series = [
        {"date": day, "spend": v["spend"], "sessions": int(v["sessions"]),
         "tokens": int(v["tokens"]),
         "partial": coverage_start_ms is not None
         and coverage_start_ms > int(datetime.strptime(day, "%Y-%m-%d")
                                     .replace(tzinfo=ZoneInfo(tz)).timestamp() * 1000)}
        for day, v in sorted(day_acc.items())
    ]

    recent = [
        {"ts": r["ts_ms"], "task": r["task"], "model": r["model"],
         "tokens": r["input_tokens"] + r["output_tokens"], "cost": r["cost"],
         "turns": r["turns"], "status": r["status"], "retry": r["retry"]}
        for r in recent_rows
    ]

    days = max(1, round((range_end_ms - range_start_ms) / 86_400_000))
    return {
        "range": {"start": range_start_ms, "end": range_end_ms, "days": days,
                  "coverage_start": coverage_start_ms, "tz": tz},
        "totals": totals,
        "leaderboard": leaderboard,
        "cost_per_session": cps,
        "sessions_this_month": sessions_this_month,
        "series": series,
        "recent": recent,
    }
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `... pytest tests/test_aggregate.py -v` → PASS (all).

```bash
git add src/ach_stats/api/app/aggregate.py src/ach_stats/api/tests/test_aggregate.py
git commit -m "feat(stats-svc): contract builder — partial flags, TZ month, series, recent"
```

---

### Task 6: FastAPI routes wire reader + aggregate

**Files:**
- Modify: `src/ach_stats/api/app/main.py`
- Test: `src/ach_stats/api/tests/test_routes.py` (extend)

**Interfaces:**
- Consumes: `reader.*`, `aggregate.build_contract`.
- Produces: `GET /api/leaderboard?days=30` → contract; `GET /api/sessions?n=50` → `{"recent":[...]}`.
  A redis client factory is injected via `app.state.redis` so tests use fakeredis; env
  `ACH_STATS_REDIS_URL`, `ACH_STATS_TZ` configure production.

- [ ] **Step 1: Write the failing test (append to `test_routes.py`)**

```python
import time

import fakeredis.aioredis
from fastapi.testclient import TestClient

from app.main import create_app


def _seed_app():
    app = create_app()
    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    app.state.redis = fake
    app.state.tz = "UTC"
    now = int(time.time() * 1000)
    import anyio

    async def seed():
        await fake.xadd("ach:sessions",
                        {"v": "1", "model": "claude-opus-4-8", "cost": "0.90",
                         "output_tokens": "100", "duration_ms": "1000", "input_tokens": "100",
                         "status": "completed", "turns": "2", "task": "Set up flags"},
                        id=f"{now - 1000}-0")
        await fake.xadd("ach:sessions",
                        {"v": "1", "model": "glm-5-2", "cost": "0.10", "output_tokens": "50",
                         "duration_ms": "1000", "input_tokens": "50", "status": "completed",
                         "turns": "1", "task": "Add pagination"},
                        id=f"{now - 500}-0")
    anyio.from_thread.run  # noqa: B018  (import marker; seeding runs below)
    import asyncio
    asyncio.get_event_loop().run_until_complete(seed())
    return app


def test_leaderboard_route():
    client = TestClient(_seed_app())
    r = client.get("/api/leaderboard?days=30")
    assert r.status_code == 200
    body = r.json()
    assert body["leaderboard"]["sorted_by"] == "spend"
    assert body["leaderboard"]["rows"][0]["model"] == "claude-opus-4-8"
    assert body["totals"]["sessions"] == 2


def test_sessions_route():
    client = TestClient(_seed_app())
    r = client.get("/api/sessions?n=10")
    assert r.status_code == 200
    assert r.json()["recent"][0]["model"] == "glm-5-2"  # newest first
```

> If the `asyncio.get_event_loop()` seeding is awkward in your pytest setup, seed with
> `TestClient`'s portal instead: `with TestClient(app) as c:` and an `@app.on_event("startup")`
> seeding hook guarded by a test flag. The behavioral assertions are what matter.

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/api && uv run --project . pytest tests/test_routes.py -v`
Expected: FAIL (routes 404 / missing).

- [ ] **Step 3: Implement routes**

Replace `src/ach_stats/api/app/main.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""FastAPI app: leaderboard/sessions JSON + the built SPA. See design spec §6."""

from __future__ import annotations

import os
import time
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.aggregate import build_contract
from app.reader import read_coverage_start, read_recent, read_window


def _redis(request: Request) -> Any:
    client = getattr(request.app.state, "redis", None)
    if client is None:
        import redis.asyncio as redis_asyncio

        client = redis_asyncio.from_url(
            os.environ["ACH_STATS_REDIS_URL"], decode_responses=True
        )
        request.app.state.redis = client
    return client


def _tz(request: Request) -> str:
    return getattr(request.app.state, "tz", os.environ.get("ACH_STATS_TZ", "UTC"))


def create_app() -> FastAPI:
    app = FastAPI(title="ach-stats")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/leaderboard")
    async def leaderboard(request: Request, days: int = Query(30, ge=1, le=62)) -> JSONResponse:
        client = _redis(request)
        now = int(time.time() * 1000)
        start = now - days * 86_400_000
        window = await read_window(client, start, now)
        recent = await read_recent(client, 12)
        coverage = await read_coverage_start(client)
        contract = build_contract(
            window_rows=window, recent_rows=recent, coverage_start_ms=coverage,
            now_ms=now, tz=_tz(request), range_start_ms=start, range_end_ms=now,
        )
        return JSONResponse(contract)

    @app.get("/api/sessions")
    async def sessions(request: Request, n: int = Query(50, ge=1, le=200)) -> JSONResponse:
        client = _redis(request)
        recent = await read_recent(client, n)
        payload = [
            {"ts": r["ts_ms"], "task": r["task"], "model": r["model"],
             "tokens": r["input_tokens"] + r["output_tokens"], "cost": r["cost"],
             "turns": r["turns"], "status": r["status"], "retry": r["retry"]}
            for r in recent
        ]
        return JSONResponse({"recent": payload})

    return app
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `... pytest tests/test_routes.py -v` → PASS.

```bash
git add src/ach_stats/api/app/main.py src/ach_stats/api/tests/test_routes.py
git commit -m "feat(stats-svc): /api/leaderboard + /api/sessions routes"
```

---

### Task 7: Serve the built SPA as static

**Files:**
- Modify: `src/ach_stats/api/app/main.py`

**Interfaces:**
- Produces: mount of `ui/dist` at `/` (after API routers, so `/api/*` and `/healthz` win). Guarded
  so the API imports even when `ui/dist` is absent (tests, pre-build).

- [ ] **Step 1: Add the guarded static mount (end of `create_app`, before `return app`)**

```python
    from pathlib import Path

    from fastapi.staticfiles import StaticFiles

    dist = Path(__file__).resolve().parent.parent / "ui" / "dist"
    if dist.is_dir():
        app.mount("/", StaticFiles(directory=str(dist), html=True), name="spa")
```

- [ ] **Step 2: Verify tests still pass (no dist present → mount skipped)**

Run: `cd src/ach_stats/api && uv run --project . pytest -q`
Expected: PASS (all service tests; static mount skipped because `ui/dist` doesn't exist yet).

- [ ] **Step 3: Commit**

```bash
git add src/ach_stats/api/app/main.py
git commit -m "feat(stats-svc): serve built SPA static (guarded when dist absent)"
```

---

### Task 8: Scaffold the UI by lifting the alitellm-auth design system

**Files:**
- Create: `src/ach_stats/ui/` (Vite + TS + Tailwind + shadcn), lifted from `../alitellm-auth/src/ui`.

**Interfaces:**
- Produces: a Vite app that builds to `src/ach_stats/ui/dist`, with `AppShell`, `ThemeToggle`, the
  shadcn `components/ui/*`, `lib/format.ts`, and TanStack Query wired — retargeted to ach-stats.

> **This is a lift, not new authorship.** The donor is `../alitellm-auth/src/ui`. Copy the design
> system verbatim; delete the auth/keys features; keep the shell + primitives.

- [ ] **Step 1: Copy the UI skeleton**

```bash
mkdir -p src/ach_stats/ui
cp -r ../alitellm-auth/src/ui/{package.json,vite.config.ts,tsconfig.json,tsconfig.node.json,index.html,components.json} src/ach_stats/ui/
cp -r ../alitellm-auth/src/ui/src src/ach_stats/ui/src
```

- [ ] **Step 2: Strip donor features not in scope**

Delete auth/keys routes+components (out of scope): under `src/ach_stats/ui/src`, remove
`routes/{Login,Mcp,A2a,HowTo,Models}.tsx` (+ their `.test.tsx`) and `components/keys/`. Keep
`components/layout/` (AppShell, ThemeToggle, SiteFooter, Loading/Error cards), `components/ui/`,
`lib/format.ts`, `main.tsx`, `index.css`. Keep `components/stats/{KpiRow,SpendChart,chart-common}`
(reused in Task 10); remove the stats leaves not in the v0 cut
(`UsageDonut,RequestsChart,TopKeys,BudgetPanel,ExportCsvButton,DateRange`).

- [ ] **Step 3: Set the app identity + install**

Edit `src/ach_stats/ui/package.json`: set `"name": "ach-stats-ui"`. Point the Vite dev proxy (in
`vite.config.ts`) `/api` → `http://localhost:8000`. Then:

```bash
cd src/ach_stats/ui && npm install && npm run build
```

Expected: `dist/` is produced (donor build config already targets `dist`). Fix any import errors
from the deletions (remove references to deleted routes in the router/`App.tsx`).

- [ ] **Step 4: Commit**

```bash
git add src/ach_stats/ui
git commit -m "feat(stats-ui): lift alitellm-auth design system, strip out-of-scope features"
```

---

### Task 9: UI contract types + data hook

**Files:**
- Create: `src/ach_stats/ui/src/lib/api-types.ts`
- Create: `src/ach_stats/ui/src/hooks/use-leaderboard.ts`
- Test: `src/ach_stats/ui/src/hooks/use-leaderboard.test.ts`

**Interfaces:**
- Produces: TS types mirroring the §4.4 contract; `useLeaderboard(days: number)` (TanStack Query)
  hitting `GET /api/leaderboard?days=`.

- [ ] **Step 1: Write the failing test**

`src/ach_stats/ui/src/hooks/use-leaderboard.test.ts`:

```typescript
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import { useLeaderboard } from './use-leaderboard';

afterEach(() => vi.restoreAllMocks());

test('useLeaderboard fetches and returns the contract', async () => {
  const contract = { leaderboard: { sorted_by: 'spend', rows: [] }, totals: { sessions: 0 } };
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(contract), { status: 200 })));

  const qc = new QueryClient();
  const wrapper = ({ children }: { children: React.ReactNode }) => (
    <QueryClientProvider client={qc}>{children}</QueryClientProvider>
  );
  const { result } = renderHook(() => useLeaderboard(30), { wrapper });
  await waitFor(() => expect(result.current.data).toBeDefined());
  expect(result.current.data!.leaderboard.sorted_by).toBe('spend');
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/ui && npm run test -- use-leaderboard`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement types + hook**

`src/ach_stats/ui/src/lib/api-types.ts`:

```typescript
export interface LeaderboardRow {
  rank: number; model: string; provider: string; tag: string | null;
  score: number | null; speed_tok_s: number | null; cost_per_mtok: number | null;
  spend: number; sessions: number;
}
export interface Totals {
  sessions: number; tokens: number; spend: number;
  avg_cost_per_session: number | null; aborted: number; partial: boolean;
}
export interface RecentRow {
  ts: number; task: string; model: string; tokens: number; cost: number;
  turns: number; status: string; retry: boolean;
}
export interface SeriesPoint { date: string; spend: number; sessions: number; tokens: number; partial: boolean; }
export interface Contract {
  range: { start: number; end: number; days: number; coverage_start: number | null; tz: string };
  totals: Totals;
  leaderboard: { sorted_by: 'spend' | 'score'; rows: LeaderboardRow[] };
  cost_per_session: { model: string; avg: number | null }[];
  sessions_this_month: { rows: { model: string; count: number }[]; partial: boolean };
  series: SeriesPoint[];
  recent: RecentRow[];
}
```

`src/ach_stats/ui/src/hooks/use-leaderboard.ts`:

```typescript
import { useQuery } from '@tanstack/react-query';

import type { Contract } from '@/lib/api-types';

async function fetchLeaderboard(days: number): Promise<Contract> {
  const res = await fetch(`/api/leaderboard?days=${days}`);
  if (!res.ok) throw new Error(`leaderboard ${res.status}`);
  return (await res.json()) as Contract;
}

export function useLeaderboard(days: number) {
  return useQuery({ queryKey: ['leaderboard', days], queryFn: () => fetchLeaderboard(days) });
}
```

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `cd src/ach_stats/ui && npm run test -- use-leaderboard` → PASS.

```bash
git add src/ach_stats/ui/src/lib/api-types.ts src/ach_stats/ui/src/hooks/use-leaderboard.ts \
        src/ach_stats/ui/src/hooks/use-leaderboard.test.ts
git commit -m "feat(stats-ui): contract types + useLeaderboard hook"
```

---

### Task 10: Leaderboard page — table + KPI + recent + series + partial banner

**Files:**
- Create: `src/ach_stats/ui/src/routes/Leaderboard.tsx`
- Create: `src/ach_stats/ui/src/routes/Leaderboard.test.tsx`
- Modify: the app router (`App.tsx`) to render `Leaderboard` at `/`.

**Interfaces:**
- Consumes: `useLeaderboard` (Task 9), `LeaderboardRow` types, donor `KpiRow`/`SpendChart` +
  `lib/format`.
- Produces: the dashboard page — ranked table (rank·model·provider·score·speed·$/Mtok·tag with
  "unrated" for null score), KPI row (incl. `aborted`), recent-sessions table (badges
  `status`/`retry`), one `SpendChart`, and a "showing data since {date}" banner when
  `totals.partial`.

- [ ] **Step 1: Write the failing test**

`src/ach_stats/ui/src/routes/Leaderboard.test.tsx`:

```tsx
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';

import Leaderboard from './Leaderboard';

afterEach(() => vi.restoreAllMocks());

function renderPage(contract: unknown) {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(contract), { status: 200 })));
  const qc = new QueryClient();
  render(
    <QueryClientProvider client={qc}>
      <Leaderboard />
    </QueryClientProvider>,
  );
}

const base = {
  range: { start: 0, end: 1, days: 30, coverage_start: null, tz: 'UTC' },
  totals: { sessions: 3, tokens: 100, spend: 1.0, avg_cost_per_session: 0.33, aborted: 1, partial: false },
  leaderboard: {
    sorted_by: 'spend',
    rows: [
      { rank: 1, model: 'claude-opus-4-8', provider: 'Anthropic', tag: 'Frontier', score: null,
        speed_tok_s: 63, cost_per_mtok: 31.5, spend: 0.9, sessions: 2 },
    ],
  },
  cost_per_session: [], sessions_this_month: { rows: [], partial: false }, series: [], recent: [],
};

test('renders ranked model and unrated score', async () => {
  renderPage(base);
  await waitFor(() => expect(screen.getByText('claude-opus-4-8')).toBeInTheDocument());
  expect(screen.getByText(/unrated/i)).toBeInTheDocument();
});

test('renders the partial banner when totals.partial', async () => {
  renderPage({ ...base, totals: { ...base.totals, partial: true },
               range: { ...base.range, coverage_start: 1_700_000_000_000 } });
  await waitFor(() => expect(screen.getByText(/showing data since/i)).toBeInTheDocument());
});
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd src/ach_stats/ui && npm run test -- Leaderboard`
Expected: FAIL (module not found).

- [ ] **Step 3: Implement the page**

`src/ach_stats/ui/src/routes/Leaderboard.tsx`:

```tsx
import { useLeaderboard } from '@/hooks/use-leaderboard';
import type { LeaderboardRow } from '@/lib/api-types';

function ScoreCell({ score }: { score: number | null }) {
  if (score === null) return <span className="text-muted-foreground italic">unrated</span>;
  return <span>{score.toFixed(1)}</span>;
}

export default function Leaderboard() {
  const { data, isPending, isError } = useLeaderboard(30);
  if (isPending) return <div className="p-8 text-muted-foreground">Loading…</div>;
  if (isError || !data) return <div className="p-8 text-destructive">Failed to load stats.</div>;

  const { leaderboard, totals, range, recent } = data;
  const sinceBanner =
    totals.partial && range.coverage_start
      ? new Date(range.coverage_start).toLocaleDateString()
      : null;

  return (
    <div className="space-y-6 p-6">
      {sinceBanner && (
        <div className="rounded-md border border-amber-500/40 bg-amber-500/10 px-4 py-2 text-sm">
          Showing data since {sinceBanner} (older data past retention).
        </div>
      )}

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <Kpi label="Total Sessions" value={String(totals.sessions)} />
        <Kpi label="Total Spend" value={`$${totals.spend.toFixed(2)}`} />
        <Kpi label="Aborted" value={String(totals.aborted)} />
        <Kpi label="Avg $/Session"
             value={totals.avg_cost_per_session === null ? '—' : `$${totals.avg_cost_per_session.toFixed(3)}`} />
      </div>

      <div>
        <h2 className="mb-1 text-lg font-semibold">Leaderboard</h2>
        <p className="mb-3 text-xs uppercase tracking-wide text-muted-foreground">
          Ranked by {leaderboard.sorted_by}
        </p>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-muted-foreground">
            <tr><th>Rank</th><th>Model</th><th>Provider</th><th>Score</th><th>Speed</th>
                <th>$/Mtok</th><th>Sessions</th><th>Tag</th></tr>
          </thead>
          <tbody>
            {leaderboard.rows.map((r: LeaderboardRow) => (
              <tr key={r.model} className="border-t border-border/50">
                <td>{r.rank}</td><td className="font-mono">{r.model}</td><td>{r.provider}</td>
                <td><ScoreCell score={r.score} /></td>
                <td>{r.speed_tok_s === null ? '—' : `${Math.round(r.speed_tok_s)} tok/s`}</td>
                <td>{r.cost_per_mtok === null ? '—' : `$${r.cost_per_mtok.toFixed(2)}`}</td>
                <td>{r.sessions}</td><td>{r.tag ?? '—'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div>
        <h2 className="mb-3 text-lg font-semibold">Recent Sessions</h2>
        <table className="w-full text-sm">
          <thead className="text-left text-xs uppercase text-muted-foreground">
            <tr><th>Task</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Turns</th><th>Status</th></tr>
          </thead>
          <tbody>
            {recent.map((r, i) => (
              <tr key={i} className="border-t border-border/50">
                <td>{r.task}</td><td className="font-mono">{r.model}</td><td>{r.tokens}</td>
                <td>${r.cost.toFixed(2)}</td><td>{r.turns}</td>
                <td>{r.status}{r.retry ? ' · retry' : ''}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function Kpi({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/50 bg-card p-4">
      <div className="text-xs uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className="mt-1 text-2xl font-semibold">{value}</div>
    </div>
  );
}
```

> The plain `Kpi`/table markup above is a minimal, test-satisfying baseline. If the donor's
> `KpiRow` and `SpendChart` fit, swap them in — but keep the DOM text the tests assert
> (`unrated`, `showing data since`).

Modify `App.tsx`: route `/` renders `<Leaderboard />` inside the existing `AppShell` (replace the
donor's default dashboard route).

- [ ] **Step 4: Run to verify it passes, then commit**

Run: `cd src/ach_stats/ui && npm run test -- Leaderboard` → PASS. Then `npm run build` → dist ok.

```bash
git add src/ach_stats/ui/src/routes/Leaderboard.tsx src/ach_stats/ui/src/routes/Leaderboard.test.tsx \
        src/ach_stats/ui/src/App.tsx
git commit -m "feat(stats-ui): Leaderboard page — table, KPIs, recent, partial banner"
```

---

### Task 11: Multi-stage Dockerfile

**Files:**
- Create: `docker/stats.Dockerfile`, `docker/.dockerignore.stats`

- [ ] **Step 1: Write the Dockerfile**

`docker/stats.Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
# Stage 1 — build the SPA
FROM node:22-slim AS ui
WORKDIR /ui
COPY src/ach_stats/ui/package.json src/ach_stats/ui/package-lock.json ./
RUN npm ci
COPY src/ach_stats/ui/ ./
RUN npm run build

# Stage 2 — python deps
FROM python:3.12-slim AS deps
WORKDIR /app
COPY src/ach_stats/api/pyproject.toml ./
RUN pip install --no-cache-dir --target=/app/site-packages \
    "fastapi==0.116.1" "uvicorn[standard]==0.34.0" "redis>=5,<6" "pydantic>=2,<3"

# Stage 3 — runtime
FROM python:3.12-slim
WORKDIR /app
ENV PYTHONPATH=/app/site-packages:/app
COPY --from=deps /app/site-packages /app/site-packages
COPY src/ach_stats/api/app /app/app
COPY --from=ui /ui/dist /app/ui/dist
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
```

`docker/.dockerignore.stats`:

```
**/__pycache__
**/node_modules
**/dist
**/.pytest_cache
**/tests
```

- [ ] **Step 2: Build to verify**

Run: `docker build -f docker/stats.Dockerfile -t ach-stats:dev .`
Expected: image builds; final stage contains `app/` + `ui/dist`, no build tools.

- [ ] **Step 3: Commit**

```bash
git add docker/stats.Dockerfile docker/.dockerignore.stats
git commit -m "feat(stats): multi-stage Dockerfile (node build -> python serve)"
```

---

### Task 12: docker-compose dev wiring

**Files:**
- Modify: `docker-compose.dev.yml` (add `redis` + `ach-stats`; give the harness `ACH_STATS_*`)

- [ ] **Step 1: Add services**

Add to `docker-compose.dev.yml`:

```yaml
  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  ach-stats:
    build:
      context: .
      dockerfile: docker/stats.Dockerfile
    environment:
      ACH_STATS_REDIS_URL: redis://redis:6379
      ACH_STATS_TZ: Europe/Madrid
    ports: ["8000:8000"]
    depends_on: [redis]
```

On the existing harness service, add:

```yaml
    environment:
      ACH_STATS_REDIS_URL: redis://redis:6379
      ACH_STATS_RETENTION: "3024000"
      ACH_STATS_TZ: Europe/Madrid
```

- [ ] **Step 2: Smoke test end-to-end**

```bash
docker compose -f docker-compose.dev.yml up -d redis ach-stats
curl -sf http://localhost:8000/healthz
# seed one entry, then:
curl -sf "http://localhost:8000/api/leaderboard?days=30" | head -c 400
docker compose -f docker-compose.dev.yml down
```

Expected: `/healthz` → `{"status":"ok"}`; leaderboard returns a contract JSON (empty rows until the
harness writes real sessions).

- [ ] **Step 3: Commit**

```bash
git add docker-compose.dev.yml
git commit -m "feat(stats): dev compose — redis + ach-stats + harness env wiring"
```

---

## Self-Review

- **Spec coverage (A2 rows of spec §4.2/§4.4/§6/§7):**
  - Tolerant reader + `v` dispatch → Task 3.
  - provider/tag static map → Task 2.
  - totals (aborted included + `aborted` count) + leaderboard + derived + `sorted_by` + null score
    "unrated" → Tasks 4, 10.
  - coverage_start + per-panel `partial` + TZ calendar month + series + recent → Task 5.
  - `/api/leaderboard`, `/api/sessions`, `/healthz`, SPA static → Tasks 1, 6, 7.
  - UI lift + Leaderboard + KPI(aborted) + recent(status/retry) + partial banner → Tasks 8–10.
  - Multi-stage Docker + `.dockerignore` + dev compose → Tasks 11, 12.
  - **Deferred (spec §6.4/§10):** BudgetPanel, CSV, UsageDonut, RequestsChart, OIDC auth — NOT in
    these tasks by design; network-boundary is a deploy concern noted in the spec.
- **Placeholder scan:** none — Python steps carry full code; UI lift lists exact donor paths + full
  new code; the one `KpiRow/SpendChart` swap note keeps the test-asserted DOM text explicit.
- **Type consistency:** contract keys match across `aggregate.build_contract` (Task 5),
  `api-types.ts` (Task 9), and `Leaderboard.tsx` (Task 10): `leaderboard.sorted_by`,
  `leaderboard.rows[].{rank,model,provider,tag,score,speed_tok_s,cost_per_mtok,spend,sessions}`,
  `totals.{sessions,spend,aborted,avg_cost_per_session,partial}`, `range.coverage_start`,
  `recent[].{ts,task,model,tokens,cost,turns,status,retry}`. Stream name `ach:sessions` matches A1.
