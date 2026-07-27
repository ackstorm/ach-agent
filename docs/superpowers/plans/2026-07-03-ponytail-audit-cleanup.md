# Ponytail Audit Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Apply the verified 2026-07-03 repo-wide over-engineering audit: delete dead code, collapse duplicated logic, and drop unused config — ~250 lines and 1 dependency, zero behavior change.

**Architecture:** Pure subtraction/refactor sweep across `src/ach_agent/`, `src/ach_stats/`, and build files. No new features. Existing test suite is the safety net; where tests reference deleted symbols, they are repointed or rewritten to assert the same invariant through a live surface.

**Tech Stack:** Python 3.12, pytest (asyncio_mode=auto), ruff, mypy --strict, uv, Docker.

## Global Constraints

- **Dirty working tree:** `git status` at plan time shows unrelated in-flight modifications (`docker-compose.yml`, `src/ach_agent/config/schema.py`, `src/ach_agent/memory/codemem.py`, `tests/e2e/test_durability_e2e.py`, `tests/integration/test_codemem_wiring.py`, `tests/test_resolve_codemem.py`, docs). **Before Task 1: STOP and ask the user to commit or stash them.** Task 3 edits `schema.py` and Task 4 edits `test_codemem_wiring.py` — both are dirty; proceeding would fold unrelated work into cleanup commits.
- Work on branch `chore/ponytail-audit-cleanup` off `main`.
- Every commit: `git add` **explicit paths only** (never `git add -A` / `git add .`).
- Every task must leave `uv run ruff check src/ tests/ && uv run ruff format --check src/ tests/` clean and its scoped tests green. `mypy --strict` on touched `src/ach_agent/` modules.
- Router is the repo's IP: Task 5 preserves the `dedup → backpressure → lane` order and all three bounds exactly. Only representation changes (counter class → int) and log-call dedup are allowed.
- Decisions locked with the user (2026-07-03): **keep** the a2a egress scaffold block in `main.py` (planned Plan 3/4 hosting work), **cut** `MessageEvent.user_consented` (D-03 throwaway), **keep** `_StreamEntry` Protocol and `ManagedServer._sessions` fallback (rejected findings).
- Commit messages: conventional, `chore:`/`refactor:` prefixes, <72-char subject.

---

### Task 1: engine/a2a_egress.py — delete A2ANotificationStore, wait via client polling

**Files:**
- Modify: `src/ach_agent/engine/a2a_egress.py` (delete class ~lines 43–100; edit `build_a2a_tools` ~289–330; edit `_make_agent_tools` ~326–378)
- Test: `tests/engine/test_a2a_egress.py`

**Interfaces:**
- Consumes: existing `A2AAgentClient.wait_task(task_id, timeout=300.0, poll_interval=2.0) -> dict` (a2a_egress.py:249) — returns `{"task_id", "status", "result", "error"}`; on timeout `status == "timeout"`.
- Produces: `build_a2a_tools(agents, ek=None, client_factory=None) -> list[ToolSpec]` (the `_store` kwarg is GONE); `_make_agent_tools(name, client) -> list[ToolSpec]`. `main.py:1137`'s call `build_a2a_tools(manifest.a2a_agents, ek=ek)` is unchanged.

Rationale: `notify_completion` has zero prod callers (the HTTP push receiver was dropped), so `status(wait=True)` → `store.wait_for_task` can only ever time out in prod. `client.wait_task` (poll loop) is the working equivalent and is currently never called.

- [ ] **Step 1: Rewrite the wait-path tests to use client polling**

In `tests/engine/test_a2a_egress.py`:

1. Remove `A2ANotificationStore` from the import at the top:

```python
from ach_agent.engine.a2a_egress import (
    ToolSpec,
    build_a2a_tools,
)
```

2. Add a `wait_task` method to `FakeClient` (after `get_task_status`):

```python
    async def wait_task(
        self, task_id: str, timeout: float = 300.0, poll_interval: float = 2.0
    ) -> dict[str, Any]:
        self.calls.append(("wait_task", (task_id, timeout)))
        if self.raises:
            raise self.raises
        return self.status_result
```

3. Replace `test_status_tool_wait_resolves_via_notification_store` with:

```python
async def test_status_tool_wait_polls_via_client_wait_task() -> None:
    client = FakeClient(
        status_result={
            "task_id": "t-wait",
            "status": "completed",
            "result": "done!",
            "error": None,
        }
    )
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_status").handler(
        task_id="t-wait", wait=True, timeout=5.0
    )
    assert out["ok"] is True
    assert out["status"] == "completed"
    assert out["result"] == "done!"
    assert ("wait_task", ("t-wait", 5.0)) in client.calls


async def test_status_tool_wait_timeout_maps_to_not_ok() -> None:
    client = FakeClient(
        status_result={
            "task_id": "t-slow",
            "status": "timeout",
            "result": None,
            "error": "timeout after 5.0s waiting for task",
        }
    )
    tools = build_a2a_tools([_agent("rev")], client_factory=_factory(client))
    out = await _by_name(tools, "a2a_rev_status").handler(
        task_id="t-slow", wait=True, timeout=5.0
    )
    assert out["ok"] is False
    assert "timeout" in out["error"]
```

4. Delete `test_notification_store_wait_unknown_returns_none` and `test_notification_store_register_then_notify` (bottom of file) plus the `# notification store unit behaviour` section header.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/engine/test_a2a_egress.py -q`
Expected: FAIL — `ImportError` is gone but `build_a2a_tools` still passes a store / `FakeClient.wait_task` never called (`test_status_tool_wait_polls_via_client_wait_task` fails).

- [ ] **Step 3: Delete the store, rewire status handler**

In `src/ach_agent/engine/a2a_egress.py`:

1. Delete the whole `A2ANotificationStore` section: the `# A2ANotificationStore — in-memory task → completion map (ported verbatim)` comment block and the class (lines ~43–100).
2. In `build_a2a_tools`: remove the `_store: A2ANotificationStore | None = None` parameter, the `store = _store if _store is not None else A2ANotificationStore()` line, and change the loop call to `tools.extend(_make_agent_tools(name, client))`. In its docstring: change the status-tool line to `` - ``a2a_{name}_status`` poll/wait: get_task_status | client.wait_task `` and drop the `_store:` arg doc line.
3. `_make_agent_tools(name: str, client: A2AAgentClient) -> list[ToolSpec]` — drop the `store` param. In `fire`, delete the `store.register_task(task_id)` line. Replace `status` with:

```python
    async def status(task_id: str, wait: bool = False, timeout: float = 300.0) -> dict[str, Any]:
        try:
            if wait:
                resolved = await client.wait_task(task_id, timeout=timeout)
                if resolved.get("status") == "timeout":
                    return {"ok": False, "error": resolved.get("error")}
            else:
                resolved = await client.get_task_status(task_id)
            return {
                "ok": True,
                "status": resolved.get("status"),
                "result": resolved.get("result"),
            }
        except Exception as exc:  # noqa: BLE001 — tools must never raise
            log.warning("a2a egress status call failed", agent=name, error=str(exc))
            return {"ok": False, "error": str(exc)}
```

4. Grep check: `grep -rn 'A2ANotificationStore\|register_task\|wait_for_task' src/ tests/` must return zero hits.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/engine/test_a2a_egress.py -q && uv run mypy --strict src/ach_agent/engine/a2a_egress.py`
Expected: PASS, mypy clean.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/a2a_egress.py tests/engine/test_a2a_egress.py
git commit -m "refactor(engine): drop dead A2ANotificationStore, wait via client.wait_task"
```

---

### Task 2: engine tidy — SanitizedEnv class, OnKillCallback, _trace_sse, __contains__

**Files:**
- Modify: `src/ach_agent/engine/sanitized_env.py` (delete class ~109–146 + docstring line), `src/ach_agent/engine/__init__.py`, `src/ach_agent/engine/client.py:32-42,256,277`, `src/ach_agent/engine/pool.py:167-169`, `src/ach_agent/main.py:10,52,537-539,559-560`
- Test: `tests/engine/test_sanitized_env.py`, `tests/e2e/test_skeleton.py:259-263`

**Interfaces:**
- Consumes: `redact_ek_processor` / `configure_logging` / `add_secret_redaction` in `sanitized_env.py` — these STAY (SEC-01 lives in the processors).
- Produces: `ach_agent.engine.__all__` = `["EngineConfig", "ManagedServer", "EnginePool", "run_invocation"]`. `SanitizedEnv` and `OnKillCallback` cease to exist.

Rationale: `SanitizedEnv` is dead in prod — `launch()` builds its env via `build_opencode_env()` (lifecycle.py:343,418) and never wraps it; the only prod "use" is the F841-suppressed dead local at main.py:560 whose `# used by launch` comment is false. The SEC-01 guarantee is carried by `redact_ek_processor`, which stays fully tested.

