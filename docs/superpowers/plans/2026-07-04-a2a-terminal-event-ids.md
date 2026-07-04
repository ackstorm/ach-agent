# A2A Terminal Event ID Stamping — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every `TaskStatusUpdateEvent` the A2A channel enqueues carries the inbound `task_id` + `context_id` so a2a-sdk's `TaskManager.save_task_event` accepts it, instead of raising `InvalidParamsError("Context in event doesn't match TaskManager ...")` and returning HTTP 500 on every `message:send`.

**Architecture:** `A2AAgentExecutorBridge` (`src/ach_agent/channels/a2a.py`) is the a2a-sdk `AgentExecutor`. It builds terminal events via `_status_event()`. The fix threads the two ids from the `RequestContext` into `_status_event()` for **all** emit paths (auth-reject, missing-id, full-queue, cancel, completion, failure). Out-of-band callbacks (`signal_completion`/`signal_failure`) run after `execute()` returns, so the ids are persisted in `self._pending` alongside the event queue + completion Event.

**Tech Stack:** Python 3.12 asyncio, `a2a-sdk 1.1.0` (proto `TaskStatusUpdateEvent` fields: `task_id`, `context_id`, `status`, `metadata`), pytest + pytest-asyncio.

## Global Constraints

- RTR-06: `a2a.*` imports are **function-scoped only** — never at module level in `a2a.py`. (verify: `grep -nE "^import a2a|^from a2a" src/ach_agent/channels/a2a.py` → zero results)
- Secret (`ek_` / auth value) is NEVER logged; auth compare stays `hmac.compare_digest`.
- The executor must never hang (Pitfall 5): every path that registers `_pending` must have a terminal signal that pops it + sets the completion Event.
- Lint gate: `make lint` = `ruff check` + `ruff format --check` + `mypy --strict` over all of `src/`.
- `session_key = context_id or task_id`, but the SDK validates **both** ids on the event — persist both, not just `session_key`.

## Current state (read before executing)

The **source fix is already applied** in the working tree (`src/ach_agent/channels/a2a.py`). Executors should **verify** it matches the code shown in Task 1, not rewrite it. The **remaining real work is tests** (Tasks 2–3): one existing unit test constructs `_pending` as a 2-tuple and now breaks; no test asserts the ids are stamped. If a fresh worktree is used and the source edits are absent, apply Task 1 first.

Out of scope (secondary items from the bug report, NOT fixed here):
- Auth reject returns HTTP 500 vs 401 — resolved as a side effect once ids are stamped (the failed event is now valid); no dedicated status-code work in this plan.
- Agent Card discovery (`capabilities:{}`, no `url`, no `securitySchemes`) — existing `TODO(a2a)`, separate plan.
- JSON-RPC gRPC-style method names / A2A protocol `0.3` vs `1.0` version mismatch — caller-side, not fixable in this handler.

---

### Task 1: Stamp `task_id`/`context_id` on every terminal event (source)

**Files:**
- Modify: `src/ach_agent/channels/a2a.py`

**Interfaces:**
- Consumes: `RequestContext.task_id`, `RequestContext.context_id` (a2a-sdk); `TaskStatusUpdateEvent(task_id=, context_id=, status=)`.
- Produces:
  - `_status_event(state: str, text: str | None = None, task_id: str = "", context_id: str = "") -> Any`
  - `A2AAgentExecutorBridge._pending: dict[str, tuple[Any, asyncio.Event, str, str]]` — value is `(event_queue, completion, task_id, context_id)`
  - `_signal_async(state: str, text: str, event_queue: Any, completion: asyncio.Event, task_id: str = "", context_id: str = "") -> None`

- [ ] **Step 1: `_status_event` accepts + sets the ids**

```python
def _status_event(
    state: str, text: str | None = None, task_id: str = "", context_id: str = ""
) -> Any:
    """Build a TaskStatusUpdateEvent for a terminal a2a state.

    state: "failed" | "canceled" | "completed" — maps to the a2a TASK_STATE_* enum.
    text: single-part message text for FAILED/COMPLETED; CANCELED carries no message.
    task_id/context_id: MUST match the TaskManager's ids or save_task_event raises
    InvalidParamsError("Context in event doesn't match TaskManager ...").
    """
    from a2a.types.a2a_pb2 import (
        TASK_STATE_CANCELED,
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    state_map = {
        "failed": TASK_STATE_FAILED,
        "canceled": TASK_STATE_CANCELED,
        "completed": TASK_STATE_COMPLETED,
    }
    if text is None:
        status = TaskStatus(state=state_map[state])
    else:
        msg = Message()
        part = msg.parts.add()
        part.text = text
        status = TaskStatus(state=state_map[state], message=msg)
    return TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=status)
```

