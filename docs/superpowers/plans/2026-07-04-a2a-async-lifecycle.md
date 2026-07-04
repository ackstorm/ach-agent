# A2A Async Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let A2A callers get their `task_id` immediately (non-blocking) and poll `GetTask` for the terminal result, without holding the HTTP request for the whole engine run — engine stays synchronous and transport-blind.

**Architecture:** The a2a-sdk `DefaultRequestHandler` already runs our `execute()` as a **background producer task** and already branches on `SendMessageConfiguration.return_immediately`. With `blocking=False` its `ResultAggregator.consume_and_break_on_interrupt` returns "after the first event that creates a Task", then keeps consuming in the background and persists later events to the `task_store`. Today our `execute()` enqueues **nothing** until the terminal event, so there is no event to break on and the non-blocking flag is a no-op. Fix: `execute()` emits ONE interim `WORKING` `TaskStatusUpdateEvent` before `await completion.wait()`. The engine, the `{action,text}` terminal contract, and the `<output_format>` directive are untouched. This is grounded in the installed `a2a-sdk 1.1.0` source, not assumed.

**Tech Stack:** Python 3.12 / asyncio, `a2a-sdk>=1.1.0,<2` (protobuf types in `a2a.types.a2a_pb2`), pytest(+asyncio). All a2a imports function-scoped (RTR-06).

## Global Constraints

- **RTR-06:** `a2a.*` imports live ONLY inside functions/methods in `src/ach_agent/channels/a2a.py`. Never at module level, never in `seam.py`/`router.*`/`engine.*`.
- **W9 engine fence:** `engine_runner` never imports `channels.a2a`. The engine produces `{action,text}` and fires injected `on_complete(session_key,text)` / `on_fail(session_key,reason)`; it never sees a `task_id`. This plan touches ONLY `channels/a2a.py` + its tests.
- **Terminal-event id match:** every `TaskStatusUpdateEvent` we enqueue MUST carry `task_id`/`context_id` matching the `TaskManager` (its `save_task_event` raises `InvalidParamsError` on mismatch). `execute()` already extracts both up front (a2a.py:118-119).
- **Never hang (Pitfall 5):** every accepted request must reach a terminal event via `signal_completion`/`signal_failure`. The interim `WORKING` event is non-terminal and does NOT satisfy this — `completion.wait()` still gates the terminal event.
- **Decision gate before building — who is the long-task caller?** LiteLLM reply-holds `message/send` (blocking), so it gets **zero** benefit from non-blocking + poll. Phase 1 helps **direct A2A clients** only. Confirm the real long-task caller is a direct A2A client (not the LiteLLM proxy) before investing past the Phase-1 spike. Verified: `on_message_send` sets `blocking = not params.configuration.return_immediately` — the flag is honored only if the *caller* sends it.

## SDK facts this plan relies on (verified in `.venv/.../a2a/`)

- `default_request_handler.py:316` — `execute()` runs as `producer_task = asyncio.create_task(self._run_event_stream(...))`; NOT inline. `_run_event_stream` (`:250-251`) awaits `execute()` then `queue.close()`.
- `default_request_handler.py:368` — `blocking = not params.configuration.return_immediately`.
- `result_aggregator.py:105,162-177` — non-blocking: "returns after the first event that creates a Task or Message"; on break it spawns `_continue_consuming(...)` as a background task and `break`s.
- `result_aggregator.py:179+` / `task_manager.py:262-311` — `_continue_consuming` → `task_manager.process(event)` → `save_task_event` → `_save_task` → `task_store.save(...)`. So the terminal event enqueued later by `signal_completion` IS persisted to the store for `GetTask`.
- `a2a_pb2` `TASK_STATE_WORKING` exists (non-terminal).

## File Structure

- Modify: `src/ach_agent/channels/a2a.py`
  - `_status_event` (`:48-81`) — add `"working" → TASK_STATE_WORKING` to `state_map` + import.
  - `A2AAgentExecutorBridge.execute` (`:107-221`) — enqueue one interim `WORKING` event after the DUPLICATE check, immediately before `await completion.wait()`.
- Modify: `tests/channels/test_a2a.py`
  - Fix `test_a2a_engine_not_ready_routes_normally` (`:309-335`) — it asserts `eq.events == []` post-dispatch; the interim event makes that one `WORKING` event.
  - Add `test_a2a_execute_emits_interim_working_before_completion`.
  - Add `test_a2a_terminal_sequence_is_working_then_completed` (drive through `signal_completion`).

