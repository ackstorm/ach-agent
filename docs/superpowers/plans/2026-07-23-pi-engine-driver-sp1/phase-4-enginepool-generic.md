# Phase 4 — `EnginePool` generic over the driver

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first.

**Goal:** Relocate `pool.py` → `engine/base/pool.py`, make `EnginePool` construct servers via an injected `EngineDriver` (default `OpencodeDriver`), and wrap the pool-owned sessions map in a transparent **engine-type-namespaced** view so opencode `ses_…` ids and Pi session-file paths can never collide on a persisted home (§5.4). Rename `pool.oc_sessions` → `pool.sessions` (keep `oc_sessions` as an alias to avoid mid-migration churn). Keep `engine/pool.py` as a re-export shim.

**Exit criterion:** `tests/engine/test_pool.py` + new namespacing test + `make conformance` green; `main.py`'s `EnginePool(oc_sessions=session_store)` still works unchanged (driver defaults to opencode).

**Files:**
- Move: `src/ach_agent/engine/pool.py` → `src/ach_agent/engine/base/pool.py`
- Create: `src/ach_agent/engine/pool.py` (shim)
- Modify (inside `base/pool.py`): add `_NamespacedSessionMap`, driver injection, `sessions`/`oc_sessions`
- Modify: `tests/engine/test_pool.py` (constructor + any raw-key assertion)
- Create: `tests/engine/base/test_pool_namespacing.py`

**Interfaces:**
- Consumes: Phases 1 (`EngineConfig`, `EngineDriver`), 3 (`OpencodeDriver`).
- Produces (consumed by Phase 6/8): `EnginePool(driver: EngineDriver | None = None, sessions_map=None, *, oc_sessions=None)`; `pool.sessions` (the engine-namespaced `MutableMapping[str, str]` passed to `run_contract_turn`); `pool.oc_sessions` alias.

**Behavior notes:**
1. The reuse gate in `acquire` stays `existing.is_alive()` (cheap process-alive; correct for both engines — `ManagedServer` wraps the Pi subprocess too). `driver.health()` (opencode HTTP ping / Pi RPC roundtrip) is for explicit readiness, not the hot reuse path.
2. `_start_server` indirection is kept (default `= self._driver.launch`) so existing tests that override `pool._start_server = fake` are unaffected.
3. Namespacing is transparent: `run_turn`/`engine_runner` use the **bare** `conv_key`; the wrapper prefixes with `"<engine_type>:"` in the underlying store. The underlying store's LRU `maxsize` now counts all-prefix rows (dead cross-engine rows count toward the 1024 bound — negligible; documented).

---

### Task 4.1: Relocate + generalize `EnginePool`

- [ ] **Step 1: Write the failing test**

Create `tests/engine/base/test_pool_namespacing.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ach_agent.engine.base.pool import EnginePool, _NamespacedSessionMap


def test_namespaced_map_prefixes_underlying_store() -> None:
    store: dict[str, str] = {}
    ns = _NamespacedSessionMap(store, "opencode")
    ns["k1"] = "ses_1"
    assert ns.get("k1") == "ses_1"        # transparent to the caller (bare key)
    assert store == {"opencode:k1": "ses_1"}  # prefixed in the underlying store
    assert ns.pop("k1") == "ses_1"
    assert store == {}


def test_pool_sessions_is_namespaced_by_driver_engine_type() -> None:
    store: dict[str, str] = {}

    class _Piish:
        engine_type = "pi"

    pool = EnginePool(driver=_Piish(), sessions_map=store)
    pool.sessions["c"] = "/sessions/abc.json"
    assert store == {"pi:c": "/sessions/abc.json"}
    assert pool.oc_sessions is pool.sessions   # back-compat alias


def test_pool_defaults_to_opencode_driver() -> None:
    pool = EnginePool()
    assert pool._driver.engine_type == "opencode"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/base/test_pool_namespacing.py -q`
Expected: FAIL — `ModuleNotFoundError: …engine.base.pool`.

- [ ] **Step 3: Move `pool.py` and shim the old path**

```bash
git mv src/ach_agent/engine/pool.py src/ach_agent/engine/base/pool.py
```

Create `src/ach_agent/engine/pool.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: EnginePool + session maps moved to engine/base/pool.py (SP1)."""
from ach_agent.engine.base.pool import (  # noqa: F401
    EnginePool,
    _LRUSessionMap,
    _NamespacedSessionMap,
    _SqliteSessionMap,
)
```

- [ ] **Step 4: Add `_NamespacedSessionMap` to `base/pool.py`**

Insert after `_SqliteSessionMap` (before `class EnginePool`):

```python
class _NamespacedSessionMap(MutableMapping[str, str]):
    """A transparent per-engine-type view over a shared session store (SP1 §5.4).

    Every key the caller uses (the bare conv_key) is stored under ``"<engine_type>:<key>"`` in
    the underlying map, so an opencode ``ses_…`` id and a Pi session-file path can never collide
    on a persisted home whose ``engine.type`` flipped between runs. The pool wraps its store in
    this so ``run_turn``/``engine_runner`` keep using bare keys."""

    def __init__(self, inner: MutableMapping[str, str], engine_type: str) -> None:
        self._inner = inner
        self._prefix = f"{engine_type}:"

    def _k(self, key: str) -> str:
        return f"{self._prefix}{key}"

    def __getitem__(self, key: str) -> str:
        return self._inner[self._k(key)]

    def get(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return self._inner.get(self._k(key), default)

    def __setitem__(self, key: str, value: str) -> None:
        self._inner[self._k(key)] = value

    def __delitem__(self, key: str) -> None:
        del self._inner[self._k(key)]

    def pop(self, key: str, default: str | None = None) -> str | None:  # type: ignore[override]
        return self._inner.pop(self._k(key), default)

    def __iter__(self) -> Iterator[str]:
        n = len(self._prefix)
        return (k[n:] for k in self._inner if k.startswith(self._prefix))

    def __len__(self) -> int:
        return sum(1 for k in self._inner if k.startswith(self._prefix))

    def close(self) -> None:
        inner_close = getattr(self._inner, "close", None)
        if callable(inner_close):
            inner_close()
```

