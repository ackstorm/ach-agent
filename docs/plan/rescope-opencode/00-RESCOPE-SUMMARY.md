# v1.1 Re-scope — Engine = opencode (revert), localhost proxy, no plugins

> **STATUS: PROPOSAL / DRAFT for review.** Nothing here is applied. These files do not
> overwrite the canonical `docs/plan/CONTRACT_v3.md`, `.planning/REQUIREMENTS.md`, or any code.
> They capture the design agreed in the 2026-06-25 session so we can review before re-scoping
> the GSD phases.

## What changed and why

The v1.1 milestone was scoped as "re-platform onto **OpenAI Codex** + external-MCP egress."
During design we discovered two things:

1. **We were building an agent, not configuring one.** Deep Agents (the alternative considered)
   is an agent *framework* — it makes the harness assemble skills/subagents/tools/the loop. That
   is exactly what we do **not** want: we already have a complete agent.
2. **The opencode bridge already exists in this repo**, complete and hardened
   (`src/ach_agent/engine/`: `client.py`, `lifecycle.py`, `events.py`, `pool.py`, `validator.py`,
   `sanitized_env.py`). The code **never moved to Codex** — only the docs did. So "Codex" was a
   plan, not an implementation.

**Decision: revert the engine to opencode** (drop both the Codex direction and the Deep Agents
idea). opencode is a complete agent (searches, integrates, navigates), is provider-agnostic, has
a config-driven MCP client, and now supports structured output (`format: json_schema` +
StructuredOutput tool) — good enough given our terminal contract already specifies
validate-+-retry as the backstop.

## Architecture (agreed)

- **Engine:** opencode (`opencode serve` + SSE), reusing the existing bridge. Hard part is done.
- **Secret hygiene via localhost proxy (NEW, the one substantial new component):** the harness
  runs a **localhost reverse-proxy** for BOTH the model and MCP traffic. opencode points only at
  `http://localhost/...` (model `…/v1|/gemini|/anthropic`, MCP servers as localhost URLs). The
  proxy injects `Authorization: Bearer ek_` on the way to ACH. **The `ek_` never appears in
  opencode's config or env.** This consciously trades "harness out of the egress path" for "ek
  never leaves the harness" — strong hygiene, since opencode is a third-party binary.
- **Egress = external MCP, fronted by the proxy.** opencode is the MCP client (to localhost); the
  proxy fronts the ACH-fronted MCP servers. `capability.filter.exclude` withholds tools/servers
  **before** they are offered (a gate above the model).
- **a2a egress = harness-hosted MCP tools** (reuse ackbot `handlers/a2a/{tools,client,notification_store}.py`):
  peer agents surfaced as `a2a_{name}` / `_async` / `_status` tools. The one **harness-hosted**
  MCP (everything else is proxied/remote). a2a **ingress** is the existing `channels/a2a.py`
  (a2a-sdk server); FAILED is signaled via `TaskStatusUpdateEvent(state=failed)`.
- **Model block:** `{ name, type, params }`. `type ∈ {openai, gemini, anthropic}` selects the ACH
  compat endpoint the proxy exposes. `name` passed verbatim; must resolve against the hydrated
  models list (else hard-fail). `params` is an **open, unvalidated** dict splatted to the model
  client (temperature, thinking_level, anything — user's fault if it breaks).
- **ACH simplified — no plugins.** Context = **skills / prompts / artifacts** only. Each is a
  `tar.gz` downloaded and decompressed into a designated directory. The plugin-explosion design
  (subagents/mcps/hooks) is **dropped**.
- **Terminal contract = single object** per channel class (NOT a list — the list was v2 dispatch;
  with MCP egress, side-effects happen as tool calls during the turn, so the terminal is the final
  decision). Each action carries a free-text **`text`** field (+ optional `thoughts`) so the model
  always has a place to "finish". Policy: **a2a → FAILED to callback**; **async
  (webhook/cron/queue) → `none`** is benign; **tui → no contract** (free text). Mechanism already
  exists (`validator.py`: `extract_actions` + `validate_actions` + `repair_turn`).
- **Retires:** `actions/dispatch_actions` + `GitlabCommentAdapter` posting (the harness no longer
  posts — the agent posts via gitlab-mcp). The harness only **relays** the terminal text to a
  waiting webhook-reply (sync) or a2a callback (async).

## What is already built vs. genuinely new

**Already here (keep):** router (the IP), opencode bridge, http surface, drain, MessageEvent+seam,
memory (fail-open MCP client), config schema (mostly), ~54 test files.

**Genuinely new (small):**
- localhost proxy (model + MCP) injecting `ek` — main new component (streaming SSE is the fiddly bit).
- context fetch (tar→dir) for skills/prompts/artifacts.
- queue (redis) + tui channels (today: stubs).
- a2a egress MCP tools (port ackbot `tools.py`/`client.py`/`notification_store.py`).

**Reconnect / delete:**
- map `model{name,type,params}` → `EngineConfig`; remove the Phase-2 mypy override + `ResponseActionBlock` alias; this re-greens the http/main_wiring tests.
- delete slack/telegram + Hermes dep; webhook becomes `source`-selected.
- delete plugin support.

## Resolved decisions (2026-06-25)

1. **`schemaVersion` = `"1"`** (D-03). The harness validates `"1"`; `ach-runtime` must render `"1"`.
2. **Hydration = the harness calls `POST {ACH_BASE_URL}/platform/hydrate`** (`x-ach-key: ek_`).
   **No CLI in the agent.** The harness owns hydration.
3. **Structured output = Option A** (text-based): keep the existing `extract_actions` +
   `validate_actions` + `repair_turn`; the harness extracts the JSON from the model's text output,
   validates (Pydantic), retries ≤1. opencode's native `format: json_schema` is a future
   optimization, not v1.
4. **Terminal = single object**, `action` singular, with a free-text **`text`** field (+ optional
   `thoughts`) so the model always has a place to "finish": `NoneAction{action:"none", text, thoughts}`,
   `A2AReply{action:"a2a_reply", text, thoughts}`. Code (`validator.py` `actions[]`) aligns to this.
5. **a2a egress through the proxy** — peers are ACH-fronted; the harness-hosted a2a tools call via
   the localhost proxy with the `ek`. **The `ek` is never exposed** to opencode.

## Proposed phase re-scope (maps to existing ROADMAP)

| Phase | Was | Becomes |
|-------|-----|---------|
| 2 | Engine Swap to Codex | **Reconnect opencode to the v3 config** (model block → EngineConfig; remove override/alias; re-green tree) |
| 3 | Channel Redraw + Drop Hermes | unchanged: drop slack/telegram+Hermes, webhook `source`-select, implement queue+tui |
| 4 | External-MCP Egress | **localhost proxy (model+MCP) + hydration + context fetch + a2a egress MCP** |
| 5 | Conformance Re-green + Integration Guard | unchanged, but guard = opencode + MCP-via-proxy + structured output (SPK-01/#15451 obsolete) |

See `CONTRACT_v3.opencode.DRAFT.md` and `REQUIREMENTS.opencode.DRAFT.md` for the full revised text.