- [ ] **Step 1: Rework tests off the class**

In `tests/engine/test_sanitized_env.py`:

1. Module docstring: change first line to `"""ek_ redaction tests: SEC-01."""` (drop SanitizedEnv mentions).
2. In `test_ek_never_logged`: remove `SanitizedEnv` from the import tuple; replace the repr-logging block

```python
    # Log the SanitizedEnv repr — must not leak the sentinel
    sanitized = SanitizedEnv(env)
    log = structlog.get_logger("test")
    log.info("env repr", env_repr=repr(sanitized))
```

with

```python
    # Log the raw env dict — redact_ek_processor must scrub the sentinel
    log = structlog.get_logger("test")
    log.info("env dict", env=env)
```

3. Delete `test_sanitized_env_repr_masks_ek` and `test_sanitized_env_as_dict_returns_real_values` entirely. Keep every `redact_ek_processor` / mid-token / gitlab-token test untouched.

In `tests/e2e/test_skeleton.py` (~259–263), replace:

```python
    # Simulate what would happen if ACH_API_KEY leaked into a log call
    # (SanitizedEnv repr logs as [REDACTED]; raw ek_ value must never appear)
    from ach_agent.engine.sanitized_env import SanitizedEnv

    sanitized = SanitizedEnv(os.environ.copy())
    log.info("env check", env_repr=repr(sanitized))
```

with:

```python
    # Simulate what would happen if ACH_API_KEY leaked into a log call
    # (redact_ek_processor scrubs it; raw ek_ value must never appear)
    log.info("env check", env=os.environ.copy())
```

- [ ] **Step 2: Run reworked tests — must pass BEFORE deleting the class**

Run: `uv run pytest tests/engine/test_sanitized_env.py tests/e2e/test_skeleton.py -q`
Expected: PASS (proves the processor alone upholds SEC-01; the class was redundant).

- [ ] **Step 3: Delete the class and dead exports**

