# v1.1 opencode re-scope — execution map (for parallel/agent execution)

Four plans implement the v3 opencode re-scope. **They are a dependency chain, not free-parallel work.**

```
Plan 1  de-cruft & reconnect      ✅ DONE  (branch feat/v1.1-opencode-rescope, make lint green, 140/3 non-e2e)
   │
   ▼
Plan 2  proxy + hydration + context ✅ DONE  (hydrate/context/mcp_proxy/model_proxy + boot wiring; lint green, 149/3 non-e2e)
   │
   ▼
Plan 3  channel redraw                (webhook source / queue / tui / a2a egress) ← fork from here
   │
   ▼
Plan 4  conformance re-green + guard   (depends on 2 AND 3)
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

## New dependencies to add (`pyproject.toml`)

- `httpx` (Plan 2 — promote from the dev group to `dependencies` if used at runtime).
- `redis>=5,<6` (Plan 3 — `redis.asyncio`).

## Branch / prereqs

All work is on `feat/v1.1-opencode-rescope` (Plan 1 merged into it). Canonical `CONTRACT_v3.md` /
`REQUIREMENTS.md` / `CLAUDE.md` are updated on disk (gitignored). Each plan's header lists its prereq.
