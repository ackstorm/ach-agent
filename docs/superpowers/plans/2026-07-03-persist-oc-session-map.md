# Persist opencode session map to state.db Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist the pool-owned `session_key → opencode session id` map (today in-memory only) to the shared `state.db`, bounded to a max size, so `channel.session='auto'` conversational continuity survives a full harness restart — e.g. after a restart, a new PR from repo 42 still resolves to the opencode session opencode already holds on its own persistent disk.

**Architecture:** Add `_SqliteSessionMap`, a **disk-resident** `MutableMapping[str, str]` backed by an `oc_sessions` table in the same `mountPath/state/state.db` the dedup store uses (a second WAL connection). **No in-memory copy** — every lookup is one indexed `SELECT`, every mutation a small write. A turn does one `get` (+ at most one `set`) and turns are seconds apart (an LLM call), so per-turn SQL is negligible and RAM stays flat regardless of how many `session_key`s accumulate (scales past any in-memory cache). Bounded to `maxsize` rows by **least-recently-used**: `get()` bumps `last_used`, so an actively-read key is never evicted even when the table is full. `EnginePool` gains an injectable `oc_sessions` param; `main._open_session_store(cfg)` selects disk vs in-memory like `_open_dedup_store`, but **fail-OPEN**. No config-schema / CONTRACT_v3 change — `state.db`'s internal layout is harness-private, not the operator seam.

**Tech Stack:** Python 3.12 + asyncio, `sqlite3` (stdlib, WAL), `collections.abc.MutableMapping`, pytest.

## Global Constraints

- **No new dependency** — `sqlite3` is stdlib; reuse it. (ponytail: rung 3.)
- **No in-memory cache** — a `SELECT` by primary key is microseconds and happens once per multi-second turn, so a RAM cache would add mem↔db sync for no measurable gain. The DB *is* the reuse store. (See "Deferred".)
- **No config-schema field, no CONTRACT_v3 change, no `gen_schema.py` regen** — `maxsize` stays a code constant (`1024`; raise the constant if a deployment has more active sessions). The `state.db` internal layout is harness-private, not the operator seam.
- **`src/ach_agent/engine/pool.py` MUST NOT import** from `router.*`, `hermes_agent.*`, `config.*`, or `main.*` (D-08, RTR-06). Only stdlib (`sqlite3`, `time`, `collections`, `collections.abc`, `pathlib`) + `structlog` are allowed there.
- **Persistence never breaks a turn** — a `SELECT` failure returns the caller's default (→ `run_invocation` mints a fresh opencode session; its 404 guard already handles a missing id); a write failure is swallowed with a WARN.
- **All SQL uses `?` placeholders**, never string interpolation (ASVS V5, T-03-01).
- **`state.db` is shared** with the dedup store (two WAL connections) — both set `PRAGMA busy_timeout` so a rare cross-writer collision waits instead of raising `database is locked`.
- **Open order:** `_open_session_store` is called AFTER `_open_dedup_store`, which opens/repairs `state.db` first; by then the file is valid and the session store's second connection just adds its table.
- Verify with `uv run pytest <path> -q` and `uv run mypy --strict <paths>` (scoped runs bypass the devtools container per `CLAUDE.md`). `ek_` / tokens are never logged.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `src/ach_agent/engine/pool.py` | Session map classes + pool | Add `_SqliteSessionMap(MutableMapping[str, str])` (disk-resident); make `EnginePool.__init__` accept an injectable `oc_sessions` map (default in-memory `_LRUSessionMap`). |
| `src/ach_agent/router/dedup.py` | Dedup store on `state.db` | One line: `PRAGMA busy_timeout=5000` (shared-file safety). |
| `src/ach_agent/main.py` | Boot wiring | Add `_open_session_store(cfg)`; build it after `_open_dedup_store`, pass to `EnginePool`; close `pool.oc_sessions` on both shutdown paths. |
| `tests/engine/test_pool.py` | Unit tests | `_SqliteSessionMap` reopen/LRU-evict/pop/cap + `EnginePool` injection. |
| `tests/test_session_store.py` (new) | Selection + shared-file | `_open_session_store` disabled/enabled/shared-with-dedup. |

