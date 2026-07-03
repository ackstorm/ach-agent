# Persistent session_key→oc_session Map Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `channel.session: auto` survive idle-TTL opencode server restarts by moving the `session_key → ses_…` map from the per-process `ManagedServer` to a pool-owned LRU, with a 404 stale-id fallback.

**Architecture:** opencode persists sessions in SQLite under the shared home (`~/.local/share/opencode/opencode.db`), so a fresh `opencode serve` process accepts `POST /session/{old_id}/message` → 200 (live-verified on opencode 1.17.13; unknown id → clean 404 `NotFoundError`, NOT the 500 an outdated code comment claims). Today the `session_key → ses_` link lives only in `ManagedServer._sessions` (RAM), which dies with the subprocess on idle-TTL expiry — so every cold start mints a new session even with `reuse=True`. Fix: a bounded LRU (`collections.OrderedDict` subclass, stdlib) owned by `EnginePool` (process lifetime), threaded into `run_invocation` as an optional parameter; on reuse of a cached id that 404s, mint a fresh session, overwrite the map entry, retry once.

**Tech Stack:** Python 3.12, asyncio, aiohttp (existing), pytest + pytest-asyncio (`asyncio_mode=auto`), uv, ruff, mypy --strict.

## Global Constraints

- Virtual environment only — run everything via `uv run …`, never system pip (project rule).
- `mypy --strict` and `ruff` must stay green (project CI).
- No router or Hermes imports in `engine/` modules (D-08, RTR-06 — stated at top of `pool.py` and `lifecycle.py`).
- `reuse=False` (`channel.session: none`) must NEVER read or write any session map (existing contract, tested by `test_run_invocation_reuse_false_always_creates_fresh_session`).
- The `ek_` bearer is never logged; the new log lines only carry `session_key` / `oc_session_id` / booleans — no secrets.
- Surgical changes: do not refactor adjacent code; existing tests must keep passing unmodified except the three fake pools listed in Task 4.
- LRU bound is a module constant (maxsize=256), NOT config — YAGNI.

## Verified Facts (do not re-derive)

- `POST /session` returns `{"id": "ses_…", …}` (`client.py:159-167`).
- `POST /session/{id}/message` on a fresh process with a persisted id → **200**; unknown id → **404** `aiohttp.ClientResponseError` via `resp.raise_for_status()` (`client.py:177-182`), surfaced out of `consume_sse_after_send` through its `_SendFailed → raise item.original` path (`lifecycle.py:721-722`).
- `run_invocation` is called from exactly ONE production site: `engine_runner` in `main.py` (~line 569), all-kwargs.
- `_make_engine_runner` imports `run_invocation` inside the factory body (`main.py:461`), so tests patch `ach_agent.engine.lifecycle.run_invocation` and build the runner INSIDE the patch context (pattern: `tests/test_memory_templating.py:63-70`).

---

### Task 1: `_LRUSessionMap` + `EnginePool.oc_sessions`

**Files:**
- Modify: `src/ach_agent/engine/pool.py` (imports ~line 27; `EnginePool.__init__` ~line 60)
- Test: `tests/engine/test_pool.py` (append at end)

**Interfaces:**
- Produces: `class _LRUSessionMap(OrderedDict[str, str])` with `__init__(maxsize: int = 256)`, LRU-refreshing `get(key, default=None)`, evicting `__setitem__`; and `EnginePool.oc_sessions: _LRUSessionMap` (public attribute, created empty in `__init__`). Task 2 consumes it as a `MutableMapping[str, str]` (only `.get()` and `[key] = value` are used). Task 4 passes `pool.oc_sessions` into `run_invocation`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_pool.py`:

```python
# ---------------------------------------------------------------------------
# _LRUSessionMap + EnginePool.oc_sessions (persistent session_key → ses_ map)
# ---------------------------------------------------------------------------


def test_lru_session_map_evicts_oldest() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    m["a"] = "ses-a"
    m["b"] = "ses-b"
    m["c"] = "ses-c"  # exceeds maxsize → evicts "a" (oldest)
    assert "a" not in m
    assert m.get("b") == "ses-b"
    assert m.get("c") == "ses-c"