`Iterator` is already imported in `base/pool.py` (`from collections.abc import Awaitable, Callable, Iterator, MutableMapping`).

- [ ] **Step 5: Rewrite `EnginePool.__init__` for driver injection + namespacing**

Replace `EnginePool.__init__` (currently `def __init__(self, oc_sessions=None)`) with:

```python
    def __init__(
        self,
        driver: EngineDriver | None = None,
        sessions_map: MutableMapping[str, str] | None = None,
        *,
        oc_sessions: MutableMapping[str, str] | None = None,
    ) -> None:
        from ach_agent.engine.opencode.driver import OpencodeDriver

        self._servers: dict[str, ManagedServer] = {}
        self._ref_counts: dict[str, int] = {}
        self._ttl_tasks: dict[str, asyncio.Task[None]] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._driver: EngineDriver = driver if driver is not None else OpencodeDriver()

        # Pool-owned session store, wrapped in a per-engine-type namespaced view (SP1 §5.4).
        # `sessions_map` (or the legacy `oc_sessions` kwarg) is the raw backing store (SQLite
        # when persistence.enabled, else the in-memory LRU); `self.sessions` is what callers use.
        inner = sessions_map if sessions_map is not None else oc_sessions
        if inner is None:
            inner = _LRUSessionMap()
        self.sessions: MutableMapping[str, str] = _NamespacedSessionMap(inner, self._driver.engine_type)

        # _start_server is injectable for testing; production default is the driver's launch
        # (which does mkdir + find_free_port + serve + readiness, for opencode).
        self._start_server: Callable[[EngineConfig, str], Awaitable[ManagedServer]] = (
            self._driver.launch
        )

    @property
    def oc_sessions(self) -> MutableMapping[str, str]:
        """Deprecated alias for `sessions` (SP1 renamed it; kept to avoid churn)."""
        return self.sessions
```

Add `EngineDriver` to the `TYPE_CHECKING` import block at the top of `base/pool.py`:

```python
if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineConfig, EngineDriver
    from ach_agent.engine.lifecycle import ManagedServer
```

- [ ] **Step 6: Point teardown at `driver.stop`; remove the dead module helper**

In `acquire` (dead-server branch), `release`(ttl==0), `_expire`, `_stop`, `stop_all`: replace `await existing.stop()` / `await server.stop()` with `await self._driver.stop(existing)` / `await self._driver.stop(server)`. (`OpencodeDriver.stop` just calls `server.stop()`, so behavior is identical.) Delete the now-unused module-level `async def _default_start_server(...)` at the bottom of the file (your changes orphaned it; `_start_server` now defaults to `self._driver.launch`). If a test imports `_default_start_server`, keep a thin re-export instead — verify with `grep -rn _default_start_server tests/`.

- [ ] **Step 7: Update `test_pool.py` construction**

Where `test_pool.py` builds the pool and overrides the launcher, ensure it still passes a driver-less pool (default opencode) and overrides `_start_server`:

```python
# Existing pattern still works — driver defaults to OpencodeDriver, _start_server is overridden:
pool = EnginePool()
pool._start_server = fake_start_server   # (config, session_key) -> ManagedServer
```

For any assertion that read the **raw** backing store by bare key, switch to the map API (`pool.sessions.get(key)` / `pool.oc_sessions.get(key)` — namespaced-transparent) or expect the `"opencode:"` prefix. Run the suite (Step 8) to surface them.

- [ ] **Step 8: Run pool + full suite + type-check + conformance**

Run: `uv run pytest tests/engine/base/test_pool_namespacing.py tests/engine/test_pool.py -q`
Expected: PASS.

Run: `uv run pytest tests/ -q && uv run mypy --strict src/ach_agent/engine/ && make conformance`
Expected: all PASS. (`main.py:1420 EnginePool(oc_sessions=session_store)` resolves via the shim + the `oc_sessions` kwarg; driver defaults to opencode — behavior identical.)

- [ ] **Step 9: Commit**

```bash
git add -A src/ach_agent/engine/ tests/engine/
git commit -m "refactor(engine): EnginePool generic over driver + engine-type-namespaced sessions (base/pool.py)"
```

---

## Self-review (Phase 4)

- **Spec coverage:** §4.2 `EnginePool.__init__(driver, sessions_map)`, `oc_sessions`→`sessions`, launch/stop via the driver, SQLite table name unchanged (`_SqliteSessionMap` untouched). §5.4 engine-type namespacing via `_NamespacedSessionMap`. All present.
- **Placeholders:** none — full `_NamespacedSessionMap`, full `__init__`, explicit teardown edit, explicit test-construction guidance.
- **Behavior preservation:** default opencode driver + `_start_server` override hook + `oc_sessions` kwarg/alias keep `main.py` and `test_pool.py` working without Phase-6 changes. Reuse gate stays `is_alive()` (note 1).
- **Type consistency:** `pool.sessions` is the `MutableMapping[str, str]` Phase 6 passes to `run_contract_turn(sessions=pool.sessions)`; `EnginePool(driver=…)` matches the Phase 6 selection call.
- **Documented tradeoff:** cross-engine dead rows count toward the LRU `maxsize` (negligible) and a persisted home from before SP1 loses opencode continuity once (keys gain the `opencode:` prefix) — acceptable per §5.4 ("or wipe on engine.type change").