Interface contract used by callers (do not change): the map is a `MutableMapping[str, str]` exercised only via `.get(key, default)`, `map[key] = value`, `.pop(key, default)`, `len(map)`, `key in map`. `run_invocation`'s `oc_sessions: MutableMapping[str, str] | None` signature is unchanged. The in-memory `_LRUSessionMap` (persistence disabled) is untouched and still satisfies this.

---

### Task 1: `_SqliteSessionMap` — disk-resident session map

**Files:**
- Modify: `src/ach_agent/engine/pool.py` (imports near lines 25-31; new class after `_LRUSessionMap`, i.e. after line 68)
- Test: `tests/engine/test_pool.py` (append after the `_LRUSessionMap` tests, after line 378)

**Interfaces:**
- Consumes: stdlib `sqlite3`, `time`, `Path`; `collections.abc.{MutableMapping, Iterator}`.
- Produces: `class _SqliteSessionMap(MutableMapping[str, str])` with `__init__(self, db_path: Path, maxsize: int = 1024)` and `__getitem__`, `get`, `__setitem__`, `__delitem__`, `pop`, `__contains__`, `__iter__`, `__len__`, `close()`. `get()` bumps `last_used`; `__setitem__` prunes to the `maxsize` most-recently-used rows. It is a real `MutableMapping[str, str]`, so it drops straight into `run_invocation(oc_sessions=...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_pool.py`:

```python
# ---------------------------------------------------------------------------
# _SqliteSessionMap (session_key → ses_ persisted to state.db, disk-resident)
# ---------------------------------------------------------------------------


def test_sqlite_session_map_survives_reopen(tmp_path):
    """Values written before close() are present after a fresh open (restart)."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db)
    m["gitlab:server:42"] = "ses-a"
    m["cron:nightly"] = "ses-b"
    m.close()

    m2 = _SqliteSessionMap(db)  # simulate a harness restart
    assert m2.get("gitlab:server:42") == "ses-a"
    assert m2.get("cron:nightly") == "ses-b"
    m2.close()


def test_sqlite_session_map_get_missing_returns_default(tmp_path):
    from ach_agent.engine.pool import _SqliteSessionMap

    m = _SqliteSessionMap(tmp_path / "state.db")
    assert m.get("nope") is None
    assert m.get("nope", "fallback") == "fallback"
    m.close()


def test_sqlite_session_map_evicts_lru_keeps_active(tmp_path):
    """Over maxsize → the LEAST-recently-USED row is evicted; a read-active key survives."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db, maxsize=2)
    m["a"] = "1"
    m["b"] = "2"
    assert m.get("a") == "1"  # bump "a" → "b" is now least-recently-used
    m["c"] = "3"  # over cap → evicts "b", not the just-read "a"
    assert "a" in m
    assert "b" not in m
    assert m.get("c") == "3"
    m.close()


def test_sqlite_session_map_cap_bounds_row_count(tmp_path):
    from ach_agent.engine.pool import _SqliteSessionMap

    m = _SqliteSessionMap(tmp_path / "state.db", maxsize=3)
    for i in range(5):
        m[f"k{i}"] = f"v{i}"
    assert len(m) == 3  # only the 3 most-recently-used rows remain
    m.close()


def test_sqlite_session_map_pop_deletes_row(tmp_path):
    """pop() removes the row so it does not reappear after reopen."""
    from ach_agent.engine.pool import _SqliteSessionMap

    db = tmp_path / "state.db"
    m = _SqliteSessionMap(db)
    m["lane-1"] = "ses-a"
    assert m.pop("lane-1", None) == "ses-a"
    assert m.pop("lane-1", None) is None  # idempotent
    m.close()

    m2 = _SqliteSessionMap(db)
    assert m2.get("lane-1") is None
    m2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_pool.py -k sqlite_session_map -q`
Expected: FAIL — `ImportError: cannot import name '_SqliteSessionMap'`.

- [ ] **Step 3: Add stdlib imports to pool.py**

In `src/ach_agent/engine/pool.py`, extend the import block (currently lines ~25-31). After `import asyncio` add `import sqlite3` and `import time`; extend the `collections.abc` import to include `Iterator` and `MutableMapping`:

```python
from __future__ import annotations

import asyncio
import sqlite3
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Iterator, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING

import structlog
```

- [ ] **Step 4: Bump the in-memory LRU cap to 1024, then add `_SqliteSessionMap`**

First raise the cap on the in-memory map used when persistence is disabled — `src/ach_agent/engine/pool.py:51`, `_LRUSessionMap.__init__`:

```python
    def __init__(self, maxsize: int = 1024) -> None:
```

Then insert `_SqliteSessionMap` immediately after `_LRUSessionMap` (after line 68, before `class EnginePool`):

```python
class _SqliteSessionMap(MutableMapping[str, str]):
    """Disk-resident session_key → opencode session id map, persisted in state.db.

    No in-memory copy: every lookup is an indexed SELECT and every mutation a small
    write, straight to state.db. A turn does ONE get (+ at most one set) and turns are
    seconds apart (an LLM call), so per-turn SQL is negligible — and RAM stays flat no
    matter how many session_keys accumulate. This is what lets channel.session='auto'
    survive a full harness restart: after a restart a repeat event for the same
    session_key resolves to the opencode session opencode still holds on its own disk.

    Bounded to ``maxsize`` rows by LEAST-RECENTLY-USED: get() bumps last_used, and each
    set() prunes everything outside the maxsize most-recently-used rows — so an actively
    read key (e.g. gitlab:server:42, hit every turn) is never evicted while idle mappings
    drop. Shares state.db with the dedup store via a second WAL connection (busy_timeout
    absorbs the rare cross-writer lock).

    Persistence never breaks a turn: a SELECT failure returns the default (→ the caller
    mints a fresh opencode session, handled by run_invocation's 404 guard); a write
    failure is swallowed with a WARN.
    """

    def __init__(self, db_path: Path, maxsize: int = 1024) -> None:
        self._maxsize = maxsize
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=5000")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS oc_sessions "
            "(key TEXT PRIMARY KEY, oc_session_id TEXT NOT NULL, last_used REAL NOT NULL)"
        )
        self._con.execute(
            "CREATE INDEX IF NOT EXISTS idx_oc_sessions_last_used ON oc_sessions(last_used)"
        )
        self._con.commit()

    def __getitem__(self, key: str) -> str:
        row = self._con.execute(
            "SELECT oc_session_id FROM oc_sessions WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            raise KeyError(key)
        # Bump recency so an actively-read key survives LRU eviction (see __setitem__).
        try:
            self._con.execute(
                "UPDATE oc_sessions SET last_used=? WHERE key=?", (time.time(), key)
            )
            self._con.commit()
        except sqlite3.Error:
            log.warning("session map: last_used bump failed", key=key, exc_info=True)
        return str(row[0])

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        try:
            return self[key]
        except KeyError:
            return default
        except sqlite3.Error:
            log.warning("session map: lookup failed (treating as miss)", key=key, exc_info=True)
            return default

    def __setitem__(self, key: str, value: str) -> None:
        try:
            self._con.execute(
                "INSERT OR REPLACE INTO oc_sessions (key, oc_session_id, last_used) "
                "VALUES (?,?,?)",
                (key, value, time.time()),
            )
            # Bound the table: keep only the maxsize most-recently-used rows (the row just
            # inserted has the newest last_used, so it is always among them).
            self._con.execute(
                "DELETE FROM oc_sessions WHERE key NOT IN "
                "(SELECT key FROM oc_sessions ORDER BY last_used DESC LIMIT ?)",
                (self._maxsize,),
            )
            self._con.commit()
        except sqlite3.Error:
            log.warning("session map: persist failed", key=key, exc_info=True)

    def __delitem__(self, key: str) -> None:
        cur = self._con.execute("DELETE FROM oc_sessions WHERE key=?", (key,))
        self._con.commit()
        if cur.rowcount == 0:
            raise KeyError(key)

    def pop(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        row = self._con.execute(
            "SELECT oc_session_id FROM oc_sessions WHERE key=?", (key,)
        ).fetchone()
        if row is None:
            return default
        try:
            self._con.execute("DELETE FROM oc_sessions WHERE key=?", (key,))
            self._con.commit()
        except sqlite3.Error:
            log.warning("session map: pop-delete failed", key=key, exc_info=True)
        return str(row[0])

    def __contains__(self, key: object) -> bool:
        row = self._con.execute(
            "SELECT 1 FROM oc_sessions WHERE key=?", (key,)
        ).fetchone()
        return row is not None

    def __iter__(self) -> Iterator[str]:
        return (
            str(r[0])
            for r in self._con.execute("SELECT key FROM oc_sessions").fetchall()
        )

    def __len__(self) -> int:
        row = self._con.execute("SELECT COUNT(*) FROM oc_sessions").fetchone()
        return int(row[0]) if row else 0

    def close(self) -> None:
        """Close the SQLite connection (called on harness shutdown)."""
        self._con.close()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_pool.py -k sqlite_session_map -q`