- [ ] **Step 2: `_pending` holds the 4-tuple**

```python
        # Maps session_key → (event_queue, completion_event, task_id, context_id).
        # task_id/context_id are kept so the terminal event enqueued out-of-band by
        # signal_completion/signal_failure matches the TaskManager's ids.
        self._pending: dict[str, tuple[Any, asyncio.Event, str, str]] = {}
```

- [ ] **Step 3: `execute()` extracts ids up front; all reject paths + registration pass them**

Extract at the top of `execute()` (before the auth block) so the auth-reject paths carry ids:

```python
        # Extract task/context ids up front so EVERY terminal event we enqueue (including
        # the early auth-reject paths) carries ids matching the TaskManager.
        task_id: str = getattr(context, "task_id", None) or ""
        context_id: str = getattr(context, "context_id", None) or ""
```

Then, in order down `execute()`, every `_status_event(...)` gains `task_id, context_id`:
- no-auth-secret reject → `_status_event("failed", "Unauthorized", task_id, context_id)`
- unresolvable-secret reject → `_status_event("failed", "Unauthorized", task_id, context_id)`
- header-mismatch reject → `_status_event("failed", "Unauthorized", task_id, context_id)`
- both-ids-empty reject → `_status_event("failed", "Missing task/context identifier", task_id, context_id)`
- full-queue reject → `_status_event("failed", "Queue full", task_id, context_id)`

Delete the later duplicate `task_id`/`context_id` extraction (they are now set at the top; keep only `session_key = context_id or task_id`). Registration:

```python
        completion = asyncio.Event()
        self._pending[session_key] = (event_queue, completion, task_id, context_id)
```

- [ ] **Step 4: `cancel()` passes ids**

```python
        await event_queue.enqueue_event(_status_event("canceled", None, task_id, context_id))
```

- [ ] **Step 5: `signal_completion`/`signal_failure` unpack the 4-tuple + forward ids**

```python
        event_queue, completion, task_id, context_id = entry
        ...
        loop.create_task(
            _signal_async("completed", reply_text, event_queue, completion, task_id, context_id)
        )
```
(mirror for `signal_failure` with `"failed", reason`)

- [ ] **Step 6: `_signal_async` accepts + forwards the ids**

```python
async def _signal_async(
    state: str,
    text: str,
    event_queue: Any,
    completion: asyncio.Event,
    task_id: str = "",
    context_id: str = "",
) -> None:
    """Schedule a terminal status event and set the completion event from an async context."""
    await event_queue.enqueue_event(_status_event(state, text, task_id, context_id))
    completion.set()
```

- [ ] **Step 7: Lint the changed module**

Run: `uv run mypy --strict src/ach_agent/channels/a2a.py && uv run ruff check src/ach_agent/channels/a2a.py`
Expected: no errors (the earlier "Tuple size mismatch expected 2 but received 4" is gone once Steps 5 land).

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/channels/a2a.py
git commit -m "fix(a2a): stamp task_id/context_id on terminal status events"
```

---

### Task 2: Fix the broken existing unit test + assert FAILED event carries ids

**Files:**
- Modify: `tests/channels/test_a2a.py:602` (inside `test_a2a_signal_failure_enqueues_failed_event_and_unblocks`)

**Interfaces:**
- Consumes: `A2AAgentExecutorBridge._pending` (now a 4-tuple), `_status_event` output fields `task_id`/`context_id`.

The test hand-builds `_pending` as a 2-tuple, which no longer unpacks. Update it to the 4-tuple and assert the emitted FAILED event carries the ids.

- [ ] **Step 1: Run the test to see it break on the tuple change**

Run: `uv run pytest tests/channels/test_a2a.py::test_a2a_signal_failure_enqueues_failed_event_and_unblocks -v`
Expected: FAIL — `ValueError: not enough values to unpack (expected 4, got 2)`

- [ ] **Step 2: Update `_pending` to the 4-tuple + assert ids on the event**

Replace:
```python
    bridge._pending[session_key] = (eq, completion)

    bridge.signal_failure(session_key, "bad terminal")

    # The async task scheduled by signal_failure runs on the next loop tick.
    await asyncio.wait_for(completion.wait(), timeout=2.0)

    assert completion.is_set()
    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_FAILED
    # session_key must be popped from pending
    assert session_key not in bridge._pending
