# v1.1 opencode re-scope — execution map (for parallel/agent execution)

Four plans implement the v3 opencode re-scope. **They are a dependency chain, not free-parallel work.**

```
Plan 1  de-cruft & reconnect      ✅ DONE  (branch feat/v1.1-opencode-rescope, make lint green, 140/3 non-e2e)
   │
   ▼
Plan 2  proxy + hydration + context ✅ DONE  (hydrate/context/mcp_proxy/model_proxy + boot wiring; lint green, 149/3 non-e2e)
   │
   ▼
Plan 3  channel redraw              ✅ DONE  (webhook source / queue / tui / a2a egress + a2a FAILED; lint green, 175/3 non-e2e)
   │
   ▼
Plan 4  conformance re-green + guard ✅ DONE  (INV-12/13 + queue/a2a idempotency + e2e re-green + integration guard; `make verify` green, exit 0)

ALL FOUR PLANS DONE — `make verify` (lint + test + conformance + secrets) passes. e2e: 18 passed.
```

## Safe execution models (pick one)

**A. Sequential on the branch (simplest, recommended).** One agent runs Plan 2 to completion (all tasks committed + `make lint` green), then Plan 3, then Plan 4, on `feat/v1.1-opencode-rescope`. No conflicts.

**B. Isolated worktrees, merged in order (if you insist on parallel agents).** Each plan runs in its own `git worktree` off the *current tip*, but you MUST **merge in order 2 → 3 → 4**: Plan 3's worktree forks from the merged result of Plan 2 (it needs Plan 2's `EngineConfig`/proxy seam and edits the same `main.py boot()`); Plan 4 forks from the merge of 3. **Do NOT run Plan 2 and Plan 3 concurrently against the same base** — both edit `main.py boot()` and will add/add-conflict. This is the GSD wave model: fork → execute → merge → fork next.