def test_lru_session_map_get_refreshes_recency() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    m["a"] = "ses-a"
    m["b"] = "ses-b"
    assert m.get("a") == "ses-a"  # touch "a" → "b" is now oldest
    m["c"] = "ses-c"
    assert "a" in m
    assert "b" not in m


def test_lru_session_map_get_missing_returns_default() -> None:
    from ach_agent.engine.pool import _LRUSessionMap

    m = _LRUSessionMap(maxsize=2)
    assert m.get("nope") is None
    assert m.get("nope", "fallback") == "fallback"


def test_pool_owns_oc_sessions_map() -> None:
    from ach_agent.engine.pool import EnginePool, _LRUSessionMap

    pool = EnginePool()
    assert isinstance(pool.oc_sessions, _LRUSessionMap)
    assert len(pool.oc_sessions) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_pool.py -q -k "lru or oc_sessions"`
Expected: FAIL — `ImportError: cannot import name '_LRUSessionMap'`

- [ ] **Step 3: Implement**

In `src/ach_agent/engine/pool.py`, extend the stdlib import block (currently `import asyncio` at line 27):

```python
import asyncio
from collections import OrderedDict
```

Add the class immediately after `log = structlog.get_logger(__name__)` (line 37), before `class EnginePool`:

```python
class _LRUSessionMap(OrderedDict[str, str]):
    """Bounded LRU of session_key → opencode session id (``ses_…``).

    Owned by EnginePool (process lifetime) so ``channel.session: auto`` keeps
    conversational continuity across idle-TTL server restarts: opencode persists
    sessions in SQLite under the shared home, and a fresh ``opencode serve``
    accepts POSTs to a previously created id (verified on 1.17.13; stale id →
    404, handled by run_invocation's recreate-and-retry guard).
    """

    def __init__(self, maxsize: int = 256) -> None:
        super().__init__()
        self.maxsize = maxsize

    def get(self, key: str, default: str | None = None) -> str | None:
        if key not in self:
            return default
        self.move_to_end(key)
        return super().__getitem__(key)

    def __setitem__(self, key: str, value: str) -> None:
        super().__setitem__(key, value)
        self.move_to_end(key)
        while len(self) > self.maxsize:
            self.popitem(last=False)
```

In `EnginePool.__init__` (line 60), add after `self._locks: dict[str, asyncio.Lock] = {}`:

```python
        # session_key → opencode session id (ses_…). Pool-owned so it outlives
        # individual ManagedServers: channel.session='auto' reuses the persisted
        # opencode session across idle-TTL restarts (threaded into run_invocation
        # by engine_runner as oc_sessions).
        self.oc_sessions = _LRUSessionMap()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_pool.py -q`
Expected: PASS (all — new 4 plus the existing pool suite untouched)

- [ ] **Step 5: Lint + typecheck the touched file**

Run: `uv run ruff check src/ach_agent/engine/pool.py tests/engine/test_pool.py && uv run mypy src/ach_agent/engine/pool.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/pool.py tests/engine/test_pool.py
git commit -m "feat(engine): add pool-owned LRU map of session_key to opencode session id"
```

---

### Task 2: `run_invocation(oc_sessions=…)` + session-resolution log line

**Files:**
- Modify: `src/ach_agent/engine/lifecycle.py` (imports line 27; `run_invocation` signature ~line 482; session-resolution block ~lines 512-529)
- Test: `tests/engine/test_lifecycle.py` (append after `test_run_invocation_reuse_true_reuses_session`, ~line 765)

**Interfaces:**
- Consumes: nothing new (Task 1's map is only wired in Task 4; here the param accepts any `MutableMapping[str, str]`).
- Produces: `run_invocation(..., oc_sessions: MutableMapping[str, str] | None = None)` — when provided, it is used INSTEAD of `server._sessions` for the reuse read/write; when `None`, behavior is exactly today's (per-server map), keeping all existing tests green. Also produces helper `async def _create_oc_session(client: OpenCodeClient) -> str` (module-level, used again by Task 3) and the log event `"engine: opencode session"` with keys `session_key`, `oc_session_id`, `reused`. Task 3 relies on the local variables `sessions`, `reused`, `oc_session_id` existing exactly as named below.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_lifecycle.py` (the file already imports `AsyncMock`, `MagicMock`, `patch` from `unittest.mock`):