```
with:
```python
    bridge._pending[session_key] = (eq, completion, "task-fail", session_key)

    bridge.signal_failure(session_key, "bad terminal")

    # The async task scheduled by signal_failure runs on the next loop tick.
    await asyncio.wait_for(completion.wait(), timeout=2.0)

    assert completion.is_set()
    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_FAILED
    # ids must be stamped so TaskManager.save_task_event accepts the event
    assert eq.events[0].task_id == "task-fail"
    assert eq.events[0].context_id == session_key
    # session_key must be popped from pending
    assert session_key not in bridge._pending
```

- [ ] **Step 3: Run the test to verify it passes**

Run: `uv run pytest tests/channels/test_a2a.py::test_a2a_signal_failure_enqueues_failed_event_and_unblocks -v`
Expected: PASS

- [ ] **Step 4: Run the full channel suite to catch any other 2-tuple assumption**

Run: `uv run pytest tests/channels/test_a2a.py -q`
Expected: all pass (the auth-reject tests already assert `status.state == TASK_STATE_FAILED`; they still hold).

- [ ] **Step 5: Commit**

```bash
git add tests/channels/test_a2a.py
git commit -m "test(a2a): update signal_failure test for 4-tuple pending + id assertions"
```

---

### Task 3: E2E regression — happy-path completed event carries the inbound ids

**Files:**
- Modify: `tests/e2e/test_a2a_e2e.py:156-158` (inside `test_a2a_task_routes_to_engine_and_enqueues_completed_event`)

**Interfaces:**
- Consumes: real `A2AAgentExecutorBridge.execute()` path (auth → router → `fake_engine_runner` → `on_complete` → `signal_completion`), driven with `task_id="task-e2e-1"`, `context_id="ctx-e2e-1"`.

This test exercises the actual `execute()`/`_pending` flow (not a hand-built tuple), so it is the highest-fidelity guard that the completed event now carries the ids `save_task_event` validates.

- [ ] **Step 1: Add id assertions to the completed event**

After the existing assertions (line ~158), append:
```python
    # Regression: the completed event must carry the inbound ids, else a2a-sdk's
    # TaskManager.save_task_event raises "Context in event doesn't match TaskManager ...".
    assert completed_events[0].task_id == "task-e2e-1"
    assert completed_events[0].context_id == "ctx-e2e-1"
```

- [ ] **Step 2: Run the e2e test**

Run: `uv run pytest tests/e2e/test_a2a_e2e.py::test_a2a_task_routes_to_engine_and_enqueues_completed_event -v`
Expected: PASS (fails without Task 1 — `task_id == ""`).

- [ ] **Step 3: Run the full a2a e2e suite**

Run: `uv run pytest tests/e2e/test_a2a_e2e.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add tests/e2e/test_a2a_e2e.py
git commit -m "test(a2a): e2e assert completed event carries inbound task/context ids"
```

---

## Self-Review

- **Spec coverage:** Bug root cause (terminal events omit ids) → Task 1 covers all six emit paths (5 rejects + cancel + the two signal callbacks). Broken existing test → Task 2. Regression proof through real `execute()` → Task 3. Secondary items (401 vs 500, agent card, protocol version) explicitly scoped out with rationale.
- **Placeholder scan:** none — every step shows the exact code/command.
- **Type consistency:** `_pending` value is `(event_queue, completion, task_id, context_id)` everywhere it is written (`execute` registration) and read (`signal_completion`, `signal_failure`, and the Task 2 test). `_status_event` and `_signal_async` signatures match their call sites. Proto field names `task_id`/`context_id` verified against `TaskStatusUpdateEvent.DESCRIPTOR`.