1. `src/ach_agent/engine/sanitized_env.py`: delete `class SanitizedEnv` (from `class SanitizedEnv:` through `def __str__`'s `return self.__repr__()`, ~109–146). Module docstring: first line → `"""structlog redaction processors."""`; delete the sentence `The SanitizedEnv class wraps the subprocess env dict; ` (keep the rest about the processors).
2. `src/ach_agent/engine/__init__.py`: delete `from collections.abc import Callable`, `from ach_agent.engine.sanitized_env import SanitizedEnv`, the 3-line `OnKillCallback` comment + alias, and the `"OnKillCallback",` / `"SanitizedEnv",` entries in `__all__`.
3. `src/ach_agent/main.py`:
   - Line 52: `from ach_agent.engine.sanitized_env import add_secret_redaction, configure_logging`
   - Line 10 docstring: `5. Construct Router (wraps SanitizedEnv engine launch)` → `5. Construct Router`
   - Lines ~537–539 docstring: replace `SanitizedEnv is used to build the subprocess launch env (SEC-01 / T-01-EK folded todo): the engine_cfg carries paths, never ek_ values.` with `The subprocess launch env is built by build_opencode_env (SEC-01): the engine_cfg carries paths, never ek_ values.`
   - Lines ~559–560: delete both lines (`# Build sanitized launch env — ek_ is never read into a local variable` and `_sanitized = SanitizedEnv(os.environ.copy())  # noqa: F841 — used by launch`).
4. `src/ach_agent/engine/client.py`: delete `def _trace_sse` (lines 32–42). At the first call site (~256) replace `_trace_sse(data_str)` with:

```python
                        # Raw SSE trace (DEBUG): every event, full wire — redact
                        # processors scrub any ek_/token before rendering.
                        log.debug("sse event", length=len(data_str), raw=data_str)
```

   At the second call site (~277) replace `_trace_sse(data_str)` with just `log.debug("sse event", length=len(data_str), raw=data_str)`.
5. `src/ach_agent/engine/pool.py`: delete `__contains__` (lines 167–169). First confirm `class _SqliteSessionMap(MutableMapping[str, str])` at pool.py:72 — the `MutableMapping` mixin supplies `in` via `__getitem__`.
6. Grep check: `grep -rn 'SanitizedEnv\|OnKillCallback\|_trace_sse' src/ tests/` → zero hits.

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/engine/ tests/e2e/test_skeleton.py -q && uv run mypy --strict src/ach_agent/engine/ && uv run ruff check src/ach_agent/ tests/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/sanitized_env.py src/ach_agent/engine/__init__.py src/ach_agent/engine/client.py src/ach_agent/engine/pool.py src/ach_agent/main.py tests/engine/test_sanitized_env.py tests/e2e/test_skeleton.py
git commit -m "refactor(engine): delete dead SanitizedEnv class, OnKillCallback, _trace_sse wrapper"
```

---

### Task 3: config/schema.py — data-driven type↔block coherence validator

**Files:**
- Modify: `src/ach_agent/config/schema.py:510-552`
- Test: `tests/config/test_schema.py` (existing coverage; no edits expected)

**Interfaces:**
- Produces: same validator name `check_type_block_coherence`, same `ValueError` messages byte-for-byte (tests and operators may match on them): `requires a '<t>' block` for webhook/cron/queue, `requires an 'a2a' block`, `requires 'source' field`, `forbids '<foreign>' block`.

- [ ] **Step 1: Replace the 4-branch validator**

Replace the whole method body (keep decorator + docstring) with:

```python
    @model_validator(mode="after")
    def check_type_block_coherence(self) -> ChannelConfig:
        """D-04: enforce channel type↔sub-block coherence at config load time.

        The channel type names its required sub-block (type Literal == field name,
        1:1); every other type's block is forbidden. webhook additionally requires
        'source'. Raises ValueError (wrapped by Pydantic into ValidationError →
        sys.exit(1)).
        """
        t = self.type
        if getattr(self, t) is None:
            article = "an" if t == "a2a" else "a"
            raise ValueError(f"channel '{self.name}': type='{t}' requires {article} '{t}' block")
        if t == "webhook" and self.source is None:
            raise ValueError(f"channel '{self.name}': type='webhook' requires 'source' field")
        for foreign in ("webhook", "cron", "queue", "a2a"):
            if foreign != t and getattr(self, foreign) is not None:
                raise ValueError(
                    f"channel '{self.name}': type='{t}' forbids '{foreign}' block"
                )
        return self
```

- [ ] **Step 2: Run config tests**

Run: `uv run pytest tests/config/ -q && uv run mypy --strict src/ach_agent/config/`
Expected: PASS. If any test asserts an exact message that now differs, the implementation is wrong (messages must be identical) — fix the code, not the test.

- [ ] **Step 3: Commit**

```bash
git add src/ach_agent/config/schema.py
git commit -m "refactor(config): data-driven channel type/block coherence check"
```

---

### Task 4: memory/adapter.py — delete the re-export shim

**Files:**
- Delete: `src/ach_agent/memory/adapter.py`
- Modify: `src/ach_agent/main.py:498,1288`, `tests/conformance/test_inv05_memory_fail_open.py:19`, `tests/integration/test_codemem_wiring.py:141`, `tests/test_main_memory_dispatch.py:27,46`, `tests/memory/test_memory_adapter.py:26,90,191,274`

**Interfaces:**
- Consumes: `ach_agent.memory.hindsight.prepare_memory` (hindsight.py:97) — the real implementation the shim re-exports.
- Produces: nothing new; every `ach_agent.memory.adapter.*` reference becomes `ach_agent.memory.hindsight.*`.

- [ ] **Step 1: Repoint all references**

1. `src/ach_agent/main.py` — both occurrences (498, 1288): `from ach_agent.memory.adapter import prepare_memory` → `from ach_agent.memory.hindsight import prepare_memory`.
2. `tests/conformance/test_inv05_memory_fail_open.py:19` and `tests/memory/test_memory_adapter.py:26,90` — same import swap.
3. Monkeypatch targets — `"ach_agent.memory.adapter.prepare_memory"` → `"ach_agent.memory.hindsight.prepare_memory"` in `tests/integration/test_codemem_wiring.py:141`, `tests/test_main_memory_dispatch.py:27,46`, `tests/memory/test_memory_adapter.py:191,274`. (main.py imports `prepare_memory` lazily at call time, so patching the hindsight module attribute keeps working.)
4. Delete `src/ach_agent/memory/adapter.py`.
5. Grep check: `grep -rn 'memory.adapter\|memory import adapter' src/ tests/` → zero hits.

- [ ] **Step 2: Run memory tests**

Run: `uv run pytest tests/memory/ tests/conformance/test_inv05_memory_fail_open.py tests/integration/test_codemem_wiring.py tests/test_main_memory_dispatch.py -q`
Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add -A src/ach_agent/memory/adapter.py src/ach_agent/main.py tests/conformance/test_inv05_memory_fail_open.py tests/integration/test_codemem_wiring.py tests/test_main_memory_dispatch.py tests/memory/test_memory_adapter.py
git commit -m "refactor(memory): delete adapter re-export shim, import hindsight directly"
```

---

### Task 5: router tidy — AtomicCounter → int, dead is_done, log dedup

**Files:**
- Delete: `src/ach_agent/router/admission.py`
- Modify: `src/ach_agent/router/router.py:21,58,89-90,115,117-137,144,183-191`, `src/ach_agent/router/lane.py:73-75`
- Test: `tests/router/test_router.py:32,35`, `tests/router/test_bounds.py:132,174`

**Interfaces:**
- Produces: `Router._queued_total` is now a plain `int` (read directly, no `.get()`). `Router._queued_total_dec()` keeps its name/signature (Lane calls it via on_kill). `RouterAdmitResult` values and the RTR-01..05 semantics are UNCHANGED.

- [ ] **Step 1: Update tests to read the int**

- `tests/router/test_router.py:32`: `assert router._queued_total == 1`
- `tests/router/test_router.py:35`: `assert router._queued_total == 1, "secondary duplicate MUST NOT consume a queue slot (RTR-01)"`
- `tests/router/test_bounds.py:132` and `:174`: `router._queued_total.get() == 0` → `router._queued_total == 0` (keep each assert's message).

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/router/test_router.py tests/router/test_bounds.py -q`
Expected: FAIL — `AtomicCounter` is not equal to `1`/`0` (comparing object to int).

- [ ] **Step 3: Replace the counter, collapse the logs, drop is_done**

1. Delete `src/ach_agent/router/admission.py`.
2. `src/ach_agent/router/router.py`:
   - Delete `from ach_agent.router.admission import AtomicCounter` (line 21).
   - Docstring line 58: `RTR-04: maxQueuedTotal enforced via AtomicCounter` → `RTR-04: maxQueuedTotal enforced via a plain int counter`.
   - Lines 89–90: replace comment + init with:

```python
        # Plain int is safe in single-threaded asyncio (no locks needed): inc/dec
        # only run on the event loop thread (router.handle + lane consumers).
        self._queued_total: int = 0
```

   - Line 115: `if self._queued_total >= self._max_queued_total:`
   - Lines 117–137: replace the if/else warning blocks with:

```python
            # RTR-05: full queue is NEVER silent (CONTRACT §6.4, Pitfall 3)
            dropped = event.source_trait == "async_no_retry"
            if dropped:
                EXPIRE_DROPS.inc()
            log.warning(
                "router: drop — queue full" if dropped else "router: backpressure — queue full",
                source_trait=event.source_trait,
                session_key=event.session_key,
                idempotency_key=event.idempotency_key,
                queued=self._queued_total,
                max_queued_total=self._max_queued_total,
            )
```

   - Line 144: `self._queued_total.inc()` → `self._queued_total += 1`
   - Line ~191 (`_queued_total_dec` body): `self._queued_total.dec()` → `self._queued_total -= 1`
3. `src/ach_agent/router/lane.py`: delete `is_done` (lines 73–75, def + docstring + return) — zero callers anywhere.
4. Grep check: `grep -rn 'AtomicCounter\|is_done\|from ach_agent.router.admission' src/ tests/` → zero hits.

- [ ] **Step 4: Run router + conformance suites**

Run: `uv run pytest tests/router/ tests/conformance/ -q && make conformance && uv run mypy --strict src/ach_agent/router/`
Expected: PASS — all 11 named CONTRACT §6 invariants green.

- [ ] **Step 5: Commit**

```bash
git add -A src/ach_agent/router/admission.py src/ach_agent/router/router.py src/ach_agent/router/lane.py tests/router/test_router.py tests/router/test_bounds.py
git commit -m "refactor(router): plain int queued_total, drop dead Lane.is_done, dedup full-queue log"
```

---

### Task 6: main.py tidy — inert loop, _A2AHandler hoist, dead guards

**Files:**
- Modify: `src/ach_agent/main.py:1374-1387,1409,1436-1461,1501-1512,1547-1552`

**Interfaces:**
- Consumes: `Router.handle(event)`, `bridge.signal_completion` / `bridge.signal_failure` (unchanged).
- Produces: module-level `class _A2AHandler` with `__init__(self, rtr, fn, fn_fail)` and `async handle(event) -> Any` — identical behavior to today's loop-local class.

Rationale: the per-channel registration loop is inert (every branch is `pass`/log; cron/queue/webhook/a2a wiring all happens above it, and the duplicate-name check runs AFTER that wiring so it prevents nothing). `tasks` always contains the unconditionally-booted uvicorn task, so both `if tasks:` guards and the `else` fallback are dead.

- [ ] **Step 1: Hoist _A2AHandler to module scope**

Add at module level (near the other module-level helpers, after `_make_engine_runner`):

```python
class _A2AHandler:
    """Router wrapper injecting on_complete/on_fail into delivery_context (W9 pattern)."""

    def __init__(self, rtr: Any, fn: Any, fn_fail: Any) -> None:
        self._rtr = rtr
        self._fn = fn
        self._fn_fail = fn_fail

    async def handle(self, event: MessageEvent) -> Any:
        event.delivery_context["on_complete"] = self._fn
        event.delivery_context["on_fail"] = self._fn_fail
        return await self._rtr.handle(event)
```

Delete the identical loop-local class definition (lines ~1374–1387, keep the `# Wrap the router...` comment above the `bridge._handler = _A2AHandler(router, _on_complete, _on_fail)` assignment).

- [ ] **Step 2: Collapse the inert registration loop**

Delete `seen_channel_names: set[str] = set()` (~1409) and the whole `for channel in cfg.channels:` block (~1436–1461: dup check + four `pass`/log branches). In their place, immediately before the `# Boot uvicorn UNCONDITIONALLY` comment, add:

```python
    log.info("channels registered", names=[ch.name for ch in cfg.channels])
```

- [ ] **Step 3: Drop the dead task guards**

1. Lines ~1501–1512: remove `if tasks:` / `else:` — keep the wait unconditional:

```python
    # Wait for SIGTERM/SIGINT OR all tasks to finish (tasks loop forever normally).
    # uvicorn boots unconditionally, so `tasks` is never empty.
    shutdown_task = asyncio.create_task(shutdown_event.wait())
    await asyncio.wait(
        [shutdown_task, *tasks],
        return_when=asyncio.FIRST_COMPLETED,
    )
```

2. Lines ~1547–1552 (inside the shutdown branch): remove the inner `if tasks:` guard, keep the comment + `await asyncio.gather(*tasks, return_exceptions=True)` at the outer indent.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/ -q --ignore=tests/e2e && uv run mypy --strict src/ach_agent/ && uv run ruff check src/ach_agent/`
Expected: PASS, clean.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/main.py
git commit -m "refactor(main): collapse inert channel loop, hoist _A2AHandler, drop dead task guards"
```

---

### Task 7: channels tidy — cron test counter, user_consented (D-03 cut)

**Files:**
- Modify: `src/ach_agent/channels/cron.py:59,66,91`, `src/ach_agent/channels/message_event.py:54-58`, `tests/channels/test_cron.py`, `tests/e2e/test_skeleton.py:215,225`
- Delete: `tests/channels/test_message_event_consent.py`

**Interfaces:**
- Produces: `CronScheduler` loses the `_instance_count` class attr (behavior identical). `MessageEvent` loses `user_consented` (no reader/writer exists in src).

- [ ] **Step 1: Rewrite the singleton test around a live surface**

In `tests/channels/test_cron.py`, replace `test_singleton_invariant` (~300–330) with:

```python
def test_single_scheduler_drives_all_channels() -> None:
    """D-09/SC#3: ONE CronScheduler drives ALL cron channels — one slot per channel."""
    from ach_agent.channels.cron import CronScheduler

    channel_c1 = _make_channel_cfg("c1", "* * * * *")
    channel_c2 = _make_channel_cfg("c2", "*/5 * * * *")
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)

    scheduler = CronScheduler([channel_c1, channel_c2], handler=handler)  # type: ignore[list-item]

    assert len(scheduler._slots) == 2, (
        f"D-09/SC#3: one scheduler holds one slot per cron channel, got {len(scheduler._slots)}"
    )
```

Delete every `CronScheduler._instance_count = 0` reset line in the file (lines 85, 93, 155, 162, 205, 212, 270, 272 — plus the two inside the old singleton test) and the two in `tests/e2e/test_skeleton.py` (215, 225).

- [ ] **Step 2: Cut the counter and the field**

1. `src/ach_agent/channels/cron.py`: delete `_instance_count: int = 0  # D-09: singleton test increments/decrements this` (~59), `CronScheduler._instance_count += 1` (~66), and `CronScheduler._instance_count -= 1` (~91); in `stop()`'s docstring drop `; decrement _instance_count for test isolation`.
2. `src/ach_agent/channels/message_event.py`: delete the D-03 comment block + field (lines 54–58):

```python
    # D-03: Phase 5 throwaway consent marker.
    # True = structured consent signal present (exercised by test fixtures only in v1).
    # ALL channel adapters leave this False in v1 — the real derivation is V1.1 CR.
    # Do NOT build per-channel consent derivation logic here.
    user_consented: bool = field(default=False)
```

3. Delete `tests/channels/test_message_event_consent.py`.
4. Grep check: `grep -rn 'user_consented\|_instance_count' src/ tests/` → zero hits.

- [ ] **Step 3: Run channel tests**

Run: `uv run pytest tests/channels/ tests/e2e/test_skeleton.py -q && uv run mypy --strict src/ach_agent/channels/`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add -A src/ach_agent/channels/cron.py src/ach_agent/channels/message_event.py tests/channels/test_cron.py tests/channels/test_message_event_consent.py tests/e2e/test_skeleton.py
git commit -m "refactor(channels): drop cron test-only instance counter and D-03 user_consented field"
```

---

### Task 8: ach_stats + build files — model_meta fold, pydantic dep, pin dedupe, Makefile alias

**Files:**
- Delete: `src/ach_stats/api/app/model_meta.py`, `src/ach_stats/api/tests/test_model_meta.py`
- Modify: `src/ach_stats/api/app/aggregate.py:14`, `src/ach_stats/api/tests/test_aggregate.py`, `src/ach_stats/api/pyproject.toml:9,29`, `docker/stats.Dockerfile:10-18`, `Makefile:60-64`

**Interfaces:**
- Produces: `app.aggregate.resolve(model: str) -> tuple[str, str | None]` (moved verbatim from `app.model_meta`).

- [ ] **Step 1: Fold model_meta into aggregate**

1. In `src/ach_stats/api/app/aggregate.py`: delete `from app.model_meta import resolve` (line 14) and add above `_safe_div`:

```python
# Static model -> (provider, tag) map. provider/tag are metadata, never measured (spec §4.4).
_META: dict[str, tuple[str, str | None]] = {
    "claude-opus-4-8": ("Anthropic", "Frontier"),
    "claude-fable-5": ("Anthropic", "Mythos-tier"),
    "claude-sonnet-5": ("Anthropic", "Balanced"),
    "glm-5-2": ("Zhipu AI", "Open Weight"),
}


def resolve(model: str) -> tuple[str, str | None]:
    return _META.get(model, ("unknown", None))
```

2. Delete `src/ach_stats/api/app/model_meta.py` and `src/ach_stats/api/tests/test_model_meta.py`; append to `src/ach_stats/api/tests/test_aggregate.py`:

```python
def test_resolve_model_meta():
    from app.aggregate import resolve

    assert resolve("claude-opus-4-8") == ("Anthropic", "Frontier")
    assert resolve("mystery-model-9") == ("unknown", None)
```

- [ ] **Step 2: Drop the unused pydantic dep + dedupe Dockerfile pins**

1. `src/ach_stats/api/pyproject.toml`: delete `"pydantic>=2,<3",` from `[project].dependencies` and `plugins = ["pydantic.mypy"]` from `[tool.mypy]` (no `pydantic` import exists in `app/` or `tests/`; FastAPI pulls it transitively).
2. `docker/stats.Dockerfile` stage 2: replace the by-name pin install with an install from the single source of truth:

```dockerfile
# Stage 2 — python deps (uv, matching the harness image's convention). Deps come from
# pyproject.toml (single source of truth — no duplicated pin list); `-r pyproject.toml`
# installs only [project.dependencies], no app build needed.
FROM python:3.12-slim AS deps
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv
COPY src/ach_stats/api/pyproject.toml ./pyproject.toml
RUN uv pip install --system --no-cache-dir --target=/app/site-packages -r pyproject.toml \
 && find /app/site-packages -type d -name "__pycache__" -prune -exec rm -rf {} +
```

3. `Makefile`: replace the `test-fast` block (lines 60–64) — it is byte-identical to `test` — with an alias:

```makefile
.PHONY: test-fast
test-fast: test ## alias of test (kept for hooks / muscle memory)
```

- [ ] **Step 3: Run ach_stats tests + stats image build**

Run: `cd src/ach_stats/api && uv run pytest tests/ -q && uv run mypy --strict app/ && cd -`
Expected: PASS.
Run: `docker build -f docker/stats.Dockerfile -t ach-stats:audit-test . && docker run --rm --entrypoint python ach-stats:audit-test -c "import fastapi, redis, uvicorn; print('ok')"`
Expected: build succeeds, prints `ok`.
Run: `make test-fast`
Expected: same output as `make test`.

- [ ] **Step 4: Commit**

```bash
git add -A src/ach_stats/api/app/model_meta.py src/ach_stats/api/app/aggregate.py src/ach_stats/api/tests/test_model_meta.py src/ach_stats/api/tests/test_aggregate.py src/ach_stats/api/pyproject.toml docker/stats.Dockerfile Makefile
git commit -m "chore(stats): fold model_meta into aggregate, drop unused pydantic dep, dedupe image pins"
```

---

### Task 9: harness Dockerfile — delete dead `COPY src/ ./src/`

**Files:**
- Modify: `Dockerfile:72`

Rationale: the builder stage runs `uv pip install --target=/app/deps .` — the project wheel is installed INTO `/app/deps`, runtime `PYTHONPATH=/app/deps`, entrypoint `python -m ach_agent.main`. `/app/src` is never importable (packages live under `src/`, not on any path), and the line drags the whole `src/ach_stats/` tree (FastAPI reader + React UI source) into the harness image.

- [ ] **Step 1: Delete the line**

In `Dockerfile`, delete the single line `COPY src/ ./src/` (line 72, right after the `RUN codemem --version` smoke check).

- [ ] **Step 2: Build and prove the harness still imports**

Run: `docker build -t ach-agent:audit-test .`
Expected: build succeeds.
Run: `docker run --rm --entrypoint python ach-agent:audit-test -c "import ach_agent.main; print('harness import ok')"`
Expected: `harness import ok`.

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "chore(docker): drop dead COPY src/ from runtime stage (wheel lives in /app/deps)"
```

---

### Task 10: full sweep — lint, mypy, full suite, conformance

**Files:**
- Modify: none expected (fixups only if the sweep finds fallout)

- [ ] **Step 1: Full verification**

Run: `make lint && make test && make conformance`
Expected: all green — ruff check + format, `mypy --strict` on all of `src/`, full pytest (minus e2e), 11 conformance invariants.

- [ ] **Step 2: e2e (needs local docker/services per repo norms)**

Run: `uv run pytest tests/e2e/test_skeleton.py -q`
Expected: PASS (the only e2e file this plan touched).

- [ ] **Step 3: Line-count receipt**

Run: `git diff --stat main...HEAD | tail -3`
Expected: net negative ≈ −250 lines. Paste the stat into the final report.

- [ ] **Step 4: Commit any fixups, then hand off**

No release marker — this is a chore sweep; version bump only if the user asks. Use `superpowers:finishing-a-development-branch` to choose merge/PR.

---

## Explicitly NOT in this plan (audit findings rejected or deferred)

| Finding | Verdict |
|---|---|
| a2a egress build block (main.py:1131-1147) | **Keep** — user-confirmed: scaffold for planned Plan 3/4 hosting (opencode currently cannot reach a2a tools; hosting them is its own feature plan). |
| `_StreamEntry` Protocol (stats/sink.py) | **Keep** — decouples the generic stream writer from concrete stat models; union import couples for a 2-line gain. |
| `ManagedServer._sessions` fallback (lifecycle.py) | **Keep** — cut forces `oc_sessions` to be a required param; test churn exceeds the 2-line gain. |
| `seam.py MessageHandler` Protocol | **Keep** — single-impl but mandated O7 seam; breaks channel→Router import cycle. |
| `prompt.compose`, `capability.ach.environment` schema fields | **Keep** — contract-reserved; `extra='forbid'` would reject the operator's rendered config without them. |
| Channel-name uniqueness validation in schema | **Deferred** — the inert dup-check Task 6 removes never prevented double-wiring anyway; adding schema-level uniqueness is new behavior, out of a cleanup sweep's scope. |