---

### Task 1: Interim WORKING event so the SDK's non-blocking path can return `task_id`

**Files:**
- Modify: `src/ach_agent/channels/a2a.py:60-73` (`_status_event` import + `state_map`)
- Modify: `src/ach_agent/channels/a2a.py:219-221` (`execute`, before `await completion.wait()`)
- Test: `tests/channels/test_a2a.py`

**Interfaces:**
- Consumes: `_status_event(state, text, task_id, context_id)` — existing builder; `MockEventQueue` from the test file.
- Produces: after a successful admit, `execute()` enqueues exactly one non-terminal `TASK_STATE_WORKING` `TaskStatusUpdateEvent` (ids matching `TaskManager`) before blocking on `completion.wait()`. No new public symbols.

- [ ] **Step 1: Write the failing test — interim WORKING event is emitted before completion**

Add to `tests/channels/test_a2a.py`:

```python
@pytest.mark.asyncio
async def test_a2a_execute_emits_interim_working_before_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Non-blocking support: execute() emits one interim WORKING event after a successful
    admit and BEFORE completion.wait(), so the SDK's consume_and_break_on_interrupt
    (blocking=False) has a task-creating event to break on and can return task_id now.
    The WORKING event must be non-terminal and carry ids matching the TaskManager."""
    from a2a.types.a2a_pb2 import TASK_STATE_WORKING

    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-w", context_id="ctx-w")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Dispatched, now blocked on completion.wait() — the interim WORKING event is present.
    handler.handle.assert_called_once()
    assert len(eq.events) == 1
    evt = eq.events[0]
    assert evt.status.state == TASK_STATE_WORKING
    assert evt.task_id == "task-w"
    assert evt.context_id == "ctx-w"

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
```

- [ ] **Step 2: Run it to confirm it fails**

Run: `uv run pytest tests/channels/test_a2a.py::test_a2a_execute_emits_interim_working_before_completion -v`
Expected: FAIL — `assert len(eq.events) == 1` fails with `0` (nothing enqueued before `completion.wait()` today).

- [ ] **Step 3: Add `"working"` to `_status_event` state_map**

In `src/ach_agent/channels/a2a.py`, extend the import and map in `_status_event`:

```python
    from a2a.types.a2a_pb2 import (
        TASK_STATE_CANCELED,
        TASK_STATE_COMPLETED,
        TASK_STATE_FAILED,
        TASK_STATE_WORKING,
        Message,
        TaskStatus,
        TaskStatusUpdateEvent,
    )

    state_map = {
        "failed": TASK_STATE_FAILED,
        "canceled": TASK_STATE_CANCELED,
        "completed": TASK_STATE_COMPLETED,
        "working": TASK_STATE_WORKING,
    }
```

- [ ] **Step 4: Emit the interim WORKING event before `completion.wait()`**

In `execute()`, immediately after the `DUPLICATE` handling block and before the existing `# (3) Await out-of-band completion` / `await completion.wait()`:

```python
        # Emit ONE interim WORKING event so the a2a-sdk non-blocking path has a
        # task-creating event to break on: with SendMessageConfiguration.return_immediately
        # the handler's consume_and_break_on_interrupt(blocking=False) returns the task_id
        # after this event, then persists the later terminal event to the task_store for
        # GetTask polling. In the blocking path the aggregator ignores WORKING and waits for
        # the terminal event, so this is harmless there. Ids match the TaskManager
        # (save_task_event validates them). ponytail: the SDK owns the block/non-block fork
        # via its own flag — no branch here.
        await event_queue.enqueue_event(_status_event("working", None, task_id, context_id))

        # (3) Await out-of-band completion from engine via signal_completion
        await completion.wait()
```

- [ ] **Step 5: Run the new test to confirm it passes**

Run: `uv run pytest tests/channels/test_a2a.py::test_a2a_execute_emits_interim_working_before_completion -v`
Expected: PASS.

- [ ] **Step 6: Fix the one existing test the interim event breaks**

`test_a2a_engine_not_ready_routes_normally` (`tests/channels/test_a2a.py:309-335`) asserts `assert eq.events == []` after dispatch. Replace that single assertion:

```python
    handler.handle.assert_called_once()
    # After dispatch the bridge emits one interim WORKING event (non-blocking support),
    # not a failed "service warming up" event. The engine still starts lazily in the lane.
    from a2a.types.a2a_pb2 import TASK_STATE_WORKING

    assert len(eq.events) == 1
    assert eq.events[0].status.state == TASK_STATE_WORKING
```