```python
# ---------------------------------------------------------------------------
# pool-owned session map: run_invocation(oc_sessions=...)
# ---------------------------------------------------------------------------


def _server_with_client(port: int, create_session: object) -> "ManagedServer":  # noqa: F821
    """Fresh ManagedServer wired to a mock client (simulates one opencode process)."""
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.create_session = create_session
    server = ManagedServer(port=port)
    mock_proc = MagicMock()
    mock_proc.returncode = None
    server._process = mock_proc
    server._client = mock_client
    return server


async def test_run_invocation_pool_map_survives_server_replacement() -> None:
    """The core feature: a NEW ManagedServer (idle-TTL restart) reuses the opencode
    session id cached in the pool-owned map by the previous server — create_session
    is never called on the second server."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {}

    server1 = _server_with_client(
        19885, AsyncMock(return_value={"id": "ses-persist"})
    )
    server2 = _server_with_client(
        19886, AsyncMock(return_value={"id": "ses-WRONG-never-created"})
    )

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ) as mock_consume:
        await run_invocation(
            server=server1,
            session_id="key-a",
            prompt="first",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )
        # server1 died (TTL); server2 is the replacement — same map, no create.
        await run_invocation(
            server=server2,
            session_id="key-a",
            prompt="second",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    assert shared_map == {"key-a": "ses-persist"}
    server1._client.create_session.assert_awaited_once()
    server2._client.create_session.assert_not_awaited()
    # consume_sse_after_send(client, oc_session_id, prompt, ...) — positional arg 1
    assert mock_consume.call_args_list[0].args[1] == "ses-persist"
    assert mock_consume.call_args_list[1].args[1] == "ses-persist"


async def test_run_invocation_reuse_false_ignores_pool_map() -> None:
    """reuse=False (channel.session='none') never reads nor writes the pool map."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-cached"}
    server = _server_with_client(19887, AsyncMock(return_value={"id": "ses-fresh"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ) as mock_consume:
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=False,
            oc_sessions=shared_map,
        )

    server._client.create_session.assert_awaited_once()  # fresh, not cached
    assert mock_consume.call_args_list[0].args[1] == "ses-fresh"
    assert shared_map == {"key-a": "ses-cached"}  # untouched


async def test_run_invocation_logs_oc_session(capfd) -> None:
    """Every turn logs which opencode session is used and whether it was reused."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {}
    server = _server_with_client(19888, AsyncMock(return_value={"id": "ses-log-1"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        await run_invocation(
            server=server,
            session_id="key-log",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    out, err = capfd.readouterr()
    combined = out + err
    assert "engine: opencode session" in combined
    assert "ses-log-1" in combined
    assert "reused" in combined
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_lifecycle.py -q -k "pool_map or ignores_pool or logs_oc"`
Expected: FAIL — `TypeError: run_invocation() got an unexpected keyword argument 'oc_sessions'`

- [ ] **Step 3: Implement**

In `src/ach_agent/engine/lifecycle.py`:

(a) Line 27, extend the abc import:

```python
from collections.abc import Callable, MutableMapping
```

(a2) Extend the `TYPE_CHECKING` block (~line 34) so mypy resolves the helper's string annotation:

```python
if TYPE_CHECKING:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.events import OpenCodeToolUpdate
```

(b) Add the helper immediately BEFORE `async def run_invocation` (~line 482). The file has `from __future__ import annotations`, so the TYPE_CHECKING import from (a2) resolves the annotation:

```python
async def _create_oc_session(client: OpenCodeClient) -> str:
    """POST /session and return the new ses_… id (raise if opencode returns none)."""
    created = await client.create_session()
    oc_session_id = str(created.get("id", ""))
    if not oc_session_id:
        raise RuntimeError(f"opencode create_session returned no id: {created!r}")
    return oc_session_id
```

(c) Add the parameter to `run_invocation`'s signature, after `stats`:

```python
    stats: dict[str, Any] | None = None,
    oc_sessions: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
```

(d) Replace the whole session-resolution block (currently lines 512-529, from the `# opencode requires a session created via POST /session…` comment through the second `raise RuntimeError(...)`) with:

```python
    # opencode requires a session created via POST /session before /session/{id}/message
    # (unknown id → 404 NotFoundError, verified on 1.17.13). Map the logical session_key →
    # an opencode session id, created once per key and reused for conversational continuity.
    # oc_sessions is the pool-owned LRU: it outlives ManagedServers, and opencode persists
    # sessions on disk under the shared home, so 'auto' survives idle-TTL restarts. Falls
    # back to the per-server map when not provided (tests / ad-hoc callers).
    # When reuse=False (channel.session='none'), always create a fresh opencode session
    # and never touch the map (no read, no write).
    sessions = oc_sessions if oc_sessions is not None else server._sessions
    reused = False
    if reuse:
        cached = sessions.get(session_id)
        if cached is None:
            oc_session_id = await _create_oc_session(client)
            sessions[session_id] = oc_session_id
        else:
            oc_session_id = cached
            reused = True
    else:
        oc_session_id = await _create_oc_session(client)
    log.info(
        "engine: opencode session",
        session_key=session_id,
        oc_session_id=oc_session_id,
        reused=reused,
    )
```

- [ ] **Step 4: Run the full lifecycle suite to verify new AND existing tests pass**

Run: `uv run pytest tests/engine/test_lifecycle.py -q`
Expected: PASS — including the two pre-existing reuse-policy tests (`reuse_false_always_creates_fresh_session`, `reuse_true_reuses_session`), which exercise the `oc_sessions=None` → `server._sessions` fallback.

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py && uv run mypy src/ach_agent/engine/lifecycle.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py
git commit -m "feat(engine): run_invocation accepts pool-owned session map, logs oc session id"
```

---

### Task 3: 404 stale-session guard (recreate + retry once)

**Files:**
- Modify: `src/ach_agent/engine/lifecycle.py` (the first `consume_sse_after_send` call inside `run_invocation`, ~line 535 pre-Task-2 numbering — directly after the block Task 2 rewrote)
- Test: `tests/engine/test_lifecycle.py` (append at end)

**Interfaces:**
- Consumes: Task 2's `sessions`, `reused`, `oc_session_id` locals and `_create_oc_session(client)` helper — exact names.
- Produces: behavioral guarantee only — a reused cached id that 404s is recreated and retried exactly once; any other error (or 404 on a fresh id) propagates unchanged. Log event `"engine: cached opencode session stale — recreating"` (WARNING).

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_lifecycle.py` (reuses `_server_with_client` from Task 2):

```python
# ---------------------------------------------------------------------------
# stale cached session: 404 → recreate + retry once
# ---------------------------------------------------------------------------


def _client_response_error(status: int) -> "aiohttp.ClientResponseError":  # noqa: F821
    import aiohttp

    return aiohttp.ClientResponseError(
        request_info=MagicMock(), history=(), status=status
    )


async def test_stale_cached_session_recreated_and_retried() -> None:
    """Cached id 404s on the (new) server → mint fresh session, overwrite map, retry once."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-stale"}
    server = _server_with_client(19889, AsyncMock(return_value={"id": "ses-new"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=[_client_response_error(404), canned],
    ) as mock_consume:
        result = await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
        )

    assert shared_map == {"key-a": "ses-new"}
    server._client.create_session.assert_awaited_once()
    assert mock_consume.call_args_list[0].args[1] == "ses-stale"
    assert mock_consume.call_args_list[1].args[1] == "ses-new"
    assert result["action"] == "none"


async def test_404_on_fresh_session_propagates() -> None:
    """A fresh (just-created) id cannot be 'stale' — 404 propagates, no retry loop."""
    import aiohttp

    from ach_agent.engine.lifecycle import run_invocation

    shared_map: dict[str, str] = {}
    server = _server_with_client(19890, AsyncMock(return_value={"id": "ses-x"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=_client_response_error(404),
    ) as mock_consume:
        with pytest.raises(aiohttp.ClientResponseError):
            await run_invocation(
                server=server,
                session_id="key-a",
                prompt="p",
                terminal_retries=1,
                reuse=True,
                oc_sessions=shared_map,
            )

    assert len(mock_consume.call_args_list) == 1  # no retry


async def test_non_404_error_on_reused_session_propagates() -> None:
    """Only 404 triggers the stale-recreate path; a 500 on a reused id propagates."""
    import aiohttp

    from ach_agent.engine.lifecycle import run_invocation

    shared_map: dict[str, str] = {"key-a": "ses-cached"}
    server = _server_with_client(19891, AsyncMock(return_value={"id": "ses-x"}))

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=_client_response_error(500),
    ):
        with pytest.raises(aiohttp.ClientResponseError):
            await run_invocation(
                server=server,
                session_id="key-a",
                prompt="p",
                terminal_retries=1,
                reuse=True,
                oc_sessions=shared_map,
            )

    assert shared_map == {"key-a": "ses-cached"}  # map NOT overwritten
```

