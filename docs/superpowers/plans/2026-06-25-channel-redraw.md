# Plan 3 — Channel Redraw (webhook source-select, queue, tui, a2a egress)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
>
> **Design-forward plan.** Prereqs: Plan 1 + Plan 2 merged.
>
> **Execution (see `README.md`).** Owns: `channels/webhook.py` (source-select), `channels/queue.py`, `channels/tui.py`, `engine/a2a_egress.py` (new) + edits `channels/a2a.py` and **`main.py boot()`**. ⚠ Forks from the **merged** result of Plan 2 (needs its `EngineConfig`/proxy seam; shares `main.py boot()`). Parallel-safe *within* this plan: Tasks 1 (webhook) + 2 (queue) + 3 (tui) + 4 (a2a egress) are largely independent modules; the `main.py` registration + Task 5 (a2a ingress) serialize at the end.

**Goal:** Complete the v3 channel set — webhook becomes `source`-selected (gitlab|github|generic), implement `queue` (redis) and `tui`, and add **a2a egress** (peer agents as MCP tools) plus the a2a-ingress FAILED-on-invalid-terminal path.

**Architecture:** webhook keeps the existing FastAPI route but its parser/auth are chosen by `channel.source`. `queue` consumes redis (idempotency key = redis message id). `tui` reads stdin / streams stdout (no terminal contract). a2a **egress** ports ackbot's `handlers/a2a/{tools,client,notification_store}.py` into a harness-hosted MCP exposed to opencode via the localhost proxy seam from Plan 2. a2a **ingress** reuses `channels/a2a.py`; add a FAILED signal when the terminal output is invalid.

**Tech Stack:** Python 3.12, asyncio, redis (new dep), a2a-sdk (already a dep), the existing channel/seam patterns.

## Global Constraints

- `uv run`; `make lint` green; router untouched.
- New runtime dep: `redis>=5,<6` (async client `redis.asyncio`).
- a2a egress peers are ACH-fronted → their MCP/client calls go **through the Plan-2 proxy** with the `ek`; never expose `ek` to opencode.
- Idempotency keys (CONTRACT §6.1): queue = redis message id; a2a = task id. NEVER empty/shared.
- Follow the existing `channels/seam.py` `MessageHandler` Protocol and `MessageEvent` shape — do not change the router seam.

---

### Task 1: webhook `source`-selected parser/auth

**Files:** Modify `src/ach_agent/channels/webhook.py`; Test `tests/channels/test_webhook.py`.

**Interfaces:** `handle_webhook_request(..., source: Literal["gitlab","github","generic"])` dispatches to `_parse_gitlab` / `_parse_github` / `_parse_generic` (idempotency key + delivery_context per source) and `_verify_auth` per `webhook.auth.type` (`gitlab_token` = `X-Gitlab-Token` compare; `hmac` = HMAC-SHA256 over raw body; `none`).

- [ ] **Step 1: Read the current `webhook.py`** to see the existing gitlab parsing + HMAC, then write failing tests for github + generic + the gitlab_token path:

```python
async def test_github_source_parses_delivery_id(...):
    # X-GitHub-Delivery header → idempotency_key; HMAC auth over raw body
    ...
async def test_generic_source_uses_request_id_fallback(...):
    ...
async def test_gitlab_token_auth_rejects_bad_token(...):
    # X-Gitlab-Token mismatch → 401
    ...
```

