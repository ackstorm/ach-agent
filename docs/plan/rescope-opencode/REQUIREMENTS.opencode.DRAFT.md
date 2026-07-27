# Requirements — Milestone v1.1 (v3 redesign) — opencode re-scope DRAFT

> **PROPOSAL.** Revised v1.1 requirements for review. Diff against `.planning/REQUIREMENTS.md`.
> Engine reverts from OpenAI Codex to **opencode** (the bridge already exists in
> `src/ach_agent/engine/`). `▲CHANGED` / `▲NEW` / `▲DROP` mark deltas. REQ-IDs preserved where the
> intent survives; renumbered additions use the next free id in each group.

> Scope: re-platform the harness onto **opencode** + **external-MCP egress fronted by a harness
> localhost proxy** per `CONTRACT_v3.opencode.DRAFT.md`, keeping the **router IP** and the
> CONTRACT §6 conformance suite green.

## Config schema (CFG)

- **CFG-04** (unchanged): Operator renders a v3 config validated by the Pydantic hard-fail loader;
  removed v2 blocks (`engine`, `responseActions`, `response`, `webhook.deliver`, `inputSchema`,
  `consentTier`) are rejected by `extra='forbid'`.
- **CFG-05 ▲CHANGED**: `model` is `{ name, type, params }` — `type ∈ {openai, gemini, anthropic}`
  selecting the ACH compat endpoint; `name` resolved against the hydrated models list (else
  hard-fail); `params` an **open, unvalidated** dict splatted to the model client. Other v3 fields
  enforced: `workDir`, `startupTimeoutSeconds`, `limits.maxSteps`, `limits.terminalOutputRetries`,
  `capability.filter.exclude.{tools,mcpServers,skills}`, `governed`.
- **CFG-06** (unchanged): Channel shapes — `webhook.source` (gitlab|github|generic); `queue`
  redis-only `{type:"redis",key,ackMode}`; `cron.timezone` (IANA); `a2a` async block with inbound
  `auth` (header + secretPath).

## Engine — opencode (ENG) — CONTRACT §3/§8

> The opencode bridge (`engine/{client,lifecycle,events,pool,validator,sanitized_env}.py`) **already
> exists and is hardened**. These requirements are **reconnect/reuse**, not rebuild.

- **ENG-09 ▲CHANGED**: The harness owns the `opencode serve` lifecycle (reuse `lifecycle.launch` /
  `pool.EnginePool`) and writes `opencode.json` at hydration: model `baseURL` and `mcp.*` point at
  the **localhost proxy** (§MCP), `capability.filter.exclude` applied. No `ek_` in `opencode.json`.
- **ENG-10** (unchanged intent): One agent turn runs to completion and returns the final message;
  engine not ready within `startupTimeoutSeconds` exits the process (`sys.exit(1)`). `/readyz` gates
  on adapters listening (engine warmup is not a readiness gate).
- **ENG-11 ▲CHANGED**: Terminal output is enforced by the **harness** (Option A, text-based): the
  model's text output is parsed (`validator.py` `extract_actions`), validated against the
  channel-class Pydantic model — a **single object** with `action` + free-text `text` + optional
  `thoughts` (NOT a list) — with ≤1 backstop retry (`terminalOutputRetries`/`repair_turn`), then the
  §8 table: async→`none` (ignore), a2a→callback FAILED, tui→free text. opencode's native
  `format: json_schema` is a future optimization, not v1. Adapt `validator.py` `actions[]` →
  single-object shape.