Expected: PASS (5 tests).

- [ ] **Step 6: Type-check pool.py**

Run: `uv run mypy --strict src/ach_agent/engine/pool.py`
Expected: `Success: no issues found`.

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/engine/pool.py tests/engine/test_pool.py
git commit -m "feat(engine): disk-resident SQLite session map"
```

---

### Task 2: `EnginePool` accepts an injectable session map

**Files:**
- Modify: `src/ach_agent/engine/pool.py:90-107` (`EnginePool.__init__`)
- Test: `tests/engine/test_pool.py` (append two tests near the other pool tests)

**Interfaces:**
- Consumes: `_LRUSessionMap` (default), any `MutableMapping[str, str]` (injected — e.g. `_SqliteSessionMap` from Task 1).
- Produces: `EnginePool(oc_sessions: MutableMapping[str, str] | None = None)`. Default (`None`) → a fresh in-memory `_LRUSessionMap` (unchanged behavior); an injected map is stored verbatim on `self.oc_sessions`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_pool.py`:

```python
def test_pool_accepts_injected_session_map():
    """A caller (main._open_session_store) can inject the disk-resident map."""
    from ach_agent.engine.pool import EnginePool

    injected: dict[str, str] = {"lane-1": "ses-a"}
    pool = EnginePool(oc_sessions=injected)
    assert pool.oc_sessions is injected


def test_pool_default_session_map_is_lru_still():
    """No arg → still an in-memory _LRUSessionMap (unchanged default)."""
    from ach_agent.engine.pool import EnginePool, _LRUSessionMap

    pool = EnginePool()
    assert isinstance(pool.oc_sessions, _LRUSessionMap)
    assert len(pool.oc_sessions) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_pool.py -k "injected_session_map or default_session_map_is_lru_still" -q`
Expected: FAIL — `TypeError: EnginePool.__init__() got an unexpected keyword argument 'oc_sessions'`.

- [ ] **Step 3: Add the parameter**

In `src/ach_agent/engine/pool.py`, change the `EnginePool.__init__` signature (line 90) and the `self.oc_sessions` assignment (lines 96-100):

```python
    def __init__(self, oc_sessions: MutableMapping[str, str] | None = None) -> None:
        self._servers: dict[str, ManagedServer] = {}
        self._ref_counts: dict[str, int] = {}
        self._ttl_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

        # session_key → opencode session id (ses_…). Pool-owned so it outlives
        # individual ManagedServers. Injectable so main can swap in a state.db-backed
        # map when persistence.enabled (survives a full harness restart); defaults to
        # the in-memory LRU (volatile, survives only idle-TTL server restarts).
        self.oc_sessions: MutableMapping[str, str] = (
            oc_sessions if oc_sessions is not None else _LRUSessionMap()
        )
```

- [ ] **Step 4: Run the full pool suite (no regressions)**

Run: `uv run pytest tests/engine/test_pool.py -q`
Expected: PASS — including the pre-existing `test_pool_owns_oc_sessions_map` (default is still `_LRUSessionMap`).

- [ ] **Step 5: Type-check**