Note: `tests/engine/test_lifecycle.py` already imports `pytest` (it contains `pytest.raises` usage; if not present at top, add `import pytest`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_lifecycle.py -q -k "stale or propagates"`
Expected: `test_stale_cached_session_recreated_and_retried` FAILS (the 404 propagates instead of being retried). The two propagation tests may already pass — that's expected; they pin current behavior.

- [ ] **Step 3: Implement**

In `run_invocation`, the current first consume call is:

```python
    stats = stats if stats is not None else {}
    accumulated_text = await consume_sse_after_send(
        client,
        oc_session_id,
        prompt,
        on_text=on_text,
        on_tool=on_tool,
        is_alive=server.is_alive,
        max_tool_calls=max_tool_calls,
        stats=stats,
    )
```

Replace with (add `import aiohttp` next to the existing lazy `from ach_agent.engine.client import OpenCodeClient` import at the top of `run_invocation`, ~line 506):

```python
    stats = stats if stats is not None else {}
    try:
        accumulated_text = await consume_sse_after_send(
            client,
            oc_session_id,
            prompt,
            on_text=on_text,
            on_tool=on_tool,
            is_alive=server.is_alive,
            max_tool_calls=max_tool_calls,
            stats=stats,
        )
    except aiohttp.ClientResponseError as exc:
        # Stale cached id: opencode pruned the session (e.g. home wiped between
        # restarts) → POST /session/{id}/message 404s. Mint a fresh session, update
        # the map, retry ONCE. A fresh id can't be stale and other statuses aren't
        # staleness — propagate everything else.
        if not (reused and exc.status == 404):
            raise
        log.warning(
            "engine: cached opencode session stale — recreating",
            session_key=session_id,
            oc_session_id=oc_session_id,
        )
        oc_session_id = await _create_oc_session(client)
        sessions[session_id] = oc_session_id
        accumulated_text = await consume_sse_after_send(
            client,
            oc_session_id,
            prompt,
            on_text=on_text,
            on_tool=on_tool,
            is_alive=server.is_alive,
            max_tool_calls=max_tool_calls,
            stats=stats,
        )
```

- [ ] **Step 4: Run the full lifecycle suite**

Run: `uv run pytest tests/engine/test_lifecycle.py -q`
Expected: PASS (all)

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py && uv run mypy src/ach_agent/engine/lifecycle.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py
git commit -m "feat(engine): recreate and retry once when a cached opencode session 404s"
```

---

### Task 4: Wire `pool.oc_sessions` into `engine_runner` + full verification

**Files:**
- Modify: `src/ach_agent/main.py` (the `run_invocation(...)` call inside `engine_runner`, ~line 569-580)
- Modify: `tests/test_memory_templating.py:22-34` (`_CapturingPool`)
- Modify: `tests/conformance/test_inv13_egress_via_mcp.py:~30-40` (fake pool with `async def acquire` at line 35)
- Modify: `tests/e2e/test_a2a_e2e.py:~170-185` (fake pool with `async def acquire` at line 178)
- Test: `tests/test_main_wiring.py` (append at end)

**Interfaces:**
- Consumes: `EnginePool.oc_sessions` (Task 1), `run_invocation(oc_sessions=…)` (Task 2).
- Produces: production wiring — every channel invocation shares the pool-lifetime map. All three test fake pools gain an `oc_sessions` dict attribute so `engine_runner` can read it.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_main_wiring.py` (check the file's existing imports; it already has `pytest`, `AsyncMock`/`patch` from `unittest.mock`, and `MessageEvent` — add any of these that are missing, plus `from ach_agent.engine.lifecycle import EngineConfig` and `from ach_agent.main import _make_engine_runner` if not present):

```python
async def test_engine_runner_passes_pool_oc_sessions_to_run_invocation() -> None:
    """engine_runner threads the pool-owned session map into run_invocation."""
    from typing import Any

    import ach_agent.engine.lifecycle as lifecycle
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.main import _make_engine_runner

    class _Pool:
        def __init__(self) -> None:
            self.oc_sessions: dict[str, str] = {}

        async def acquire(self, _session_key: str, _cfg: Any) -> Any:
            return object()

        async def release(self, _session_key: str, ttl_seconds: float = 0.0) -> None:
            return None

    pool = _Pool()
    captured: dict[str, Any] = {}

    async def _fake_run(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        return {"action": "none", "text": ""}

    event = MessageEvent(
        idempotency_key="k1",
        session_key="sess-1",
        channel_name="cron-x",
        payload={},
        delivery_context={},
        source_trait="async_no_retry",
    )

    with patch.object(lifecycle, "run_invocation", new=AsyncMock(side_effect=_fake_run)):
        runner = _make_engine_runner(
            pool=pool,
            engine_cfg=EngineConfig(),
            max_invocation_seconds=30,
        )
        await runner(event, lambda: None)

    assert captured["oc_sessions"] is pool.oc_sessions
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_wiring.py -q -k oc_sessions`
Expected: FAIL — `KeyError: 'oc_sessions'` (engine_runner does not pass it yet)

- [ ] **Step 3: Implement the wiring**

In `src/ach_agent/main.py`, the `run_invocation` call inside `engine_runner` (~line 569) currently ends:

```python
                reuse=reuse,
                max_tool_calls=max_tool_calls,
                stats=turn_stats,
            )
```

Add one kwarg:

```python
                reuse=reuse,
                max_tool_calls=max_tool_calls,
                stats=turn_stats,
                oc_sessions=pool.oc_sessions,
            )