- [ ] **Step 7: Run the full a2a test module**

Run: `uv run pytest tests/channels/test_a2a.py -q`
Expected: PASS (existing auth/session/full-queue/signal tests unchanged — they either assert their own terminal event or cancel before reaching the WORKING enqueue on the admit path; only `test_a2a_engine_not_ready_routes_normally` needed the update in Step 6).

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/channels/a2a.py tests/channels/test_a2a.py
git commit -m "feat(a2a): emit interim WORKING event to enable non-blocking message/send"
```

---

### Task 2: Terminal sequence regression guard (WORKING → COMPLETED)

**Files:**
- Test: `tests/channels/test_a2a.py`

**Interfaces:**
- Consumes: `signal_completion(session_key, reply_text)` (existing, a2a.py:233), `MockEventQueue`.
- Produces: proof that a full accepted turn enqueues `[WORKING, COMPLETED]` in order with matching ids — documents the two-event contract the store/consumer relies on.

- [ ] **Step 1: Write the failing test**

```python
@pytest.mark.asyncio
async def test_a2a_terminal_sequence_is_working_then_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full accepted turn enqueues WORKING (interim) then COMPLETED (terminal), in order,
    both carrying ids matching the TaskManager. This is the sequence the SDK's background
    consumer persists to the task_store for GetTask polling."""
    from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED, TASK_STATE_WORKING

    handler = _make_accepted_handler()
    channel_cfg = _make_authed_channel_cfg(monkeypatch)
    bridge = A2AAgentExecutorBridge(handler=handler, channel_cfg=channel_cfg)

    ctx = _authed_ctx(task_id="task-seq", context_id="ctx-seq")
    eq = MockEventQueue()

    task = asyncio.create_task(bridge.execute(ctx, eq))
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    # Interim WORKING is in flight; now the engine delivers.
    bridge.signal_completion("ctx-seq", "done")
    await asyncio.wait_for(task, timeout=2.0)  # signal_completion sets the Event → execute returns

    states = [e.status.state for e in eq.events]
    assert states == [TASK_STATE_WORKING, TASK_STATE_COMPLETED]
    assert eq.events[-1].status.message.parts[0].text == "done"
    for e in eq.events:
        assert e.task_id == "task-seq"
        assert e.context_id == "ctx-seq"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/channels/test_a2a.py::test_a2a_terminal_sequence_is_working_then_completed -v`
Expected: PASS with Task 1's changes in place (no new production code — this locks the contract).

Note: if it flakes on ordering, the cause is `signal_completion` scheduling `_signal_async` via `loop.create_task`; `await asyncio.wait_for(task, ...)` already yields to that task before `execute()` returns, so the COMPLETED event lands after WORKING. No sleep tuning needed.

- [ ] **Step 3: Commit**

```bash
git add tests/channels/test_a2a.py
git commit -m "test(a2a): guard WORKING→COMPLETED terminal sequence + id stamping"
```

---

### Task 3 (SPIKE, not merge-gated): prove non-blocking end-to-end against real a2a-sdk 1.1.0

**Files:**
- Scratch only: `<scratchpad>/a2a_nonblocking_spike.py` (do NOT commit).

**Interfaces:**
- Consumes: `build_a2a_app`, `make_a2a_agent_card` (real SDK routes), `httpx`/`TestClient`.
- Produces: a yes/no on "does a `return_immediately:true` `message/send` return a non-terminal task immediately, and does a subsequent `GetTask` return the terminal COMPLETED task after `signal_completion`?" This is the real value check before touching push (Task 4).

- [ ] **Step 1: Write a scratch probe**

Mount `build_a2a_app` under a `TestClient`, wire a bridge whose handler admits and whose engine is simulated by calling `bridge.signal_completion(session_key, "ok")` from a background task ~50ms after dispatch. Then:
1. `POST /` JSON-RPC `message/send` with `configuration.returnImmediately = true`; assert the response arrives BEFORE the simulated completion and carries a non-terminal (`SUBMITTED`/`WORKING`) task + a `task_id`.
2. After completion, `POST /` JSON-RPC `tasks/get` with that `task_id`; assert `TASK_STATE_COMPLETED` + text `"ok"`.

Run: `uv run python <scratchpad>/a2a_nonblocking_spike.py`
Expected: both asserts pass. If step 1 still blocks until completion, the SDK version in the lockfile does not honor `return_immediately` as read (re-check `on_message_send`), and Phase 1 needs revisiting BEFORE shipping — stop and report.

- [ ] **Step 2: Record the result**

If green: note in the commit body / decision record that non-blocking + `GetTask` works single-pod with zero extra wiring. If red: file the exact SDK divergence and STOP — do not proceed to Task 4.

---

## Phase 2 — Push callbacks (DEFERRED, gated on Task 3 green AND a direct-A2A caller)

**Do NOT build speculatively.** Ship + validate Phase 1 first. Phase 2 is small wiring but has ONE unverified link — spike it before planning tasks.

Grounded facts:
- `LegacyRequestHandler.__init__` (default_request_handler.py:91-125) takes `push_config_store` and `push_sender`, both defaulting to `None`. Ours passes neither (a2a.py:350-354) — that is the only reason push is inert today.
- `on_message_send` already stores inline `configuration.task_push_notification_config` (`:304-311`) and fires `push_notification_callback` on every event (`:373`).
- SDK ships `InMemoryPushNotificationConfigStore` and `BasePushNotificationSender(httpx_client, config_store)`.

Wiring (when Phase 2 lands): in `build_a2a_app`, construct `store = InMemoryPushNotificationConfigStore()`, `sender = BasePushNotificationSender(<app-scoped httpx.AsyncClient>, store)`, pass both to `LegacyRequestHandler`, and advertise `AgentCapabilities(push_notifications=True)` in `make_a2a_agent_card`.

**Unverified link (SPIKE FIRST):** `_send_push_notification_if_needed` only POSTs when the event is a `PushNotificationEvent`; our terminal event is a `TaskStatusUpdateEvent`. Confirm the SDK emits/wraps a `PushNotificationEvent` for terminal status BEFORE writing Phase 2 tasks — otherwise the POST never fires and the wiring is a no-op.

**Infra note:** `build_a2a_app` is sync and owns no lifespan; the `httpx.AsyncClient` for the sender must be app-scoped and closed on shutdown (wire via the FastAPI lifespan in `main.py`, not per-request). YAGNI until Phase 2 is actually greenlit.

## Phase 3 — INPUT_REQUIRED + streaming (DEFERRED, YAGNI)

- **`input_required`** needs a NEW terminal action verb → it DOES touch the `{action,text}` contract AND the `<output_format>` directive (contra the evaluator's "engine untouched" framing). `a2a_reply → COMPLETED` needs nothing new; a multi-turn verb does. Only build when a multi-turn use case exists.
- **Streaming** needs a new `on_progress(session_key, chunk)` seam in `delivery_context` — this is the ONLY item that legitimately touches the engine. Classifiers are terminal-only; do not build until a streaming consumer lands.

## Non-goals

- No async/task concept leaks into the engine or the `{action,text}` contract in Phase 1 (W9 fence held — only `channels/a2a.py` changes).
- No shared task store in Phase 1. In-memory works single-pod; a shared backend (Valkey/DB) is only needed for multi-replica or restart survival of `GetTask`/push — defer until deployment is multi-replica.
- No new interaction mode for webhook/cron/queue.

---

## Self-Review

**Spec coverage vs the evaluator's proposal:**
- Non-blocking + `GetTask` (their Phase 1) → Task 1 + Task 3 spike. Corrected scope: ~5 lines of production code, not a lifecycle branch, because the SDK already forks on `return_immediately` and persists via the background consumer.
- Shared task store (their Phase 1 item 4) → moved to Non-goals (YAGNI single-pod).
- Push (their Phase 2) → Phase 2 here, gated, with the one real unverified link flagged.
- `input_required` / streaming (their Phase 3) → Phase 3 here, with the correction that `input_required` touches the terminal contract (their doc mislabels it "engine untouched").

**Placeholder scan:** none — every code step shows the exact diff; every run step shows the command + expected result.

**Type/name consistency:** `_status_event(state, text, task_id, context_id)` signature matches a2a.py:48-49. `"working"` key added to the same `state_map` it is read from. `signal_completion(session_key, reply_text)` matches a2a.py:233. `TASK_STATE_WORKING` confirmed present in `a2a_pb2`. Test helpers (`_make_authed_channel_cfg`, `_authed_ctx`, `_make_accepted_handler`, `MockEventQueue`) all exist in the current test file.

**Risk the plan removes by construction:** the interim event breaks exactly one existing assertion (`test_a2a_engine_not_ready_routes_normally`), handled in Task 1 Step 6 — found by reading the test, not assumed.