Run: `uv run mypy --strict src/ach_agent/engine/pool.py`
Expected: `Success: no issues found`.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/pool.py tests/engine/test_pool.py
git commit -m "feat(engine): EnginePool accepts an injectable session map"
```

---

### Task 3: Wire the persistent session store at boot

**Files:**
- Modify: `src/ach_agent/router/dedup.py:85-93` (`FileBackedDedupStore.__init__` — add `busy_timeout`)
- Modify: `src/ach_agent/main.py` — add `_open_session_store` (near `_open_dedup_store`, after line ~158); rewire the boot block at `main.py:1164` + `:1211`; close `pool.oc_sessions` in the console finally (`~1295`) and the serve finally (`~1496`).
- Test: `tests/test_session_store.py` (new)

**Interfaces:**
- Consumes: `EnginePool(oc_sessions=...)` (Task 2), `_SqliteSessionMap` / `_LRUSessionMap` (Task 1), `cfg.persistence.{enabled,mount_path}`, `PERSISTENCE_DEGRADED` metric (`ach_agent.router.metrics`).
- Produces: `main._open_session_store(cfg) -> Any` returning a `_SqliteSessionMap` (enabled) or `_LRUSessionMap` (disabled / fail-open).

- [ ] **Step 1: Write the failing tests (new file)**

Create `tests/test_session_store.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for main._open_session_store — persistent vs in-memory selection."""

from __future__ import annotations

from pathlib import Path

from ach_agent.config.schema import AgentConfig

_BASE: dict = {
    "schemaVersion": "1",
    "agent": {"name": "a"},
    "model": {"name": "openai.gpt-5", "type": "openai"},
    "capability": {"ach": {"baseUrl": "https://ach.example.com"}},
}


def _cfg(persistence: dict) -> AgentConfig:
    raw: dict = dict(_BASE)
    raw["persistence"] = persistence
    return AgentConfig.model_validate(raw)


def test_open_session_store_disabled_is_in_memory() -> None:
    from ach_agent.engine.pool import _LRUSessionMap, _SqliteSessionMap
    from ach_agent.main import _open_session_store

    store = _open_session_store(_cfg({"enabled": False}))
    assert isinstance(store, _LRUSessionMap)
    assert not isinstance(store, _SqliteSessionMap)


def test_open_session_store_enabled_persists_to_state_db(tmp_path: Path) -> None:
    from ach_agent.engine.pool import _SqliteSessionMap
    from ach_agent.main import _open_session_store

    store = _open_session_store(_cfg({"enabled": True, "mountPath": str(tmp_path)}))
    assert isinstance(store, _SqliteSessionMap)
    store["lane-1"] = "ses-a"
    store.close()
    assert (tmp_path / "state" / "state.db").exists()


def test_open_session_store_shares_state_db_with_dedup(tmp_path: Path) -> None:
    """Dedup (opened first) and the session store live in the same state.db file."""
    from ach_agent.main import _open_dedup_store, _open_session_store

    cfg = _cfg({"enabled": True, "mountPath": str(tmp_path)})
    dedup = _open_dedup_store(cfg)
    dedup.mark("evt-1", ttl_seconds=3600)
    sess = _open_session_store(cfg)
    sess["lane-1"] = "ses-a"
    sess.close()
    dedup.close()

    # Reopen both from the same file — each table survives independently.
    dedup2 = _open_dedup_store(cfg)
    assert dedup2.seen("evt-1")
    sess2 = _open_session_store(cfg)
    assert sess2.get("lane-1") == "ses-a"
    sess2.close()
    dedup2.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_session_store.py -q`
Expected: FAIL — `AttributeError: module 'ach_agent.main' has no attribute '_open_session_store'`.

- [ ] **Step 3: Add `busy_timeout` to the dedup store (shared-file safety)**

In `src/ach_agent/router/dedup.py`, `FileBackedDedupStore.__init__` (after the `synchronous` pragma, line 88):

```python
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute("PRAGMA synchronous=NORMAL")
        self._con.execute("PRAGMA busy_timeout=5000")  # state.db shared w/ session map — wait, don't raise
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS dedup (key TEXT PRIMARY KEY, expiry REAL NOT NULL)"
        )
```

- [ ] **Step 4: Add `_open_session_store` to main.py**

In `src/ach_agent/main.py`, insert after `_open_dedup_store` (after line ~158, before `_clean_tool_name`):

```python
def _open_session_store(cfg: Any) -> Any:
    """Select the pool's session_key → opencode-session map per persistence config.

    persistence.enabled=false → in-memory _LRUSessionMap (volatile, current behavior).
    persistence.enabled=true  → _SqliteSessionMap on mountPath/state/state.db, so
      channel.session='auto' continuity survives a full harness restart; bounded to
      maxsize rows (LRU by last_used).

    Fail-OPEN (unlike _open_dedup_store, which fail-CLOSES): a missing mount or a DB
    error degrades to the in-memory map + WARN + PERSISTENCE_DEGRADED, because losing
    conversational continuity is a soft degrade, not a duplicate-firing hazard.

    Call AFTER _open_dedup_store: that opens/repairs state.db first, so this second WAL
    connection just adds the oc_sessions table to an already-valid file.
    """
    from ach_agent.engine.pool import _LRUSessionMap, _SqliteSessionMap
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    if not cfg.persistence.enabled:
        return _LRUSessionMap()

    db_path = Path(cfg.persistence.mount_path) / "state" / "state.db"
    try:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        store = _SqliteSessionMap(db_path)
        log.info("durable session map opened", db_path=str(db_path), rows=len(store))
        return store
    except Exception as exc:  # noqa: BLE001 — fail-open to in-memory (degraded, not fatal)
        log.warning(
            "session map open failed — using in-memory (fail-open)",
            db_path=str(db_path),
            error=str(exc),
        )
        PERSISTENCE_DEGRADED.inc()
        return _LRUSessionMap()
```

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run pytest tests/test_session_store.py -q`
Expected: PASS (3 tests).

- [ ] **Step 6: Rewire the boot block — build the store before the pool**

In `src/ach_agent/main.py`, the current order is `pool = EnginePool()` (line 1164) then `dedup_store = _open_dedup_store(cfg)` (line 1211). Change to open dedup first (it repairs state.db), then the session store, then the pool.

Replace line 1164:

```python
    pool = EnginePool()
```

with:

```python
    # D-03/D-04: dedup store first — it opens/repairs state.db (fail-closed on a bad
    # mount). Then the session map shares that now-valid file (fail-open). The pool
    # owns the session map so run_invocation reuses opencode sessions across restarts.
    dedup_store = _open_dedup_store(cfg)
    session_store = _open_session_store(cfg)
    pool = EnginePool(oc_sessions=session_store)
```

Then delete the now-duplicate line 1211 (`dedup_store = _open_dedup_store(cfg)`) and its two comment lines directly above it (1209-1210), leaving the `router = Router(...)` block intact and still referencing the `dedup_store` built above:

```python
    # Step 6 (cont.): construct Router with all limits from config (RTR-03/04)
    router = Router(
        max_concurrent_invocations=cfg.limits.max_concurrent_invocations,
        max_queued_total=cfg.limits.max_queued_total,
        idempotency_window_seconds=cfg.limits.idempotency_window_seconds,
        dedup_store=dedup_store,
        engine_runner=engine_runner,
        delivery_adapter=None,
        max_invocation_seconds=float(cfg.limits.max_invocation_seconds),
        channel_concurrency={ch.name: ch.concurrency for ch in cfg.channels},
    )
```

- [ ] **Step 7: Close the session map on both shutdown paths**

The disk-resident map holds a SQLite connection; close it on exit like `dedup_store`. `pool.oc_sessions` is the map (the in-memory `_LRUSessionMap` has no `close`, so guard with `hasattr`).

In the **console-mode finally** (`main.py:~1293`), right after `await pool.stop_all()`:

```python
        finally:
            # Stop any warm-held engine server (idle TTL may not have elapsed at EOF).
            await pool.stop_all()
            if hasattr(pool.oc_sessions, "close"):
                pool.oc_sessions.close()
            await stop_model_proxies()
```

In the **serve-mode finally** (`main.py:~1496`), right after `await pool.stop_all()`:

```python
        await pool.stop_all()
        if hasattr(pool.oc_sessions, "close"):
            pool.oc_sessions.close()
        # Plan 2: tear down the localhost proxies (closes their aiohttp runners/sessions).
        await stop_model_proxies()
```

- [ ] **Step 8: Run the affected suites (no regressions)**

Run: `uv run pytest tests/test_session_store.py tests/engine/test_pool.py tests/test_main_wiring.py tests/e2e/test_durability_e2e.py -q`
Expected: PASS — including `test_main_wiring.py::test_engine_runner_passes_pool_oc_sessions_to_run_invocation` and the dedup store-selection tests (dedup path unchanged aside from the pragma).

- [ ] **Step 9: Type-check the changed modules**

Run: `uv run mypy --strict src/ach_agent/main.py src/ach_agent/engine/pool.py src/ach_agent/router/dedup.py`
Expected: `Success: no issues found`.

- [ ] **Step 10: Full lint + test gate**

Run: `make lint && make test`
Expected: ruff clean, mypy clean, pytest green.

- [ ] **Step 11: Commit**

```bash
git add src/ach_agent/main.py src/ach_agent/router/dedup.py tests/test_session_store.py
git commit -m "feat(engine): persist opencode session map to state.db at boot"
```

---

## Deferred (out of scope — ponytail)

- **In-memory read cache** — deliberately NOT built. A PK `SELECT` is microseconds and runs once per multi-second turn; a RAM cache would add mem↔db sync for no measurable win. Add a lazy write-through dict only if profiling ever shows the per-turn `SELECT` mattering (it won't at LLM-turn cadence). `# ponytail: DB is the reuse store; cache it only if µs SELECTs ever show up in a profile.`
- **Operator-tunable maxsize** — `maxsize` is the `1024` code constant. Add a `persistence.sessionCacheMax` config field (→ CONTRACT_v3 + `gen_schema.py` regen + schema test) only if a deployment needs to tune it without a code change.
- **Age-based pruning** — the LRU count cap is the only bound. If stale mappings for long-dead session_keys ever need active cleanup (they don't hurt: rows are ~100 bytes and evicted on cap), add a `DELETE WHERE last_used < now - ttl` prune like the dedup store's.

---

## Self-Review

**1. Spec coverage.**
- "migrate session_key → opencode session map from in-memory to state.db" → Tasks 1 (`_SqliteSessionMap`) + 3 (wired at boot when `persistence.enabled`).
- "not a waste of memory / scales to 8000" → disk-resident, no in-memory copy; RAM flat regardless of row count (`test_sqlite_session_map_cap_bounds_row_count`, and no `_load`).
- "with a max size" → `maxsize` (1024) bounds the table via LRU-by-last_used; an actively-read key survives (`test_sqlite_session_map_evicts_lru_keeps_active`).
- "read when needed / reuse / delete from db" → `get` = SELECT (+recency bump), `pop`/`__delitem__` = DELETE (`test_sqlite_session_map_pop_deletes_row`, `test_sqlite_session_map_survives_reopen`).
- "state.db" (prior change) → same `mountPath/state/state.db`, `oc_sessions` table, second WAL connection (`test_open_session_store_shares_state_db_with_dedup`).

**2. Placeholder scan.** No TBD/"handle errors"/"similar to". Every code step is complete; DB errors have explicit `try/except sqlite3.Error` handling.

**3. Type consistency.** `_SqliteSessionMap` is a real `MutableMapping[str, str]` (implements `__getitem__/__setitem__/__delitem__/__iter__/__len__`); `run_invocation`'s `oc_sessions: MutableMapping[str, str] | None` is unchanged. `pop(key, default=None) -> str | None` matches `pool.oc_sessions.pop(conv_key, None)` at `main.py:710`; `get(key, default=None) -> str | None` matches `sessions.get(session_id)` at `lifecycle.py:584`. `EnginePool` default stays `_LRUSessionMap` (keeps `test_pool_owns_oc_sessions_map` green).

**4. Constraint check.** `pool.py` imports only stdlib + structlog (no router/config/main). No schema/CONTRACT change. `get` returns default on SELECT failure and writes swallow errors → never breaks a turn. `busy_timeout` on both connections covers the now-shared file.