- **ENG-12 ▲CHANGED**: Per-invocation bounds — `maxInvocationSeconds` wall-clock watchdog
  (`run_invocation`'s `asyncio.timeout` + kill — already built) plus a `maxSteps` step cap (the
  bridge's step-budget abort). Subagent/tool work counts within the same invocation budget.
- **ENG-13 ▲INVERTED**: **Keep** the opencode bridge. **Drop** the Codex direction (never
  implemented — docs only) and **retire** harness-side egress: delete `dispatch_actions` and
  `GitlabCommentAdapter` posting (the agent posts via gitlab-mcp). Remove the Phase-2 mypy override
  and the `ResponseActionBlock` alias (re-greens http/main_wiring tests).

## Egress + secret hygiene — localhost proxy & MCP (MCP) — CONTRACT §9

- **MCP-01 ▲CHANGED**: The harness runs a **localhost reverse-proxy** for the model
  (`/v1`,`/gemini`,`/anthropic`) and for each provisioned MCP server; opencode points only at
  localhost; the proxy injects `Authorization: Bearer ek_` toward `ACH_BASE_URL`. MCP servers come
  from hydration (`runtime.mcpServers`); no real ACH URLs or credentials in `opencode.json`.
- **MCP-02** (unchanged intent): `capability.filter.exclude.{tools,mcpServers,skills}` **withholds**
  capabilities before they are offered to opencode (a gate above the model); an excluded tool/server
  is not callable.
- **MCP-03** (unchanged): A down/erroring MCP server is surfaced as a per-invocation telemetry
  failure (NOT fail-open, unlike memory); the channel never posts on the model's behalf.
- **MCP-04 ▲NEW (secret hygiene)**: The `ek_` MUST NOT appear in `opencode.json`, opencode's env,
  logs, or any request opencode can observe. Conformance asserts this (CONTRACT §6.10).
- **MCP-05 ▲NEW (a2a egress as MCP)**: Peer agents (`runtime.a2aAgents`) are surfaced as
  harness-hosted MCP tools `a2a_{name}`/`_async`/`_status` (port ackbot
  `handlers/a2a/{tools,client,notification_store}.py`), routed through the proxy. The only
  harness-hosted MCP.

## Context — skills / prompts / artifacts (CTX) — CONTRACT §3 — ▲NEW group (plugins dropped)

- **CTX-01 ▲NEW**: The harness self-hydrates by calling `POST {ACH_BASE_URL}/platform/hydrate`
  (`x-ach-key: ek_`, **no CLI**) and downloads each `context.{skills,prompts,artifacts}[]` `tar.gz`,
  decompressing it into its designated directory under `workDir`/`mountPath`.
- **CTX-02 ▲NEW**: **No plugin support** in v1 (no skills+subagents+mcps+hooks bundles). Context is
  skills/prompts/artifacts only.

## Channels (CHN) — CONTRACT §2 — mostly unchanged

- **CHN-06** webhook ingress = generic HTTP + `source`-selected parser/auth (gitlab today;
  github/generic), replacing the gitlab-specific adapter.
- **CHN-07** queue channel consumes redis with idempotency key = redis message id, `ackMode:onComplete`.
- **CHN-08** tui channel reads stdin, streams free-form text to stdout (no terminal contract).
- **CHN-09** a2a channel async-only (a2a-sdk): returns `Task(SUBMITTED)`, POSTs `COMPLETED|FAILED`
  to the caller-supplied callback; inbound caller validated by the header secret. **Reuse the
  existing `channels/a2a.py`**; add the FAILED-on-invalid-terminal path.
- **CHN-10** slack/telegram adapters deleted and `hermes-agent` removed from `pyproject.toml`.

## Conformance (TEST) — CONTRACT §6

- **TEST-02** (unchanged): The 11 CONTRACT §6 invariants stay green against the v3 harness; router
  IP untouched; idempotency-key derivation extended for queue (message id) and a2a (task id); egress
  invariant (§6.9) asserted. **▲ Add §6.10 (secret hygiene): assert the `ek_` never reaches opencode.**
- **TEST-03 ▲CHANGED**: Integration guard exercises **opencode + an external MCP tool via the
  localhost proxy + structured output** together on pinned versions (replaces the Codex #15451 guard).

## De-risk spikes (SPK)

- **SPK-01 ▲DROP/REPLACE**: The Codex SDK↔binary version-skew + #15451 spike is **obsolete** (no
  Codex). Replaced by **SPK-01′ ▲NEW**: confirm opencode `format: json_schema` structured output +
  localhost-proxy model/MCP routing + `ek`-hygiene end-to-end before the egress work (MCP-*) lands.
  Much of this is already proven by the existing bridge + memory MCP path.
- **SPK-02** (unchanged): a2a spike confirms `a2a-sdk` binds the caller-supplied push-notification
  callback (inbound), before CHN-09 lands.

## Out of Scope (additions)

- Plugins (skills+subagents+mcps+hooks bundles) — context is skills/prompts/artifacts only.
- `direct` (non-governed) capability mode.
- Giving opencode the `ek_` directly / pointing opencode at real ACH URLs (always via the localhost proxy).

## Traceability (re-scoped → phase)

| Requirement | Phase | Note |
|-------------|-------|------|
| CFG-04/05/06 | 1 | CFG-05 model block + filter.exclude needs a small Phase-1 schema follow-up |
| ENG-09..13 | 2 | reconnect opencode to v3 config; re-green tree |
| SPK-01′ | 2 | largely already proven |
| CHN-06..10, SPK-02 | 3 | drop slack/telegram+Hermes; webhook source-select; queue+tui; a2a |
| MCP-01..05, CTX-01/02 | 4 | localhost proxy + hydration + context fetch + a2a egress MCP |
| TEST-02/03 | 5 | conformance re-green + integration guard |