There is **no safe cross-plan concurrency** beyond this. The real parallelism is *within* a plan (see each plan's "Parallel-safe within this plan" note below).

## File ownership / conflict zones

| File | Plan 1 | Plan 2 | Plan 3 | Plan 4 |
|------|:--:|:--:|:--:|:--:|
| `engine/validator.py`, `engine_runner` (main.py) | ✅ owns | — | — | — |
| `engine/hydrate.py`, `engine/context.py`, `engine/mcp_proxy.py` (new) | — | ✅ owns | — | — |
| `engine/lifecycle.py` `write_opencode_config` / `EngineConfig` | (params) | ✅ edits | — | — |
| **`main.py` `boot()`** | (settled) | ✅ edits (hydrate+proxy wiring) | ✅ edits (queue/tui/a2a wiring) | — |
| `channels/webhook.py`, `channels/queue.py`, `channels/tui.py`, `engine/a2a_egress.py` | — | — | ✅ owns | — |
| `channels/a2a.py` (`signal_failure`) | — | — | ✅ edits | — |
| `tests/conformance/*`, `tests/e2e/*` | (trimmed) | — | — | ✅ owns |

**The one cross-plan conflict zone is `main.py boot()` (Plans 2 & 3).** That alone forces 2-before-3.

## Shared interfaces (keep consistent across plans)

- `HydrationManifest` / `McpServer{id,endpoint}` / `A2AAgent` / `Context` — defined in Plan 2 Task 1 (`engine/hydrate.py`). Plan 3 (a2a egress) consumes `manifest.a2a_agents`; Plan 4 mocks the manifest.
- `EngineConfig` fields: `params` (Plan 1), `model_base_url` + `mcp_local_urls` (Plan 2 Task 5). `write_opencode_config` writes **localhost** model `baseURL` + `mcp.<id>` and **no `ek_`** after Plan 2.
- Localhost proxy seam: `McpProxy.start(servers, ek, exclude) -> {id: localhost_url}` and `start_model_proxy(ach_base_url, ek) -> base` (Plan 2 Tasks 3-4). Plan 3's a2a egress registers its harness-hosted tools as one more route on this proxy.
- engine_runner seams: `reply_future` (sync) + `delivery_context["on_complete"]` / `["on_fail"]` (Plan 1 + Plan 3 Task 5). Terminal object `{action,text,thoughts}`.

## Verification debts (the executing agent MUST confirm against live ACH/opencode — not guess)

These are marked in the plans as explicit steps, not placeholders:
- `POST /platform/hydrate` exact field names (sampled from a real curl; re-confirm).
- The opencode `opencode.json` model string + `baseURL` behavior under litellm, and whether `params` (temperature/thinking_level) pass through cleanly.
- That the `opencode serve` HTTP `/message` path streams correctly through the localhost model proxy (SSE).
- redis consume model for `queue` (use `XREADGROUP` + `XACK` for `ackMode:onComplete` + message-id idempotency).

## Plan 2 as-built deviations (read before Plan 3/4)

- **No per-MCP-server exclude.** The plan's `exclude=set(cfg.capability.filter.exclude.mcp_servers)`
  references a field that does NOT exist — `CapabilityFilterExcludeBlock` only carries `tools`
  (opencode-side, still deferred to Plan 3/4). Boot calls `McpProxy().start(..., exclude=set())`;
  all hydrated MCP servers are fronted.
- **Hydration is gated on `ACH_TOKEN`.** The ek_ is read from env `ACH_TOKEN` (not a config path).
  When unset (local dev, hand-written config, no ACH), boot SKIPS hydration/proxies and
  `opencode.json` falls back to `{env:ACH_API_KEY}`/`{env:ACH_BASE_URL}` refs. When set, it hydrates,
  starts both proxies, and writes localhost URLs + a dummy `apiKey` (no ek_, no ACH URL).
- **Memory MCP path preserved.** `EngineConfig.mcp_servers` (the MEM-01 memory list) is untouched and
  still tested; `write_opencode_config` now merges it with `mcp_local_urls` into one `mcp.servers`
  block (`memory-<i>` keys ∪ `<server_id>` keys — no collision). Plan 3/4 may later fold memory into
  the hydrated MCP set, but that convergence is out of Plan 2 scope.
- **`start_model_proxy(ach_base_url, ek) -> str`** keeps the mandated free-function signature; the
  instance is tracked in a module registry and torn down by `stop_model_proxies()` (called in the
  boot shutdown path after `_drain`).

## Plan 4 as-built notes

- **`make verify` = `lint test conformance secrets`** — it does NOT run e2e (`test` ignores `tests/e2e`).
  The e2e suite (Tasks 4-5) is re-greened separately (`uv run pytest tests/e2e`, 18 passed).
- Conformance added: **INV-12** (§6.10 ek never in opencode.json/logs — note the redact pattern is
  `ek_[A-Za-z0-9_\-]+`, underscore prefix), **INV-13** (§6.9 no harness-side delivery: `actions.*` gone,
  the only egress seam is the injected `on_complete`), and **INV-01** extended with queue (redis msg id)
  + a2a (task id). No `test_inv09` (retired dual-delivery, deleted in Plan 1).
- **e2e migration:** `test_gitlab_e2e.py` DELETED (v2 comment-posting; webhook-202 coverage lives in
  `test_main_wiring.py` + `test_webhook.py`). Slack/Telegram mocks removed from `conftest.py`
  (`MockEventQueue` kept). durability/skeleton configs migrated to the v3 schema (no `engine:` block).
- **Integration guard** (`test_opencode_mcp_structured_e2e.py`) is hermetic: it wires `hydrate` +
  `McpProxy` (ek) + `start_model_proxy` (SSE+ek) + `extract_terminal` against mock upstreams. The REAL
  opencode-binary round-trip stays in `scripts/e2e.sh` (`make e2e`) — not run here.
- **Lint gap noted (not fixed):** `make lint` only lints `src` (ruff+mypy); `tests/` is never linted by
  the gate, so latent ruff issues may exist outside touched files.

## Plan 3 as-built deviations (read before Plan 4)

- **queue redis URL from env.** `QueueBlock` carries only `key`/`ackMode` (no connection URL), so
  `QueueConsumer` reads `REDIS_URL` (default `redis://localhost:6379`). Consume model = redis Streams
  consumer group: `XGROUP CREATE … MKSTREAM` → `XREADGROUP` → dispatch → `XACK` only after `handle()`
  returns (onComplete); handler raise → no ack (stays pending); FULL_QUEUE → ack+drop+warn (async_no_retry,
  cron parity). `idempotency_key` = redis message id (§6.1).
- **tui** uses the webhook `reply_future` seam (sync): builds an event with a fresh future, routes it,
  awaits the engine's free-form `text`, writes it to stdout. No terminal contract. Started as a
  background task (cron precedent), stopped in the shutdown branch.
- **a2a egress (`engine/a2a_egress.py`)**: `build_a2a_tools(agents, ek)` → 3 `ToolSpec`s per agent
  (`a2a_<id>` / `_async` / `_status`), errors returned as `{"ok": False, "error": …}` (never raise).
  The `ek_` is held in the harness-hosted tool layer (opencode→localhost a2a-tools MCP→harness w/ek→peer),
  NOT routed through `McpProxy` — so no new proxy routes were needed and ek-hygiene still holds.
  **VERIFICATION DEBTS:** (1) the ported `A2AAgentClient` wire code was adapted to the *installed*
  a2a-sdk **protobuf** API (`a2a.types.a2a_pb2`, same as `channels/a2a.py`) — NOT ackbot's pydantic API;
  it is covered only by import+lint (tests inject a `FakeClient`), so a live-peer round-trip is unverified.
  (2) `build_a2a_mcp_server(tools)` (FastMCP, `server.add_tool(...)`) is built but **not hosted** — the
  boot branch (gated on `manifest.a2a_agents`, a no-op when empty) builds tools+server and logs, with a
  `# Plan 3/4 follow-up` seam to host it on localhost + add to opencode.json. Hosting + opencode wiring
  is outstanding.
- **a2a ingress FAILED**: `signal_failure(session_key, reason)` + `_fail_async` mirror `signal_completion`;
  engine_runner routes to `on_complete` only when `action=="a2a_reply"` and `text.strip()`, else `on_fail`
  (FAILED `TaskStatusUpdateEvent`). Boot W9 injects both `on_complete` and `on_fail` into delivery_context.

## New dependencies to add (`pyproject.toml`)

- `httpx` (Plan 2 — promote from the dev group to `dependencies` if used at runtime).
- `redis>=5,<6` (Plan 3 — `redis.asyncio`).

## Branch / prereqs

All work is on `feat/v1.1-opencode-rescope` (Plan 1 merged into it). Canonical `CONTRACT_v3.md` /
`REQUIREMENTS.md` / `CLAUDE.md` are updated on disk (gitignored). Each plan's header lists its prereq.