```

- [ ] **Step 4: Update the three fake pools (they lack `oc_sessions` → AttributeError)**

`tests/test_memory_templating.py` — in `_CapturingPool.__init__` (line ~27):

```python
    def __init__(self) -> None:
        self.acquired_cfgs: list[EngineConfig] = []
        self.oc_sessions: dict[str, str] = {}
```

`tests/conformance/test_inv13_egress_via_mcp.py` — the fake pool class containing `async def acquire` at line 35: add to its `__init__` (or as a class attribute if it has no `__init__`):

```python
    oc_sessions: dict[str, str] = {}
```

(If the class has instance state already, prefer `self.oc_sessions: dict[str, str] = {}` in `__init__` — match whichever style the class body uses.)

`tests/e2e/test_a2a_e2e.py` — same treatment for the fake pool class containing `async def acquire` at line 178.

- [ ] **Step 5: Run the affected suites**

Run: `uv run pytest tests/test_main_wiring.py tests/test_memory_templating.py tests/conformance/test_inv13_egress_via_mcp.py tests/e2e/test_a2a_e2e.py -q`
Expected: PASS (all)

- [ ] **Step 6: Full-repo verification**

Run: `uv run pytest -q && uv run ruff check src/ tests/ && uv run mypy src/ach_agent`
Expected: full suite PASS, ruff clean, mypy clean

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_wiring.py tests/test_memory_templating.py tests/conformance/test_inv13_egress_via_mcp.py tests/e2e/test_a2a_e2e.py
git commit -m "feat(engine): wire pool-owned oc session map into engine_runner"
```

---

## Manual smoke check (post-implementation, optional but recommended)

With a local cron config (`every5`-style) and `engine.idle_ttl_seconds` shorter than the cron interval, run the harness and confirm in the logs across two ticks:

1. Tick 1: `engine: opencode session … oc_session_id=ses_X reused=False`
2. `EnginePool._expire: TTL elapsed — stopping`
3. Tick 2 (new pid/port): `engine: opencode session … oc_session_id=ses_X reused=True` — **same `ses_X`, `reused=True`** despite the fresh process, and `input_tokens` grows (history accumulating).