(Fill the `...` with concrete request bodies/headers following the existing test's style — read `tests/channels/test_webhook.py` first.)

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the `source` dispatch: extract the current gitlab logic into `_parse_gitlab`, add `_parse_github` (`X-GitHub-Delivery` key, repo/PR delivery_context) and `_parse_generic` (`X-Request-ID` → ms-timestamp fallback). Move auth to `_verify_auth(auth_type, secret_path, headers, raw_body)`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(webhook): source-selected parser+auth (gitlab|github|generic)"`

---

### Task 2: `queue` channel (redis)

**Files:** Create `src/ach_agent/channels/queue.py`; Test `tests/channels/test_queue.py`. Modify `src/ach_agent/main.py` (`WIRED_CHANNEL_TYPES += "queue"`, wire a `QueueConsumer` task per queue channel).

**Interfaces:** `class QueueConsumer(channel_cfg, handler, pool)` with `async start()/stop()`. Consumes `channel.queue.key` via `redis.asyncio`; builds a `MessageEvent` with `idempotency_key = <redis message id>`, `session_key`, `payload`; `ackMode:onComplete` → ack only after `handler.handle()` returns ACCEPTED/processed.

- [ ] **Step 1: Failing test** with a fake redis (an in-memory stub exposing `xreadgroup`/`xack` or `blpop`-style — match the consume model you choose; document it). Assert: a message → `handler.handle` called with `idempotency_key == message_id`; ack happens only after handle; on handler raise, no ack.

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `QueueConsumer` using `redis.asyncio` streams (`XREADGROUP` + `XACK` for onComplete semantics). Build the `MessageEvent` (`source_trait="async_no_retry"` or redelivery per ackMode). Wire one consumer task per queue channel in `main.py`, started alongside cron, stopped in `_drain`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(queue): redis stream consumer channel (ackMode:onComplete)"`

---

### Task 3: `tui` channel

**Files:** Create `src/ach_agent/channels/tui.py`; Test `tests/channels/test_tui.py`. Modify `main.py` (`WIRED_CHANNEL_TYPES += "tui"`).

**Interfaces:** `class TuiChannel(channel_cfg, handler, pool)` reads lines from stdin, builds a `MessageEvent` (idempotency_key = ms-timestamp; `source_trait="sync"`), runs it, and streams the engine's free-form `text` to stdout. **No terminal contract** (the engine_runner returns `text` even for `action:none` — Plan 1 — which tui prints).

- [ ] **Step 1: Failing test** — feed a line via a fake stdin (`io.StringIO`/monkeypatched reader), stub the handler to resolve a reply, assert the text is written to a captured stdout.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the stdin read-loop (`asyncio` reader over `sys.stdin`) → `MessageEvent` → `handler.handle` → print `text`. Wire in `main.py` as a task (only when a tui channel is configured; typically single-process interactive).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(tui): stdin/stdout free-form channel (no terminal contract)"`

---

### Task 4: a2a egress — peer agents as harness-hosted MCP tools

**Files:** Create `src/ach_agent/engine/a2a_egress.py` (port of ackbot `handlers/a2a/{tools,client,notification_store}.py`); Test `tests/engine/test_a2a_egress.py`. Modify boot to register these tools into the localhost MCP proxy (Plan 2 seam).

**Interfaces:** `build_a2a_tools(agents: list[A2AAgent], proxy_route_for: Callable) -> list[ToolSpec]` exposing per agent: `a2a_{name}` (blocking), `a2a_{name}_async` (returns task_id), `a2a_{name}_status`. Calls go via the Plan-2 proxy (ek injected there). Reuse ackbot's `A2AAgentClient` (a2a-sdk wrapper) and `A2ANotificationStore` largely as-is.

- [ ] **Step 1: Read ackbot `/home/coder/workspace/local/ackbot-process/src/handlers/a2a/{tools,client,notification_store}.py`** and copy `client.py` + `notification_store.py` nearly verbatim (they're "fully reusable" per the survey); adapt `tools.py` to emit our `ToolSpec`/MCP-tool shape instead of ackbot's `ToolDef`.

- [ ] **Step 2: Failing test** — with a fake peer (a2a-sdk client monkeypatched), assert `a2a_{name}` returns the peer's text, `_async` returns a `task_id`, `_status` resolves via the notification store, and a peer error returns an error result (not a raise).

- [ ] **Step 3: Run → FAIL.**
- [ ] **Step 4: Implement** `a2a_egress.py`; in boot, build the tools from `manifest.a2a_agents` and register them as a harness-hosted MCP server route on the Plan-2 proxy (the one harness-hosted MCP), so opencode sees them as normal MCP tools.
- [ ] **Step 5: Run → PASS.**
- [ ] **Step 6: Commit** — `git commit -m "feat(a2a-egress): peer agents as harness-hosted MCP tools (port from ackbot)"`

---

### Task 5: a2a ingress — FAILED on invalid terminal

**Files:** Modify `src/ach_agent/channels/a2a.py` (add `signal_failure`); Modify the engine_runner's a2a path (Plan 1) to call failure when the terminal is unusable; Test `tests/channels/test_a2a.py`.

**Interfaces:** `A2AAgentExecutorBridge.signal_failure(session_key: str, reason: str)` enqueues `TaskStatusUpdateEvent(state=failed, message=reason)`. The engine_runner's `on_complete` seam is paired with an `on_fail` seam (or `on_complete(session_key, text, ok: bool)`).

- [ ] **Step 1: Read `channels/a2a.py`** for the existing `signal_completion` + EventQueue wiring. Write a failing test: when the terminal object is missing/invalid (engine_runner gets `{"action":"none"}` with empty text, or extraction failed), the a2a bridge emits a `failed` TaskStatusUpdateEvent.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `signal_failure` (mirror `signal_completion`); extend the boot W9 wiring to inject both `on_complete` and `on_fail` into `delivery_context`; in the engine_runner a2a branch, call `on_fail` when the terminal `action` is not `a2a_reply` (or text empty) — else `on_complete`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(a2a): FAILED callback on invalid terminal output"`

---

## Self-Review

**Coverage:** webhook source-select ✓ T1 (CHN-06), queue ✓ T2 (CHN-07), tui ✓ T3 (CHN-08), a2a egress ✓ T4 (MCP-05), a2a ingress FAILED ✓ T5 (CHN-09 / §8 table). slack/telegram deletion + Hermes drop were Plan 1 (CHN-10). **Reuse:** ackbot `client.py`/`notification_store.py` near-verbatim. **Verify:** redis consume model (streams vs lists) — pick streams (`XREADGROUP`) for onComplete + message-id idempotency; confirm the operator renders a stream key. **Read-first steps** (webhook.py, a2a.py, ackbot a2a/*) are required actions, not placeholders — exact diffs depend on the current code.

**Type consistency:** `MessageEvent` unchanged (idempotency_key/session_key/payload/delivery_context/source_trait). a2a egress `A2AAgent` from `HydrationManifest` (Plan 2). `on_complete`/`on_fail` seams thread through `delivery_context` (Plan 1 W9 pattern).
