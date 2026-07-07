# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

## [0.7.4] - 2026-07-07

### Added
- **`mcpServers` config block.** New top-level map keyed by server name, STRICT discriminated union
  on `type` — harness-managed MCP servers, a distinct namespace from `runtime.mcpServers` (hydrate's
  ACH-fronted `{id,endpoint}` externals):
  - `repoCheckout` — harness-hosted `checkout_repo(project, ref, subpath?)` tool. Gives the agent an
    **on-disk** repo tree (full-tree `rg`, run tests, build) by reading gitlab-mcp's
    `gitlab://{project}/archive/{ref}` resource **harness-side** with the `ek_` (`x-ach-key`),
    base64-decoding the gzip tar and extracting under `tmpBase` (path-traversal-safe via `tarfile`
    `filter="data"`). Fail-soft; TTL-swept on the next call (`ttlSeconds`). The gitlab MR/note channel
    stamps `head_sha`; the engine prompt gets a one-line `checkout_repo(...)` hint only when the
    facade is wired and a head SHA is present.
  - `local` — PASSTHROUGH stdio MCP: opencode launches the subprocess directly (no ACH proxy).
    Normalized into `opencode.json` `mcp.<name>` (`command` array); `env` lists env NAMES (never the
    `ek_`), resolved harness-side at write time.
  - `remote` — PASSTHROUGH remote MCP: opencode connects directly. `headers` values are `${env:NAME}`
    refs, expanded at `opencode.json` write time.

### Changed
- **`engine.repoCheckout` → `mcpServers` (`type: repoCheckout`).** The repo-checkout tool's config
  source moved out of the engine block into the new top-level `mcpServers` map (`mcpServerId` →
  `sourceMcpServerId`). Behaviour of the facade is unchanged. Breaking config-schema change, but the
  `engine.repoCheckout` block was never released — clean break, no CR migration.

## [0.7.3] - 2026-07-06

### Added
- **GitLab loop-guard + actor allowlist (webhook channel).** Two opt-in, gitlab-only fields on
  the webhook block, both enforced pre-enqueue (a dropped event returns HTTP 200
  `{"status":"ignored"}`, never reaches the router):
  - `botUsername` — the GitLab username the agent posts AS (the egress PAT's user, a distinct fact
    from `agent.name`). When set, inbound events authored by this user, plus gitlab-generated
    system notes, are dropped so the agent never re-triggers on its own comments/MRs. Replaces the
    previous prompt-only self-guarding.
  - `triggerUsers` — actor allowlist; only these GitLab usernames may trigger the agent (applies to
    every routed kind: mr/issue/note). Omit/null → any author triggers.
  Ported from the legacy `ackbot-process` gitlab handler (`bot_username`, `push_users`).

## [0.7.2] - 2026-07-06

### Fixed
- **Adapt to the deployment's real Hindsight tool names.** The harness called canonical
  `hindsight_*` names directly against the configured endpoint. A gateway (e.g.
  `api.ackstorm.ai/mcp/hindsight`) prefixes aggregated tools with the server alias, but the raw
  in-cluster service (`hindsight-api.svc:8888`) may publish them **unprefixed** (`recall` vs
  `hindsight_recall`) — so every call returned `Unknown tool` and memory died silently. At boot
  (`init_hindsight_tool_aliases`, start of `provision_memory`) the harness now runs a `tools/list`
  probe and maps each canonical name to whatever the endpoint actually publishes (exact, bare
  suffix, or `*_<suffix>`); `call_hindsight` routes through that map. Fail-open — on discovery
  error the canonical names are used unchanged.

## [0.7.1] - 2026-07-06

### Added
- **Richer memory facade tool descriptions.** The four agent-facing memory tools now carry
  descriptions a cold agent can act on (when to use each, `recall` vs `reflect`, "a mental model
  is a living auto-refreshing summary") plus `Field` descriptions for `tags` and `mental_model_id`.
- **Memory provisioning is observable.** `provision_memory` now logs `bank ensured` and a per-model
  `mental_model ensured` / `mental_model refresh triggered` on the happy path — previously only the
  final `provisioning complete` (a config count, not a success count) and failures were logged.
- **Webhook ignore log carries `noteable_type`.** A dropped GitLab `note` now logs its
  `noteable_type`, so an ignored comment is diagnosable (issue/commit/snippet vs a routable MR note).

### Fixed
- **`call_hindsight` no longer returns tool-level errors as data.** A Hindsight `isError` result
  (e.g. `Unknown tool`, bad bank) was read as `content[0].text` and returned as a valid string —
  so the error masqueraded as a memory/summary: it was injected into the `## Memory` prompt block,
  returned to the agent from `memory_recall`/`memory_retain` with `status=completed`, and let boot
  provisioning report hollow success while `create_bank`/`create_mental_model` silently failed. The
  seam now raises on `isError`, so every caller degrades and logs loud (facade → "unavailable",
  fetch → skip model, provision → "running degraded") and a backend/version mismatch surfaces at
  boot instead of poisoning the agent's context.

## [0.7.0] - 2026-07-05

### Added
- **Harness-hosted memory MCP facade.** The agent no longer talks to the raw Hindsight MCP
  (≈30 tools, including destructive `delete_bank`/`create_bank`). The harness hosts an in-process
  localhost MCP facade exposing exactly four agent-facing tools — `memory_recall(query, tags?)`,
  `memory_reflect(query, tags?)`, `memory_get_mental_model(id)`, `memory_retain(content, tags?)` —
  and injects the harness-owned `bank_id` plus the admin auth per call. opencode's `memory-0`
  server points at the facade, never at Hindsight; the raw endpoint, `bank_id`, and the admin
  secret never reach opencode.
- **Boot-once memory provisioning.** `provision_memory` ensures the bank and creates/refreshes
  the configured mental models in Hindsight at startup (fail-open — never blocks boot).

### Changed
- **BREAKING (`memory.hindsight` config).** `mentalModels` is now a list of objects
  (`{id, name, sourceQuery, autoRefresh?, maxTokens?}`) instead of `[]string` — the harness needs
  the source queries to provision the models. New optional `auth` (env-only admin secret, Bearer;
  omit for an internal/no-auth URL) and `mission`. The `ach-runtime` operator must render the
  richer block (separate PR); until then only hand-authored local configs exercise it.

### Fixed
- **Static Hindsight bank enforced.** `memory.hindsight.bank` now rejects templating (`{{ }}`) at
  config load (T-04-03), and the divergent per-event bank rendering was removed — the mental-model
  fetch, the facade, and the prompt's `{{ memory.bank }}` all use one static bank. Per-repo
  partitioning is via tags, not a templated bank.

## [0.6.8] - 2026-07-04

### Added
- **A2A `message/send` now supports non-blocking delivery.** `execute()` emits one interim
  `WORKING` `TaskStatusUpdateEvent` before it blocks on the out-of-band completion. This gives
  the a2a-sdk's existing non-blocking path (`SendMessageConfiguration.return_immediately`, which
  runs `execute()` as a background producer and breaks on the first task-creating event) an event
  to break on — so a caller sending `return_immediately: true` gets its `task_id` back immediately
  and polls `GetTask` for the terminal result, instead of holding the request for the whole engine
  run. The blocking path (`return_immediately: false`, e.g. the LiteLLM proxy) is unchanged: the
  aggregator processes but does not break on `WORKING` and still returns the terminal `COMPLETED`
  task. The engine and the `{action, text}` terminal contract are untouched; the whole change is
  the interim event, placed after all reject branches so no rejected request emits a dangling
  `WORKING`. Verified end-to-end against a2a-sdk 1.1.0 (task_id returned in ~1-5ms vs a 50ms
  engine; `GetTask` returns `COMPLETED` after completion).

## [0.6.7] - 2026-07-04

### Fixed
- **A2A card now advertises a native 1.x JSON-RPC interface — 1.x clients invoke it instead
  of `-32601`.** Follow-up to v0.6.6. With `url` present but no `supportedInterfaces`, a
  a2a-sdk 1.x client's `parse_agent_card` synthesized an interface from `url` and defaulted
  its `protocolVersion` to `0.3.0` → the client factory chose the legacy
  `CompatJsonRpcTransport` and sent JSON-RPC `message/send`, which our 1.x handler rejects
  with `-32601` (it speaks `SendMessage`). The served card now advertises
  `supportedInterfaces: [{protocolBinding: JSONRPC, protocolVersion: "1.0"}]` +
  `preferredTransport` + top-level `protocolVersion: "1.0"`, so 1.x clients pick
  `JsonRpcTransport` (`SendMessage`). 0.3.x consumers ignore the unknown fields and still
  read `url` + `skills` — verified the served card validates under a2a-sdk 0.3.24 and parses
  as a non-legacy 1.0 interface under 1.1.0.

## [0.6.6] - 2026-07-04

### Added
- **Per-channel terminal output-format directive.** The harness now appends a channel-class
  `<output_format>` block to every structured turn (a2a → `a2a_reply`; webhook/cron/queue →
  `none`; tui skipped), telling the model which terminal action its final object must carry.
  Nothing did this per turn before, so a model could emit a valid-but-wrong
  `{"action":"none"}` on an a2a turn — which `extract_terminal` accepts (the repair turn
  never fires) and the a2a path delivers to the caller as a FAILURE. `terminal_action_for`
  is the single source of truth, reused for the up-front block and the lifecycle repair/wrap
  turns, so an a2a turn never sees `none` on any surface.
- **`{{ payload | json }}` template filter.** Serializes a whole container (dict/list) as
  compact JSON — the only way to emit a non-scalar. Scalar paths still substitute as before;
  a genuinely missing path still renders empty.

### Fixed
- **A2A agent card is now parseable by a2a-sdk 0.3.x consumers (e.g. LiteLLM proxy).** The
  card at `/.well-known/agent-card.json` (built with a2a-sdk 1.1.0) omitted the top-level
  `url` and `skills` that 0.3.x requires, breaking card resolution for consumers pinned to
  `a2a-sdk<1.0` (which resolve the card before every `message/send`). The served dict now
  injects `url` + `skills: []`; `defaultInputModes`/`defaultOutputModes` (also required by
  0.3.x) are set on the card; and the card is served at the legacy `/.well-known/agent.json`
  too. Safe for 1.x consumers (lenient `parse_agent_card`). Verified against real 0.3.24.

## [0.6.5] - 2026-07-04

### Fixed
- **A2A channel: terminal status events now carry `task_id`/`context_id`.** `_status_event`
  built every `TaskStatusUpdateEvent` with empty ids, so a2a-sdk 1.1.0's
  `TaskManager.save_task_event` raised `InvalidParamsError("Context in event doesn't match
  TaskManager ...")` and every `message:send` returned HTTP 500 — the engine reply was
  computed but never delivered. The ids are now threaded from the `RequestContext` into all
  emit paths (auth/reject, cancel, and the out-of-band completion/failure callbacks, which
  read them from a `_pending` 4-tuple).

### Added
- **`prompt.compose` replace wired via `agent.build.prompt`** — the composed prompt override
  now flows through the opencode agent build.

## [0.6.4] - 2026-07-03

### Changed
- **Repo-wide dead-code sweep** (ponytail audit): dropped unused `A2ANotificationStore`,
  `SanitizedEnv`, `OnKillCallback`, `_trace_sse` wrapper, `memory/adapter.py` re-export shim,
  router `AtomicCounter` (→ plain `int`), dead `Lane.is_done`, an inert channel-registration
  loop in `main.py`, the D-03 `user_consented` throwaway field, and `ach_stats`' unused
  `pydantic` dependency. Data-driven rewrite of the channel type/block coherence validator
  (same error messages). No behavior change — net ~-420 lines.

## [0.6.3] - 2026-07-03

### Added
- **`provider.<id>.whitelist = [<model>]`** in the generated `opencode.json` — restricts
  opencode's model picker (notably the TUI) to the single configured model, so a built-in
  provider (google/anthropic) no longer exposes its whole catalog and the agent can't switch
  off the operator's model.

### Changed
- **`model.params` now renders into per-model `options`** (`provider.<id>.models.<model>.options`)
  instead of provider-level options. opencode forwards model options as per-call
  `providerOptions`, so this is where reasoning/generation knobs take effect — gemini
  `thinkingConfig.thinkingLevel` (`low|high`) / `thinkingBudget` (tokens), openai
  `reasoningEffort`, `temperature`, etc. Provider-level options stay connection-only
  (`apiKey`/`baseURL`). Existing configs (no `params`) are unaffected.

## [0.6.2] - 2026-07-03

### Fixed
- **`model.type` now selects the opencode provider and native wire.** A `type: gemini` model
  was written to `opencode.json` on the `ach` / `@ai-sdk/openai-compatible` provider pointing
  at `/v1`, so every invocation hit `/v1/chat/completions` and litellm 400'd
  (`Invalid model name passed in model=gemini-flash-latest`). Two causes, both ignoring the
  type: the provider block was hardcoded to `ach`, and the model-proxy path was taken from the
  hydration manifest endpoint (ACH reports every model at `/v1`). Now `model.type` drives both:
  `gemini` → built-in `google` provider on `/gemini/v1beta` (native `generateContent`),
  `anthropic` → built-in `anthropic`, `openai` → custom `ach` (unchanged). The proxy also drops
  the dummy `x-goog-api-key` so it never reaches ACH. See
  `docs/references/2026-07-03-provider-by-model-type.md`.

## [0.6.1] - 2026-07-03

### Changed
- **BREAKING (refines 0.6.0): `channel.session` shape reworked** from an overloaded `key`
  (one field meaning `"none"` / `"auto"` / a `{{ }}` template) to a `type` discriminator:
  `type: auto|none|custom` (default `none`). `key` is now the `{{ }}` template only — **required
  iff `type: custom`**, rejected otherwise. `maxTokens` / `overflow` unchanged and still apply to
  `auto` / `custom`. String shorthand still works: `session: auto|none` → `{type}`; any other
  string (a template) → `{type: custom, key: <str>}`. The router lane key (`event.session_key`)
  is untouched.

## [0.6.0] - 2026-07-03

### Changed
- **BREAKING: `channel.session` is now a block**, not `auto|none`. Shape:
  `{key, maxTokens, overflow}` (string shorthand `session: auto|none|"{{ … }}"` still maps to
  `{key: …}`). **Default changed to `key: "none"`** — conversation memory across turns is now
  opt-in, not automatic; the operator sets a `{{ }}` key template to enable it (e.g.
  `{{ payload.task_id }}` for a queue channel). `none` deletes its opencode session post-turn
  instead of leaking one row per event into the persistent home.
- `maxTokens` + `overflow: compact|rotate` bound conversation growth: `compact` calls
  `POST /session/{id}/compact` in place (default once memory is opted into); `rotate` evicts the
  LRU entry and deletes the old session.

### Added
- **Persistent `session_key` → opencode-session map**, pool-owned (LRU), with a 404 stale-id
  fallback: `channel.session: auto`-style continuity now survives idle-TTL opencode server
  restarts instead of resetting to a fresh conversation.
- `oc_session_id` exposed via stats for observability into which opencode session a turn landed on.
- **Stats dashboard**: daily usage chart (spend/sessions/tokens toggle), a sessions-this-month
  breakdown panel, and 7d/30d/90d range presets on the Leaderboard UI.

### Fixed
- Session-stat `model` field now comes from the configured engine, not the opencode reply object
  (which never carried model metadata) — it no longer reports `"unknown"`.

See `docs/references/2026-07-02-session-identity-and-bounds.md` for the full design rationale.

## [0.5.0] - 2026-07-02

### Security
- **Boot security preflight.** At startup the harness hardens its own process
  (`PR_SET_DUMPABLE=0`, `PR_SET_NO_NEW_PRIVS=1`) so a co-resident opencode agent (same uid)
  cannot read the harness's `/proc/<pid>/{environ,mem}` or ptrace it, and fail-closes on unsafe
  host gates (running as root, `CAP_SYS_PTRACE`, `CAP_SYS_ADMIN`). A soft-warn nudges toward
  dropping the capability bounding set. `ACH_INSECURE_ALLOW_DEGRADED=1` downgrades the host gates
  to warnings for local dev; the two `prctl` hardenings are always enforced.
- **Inbound-auth secrets are env-only.** `webhook.auth.secret` / `a2a.auth.secret` are read from
  environment variables the operator injects (e.g. via `secretKeyRef`); the rendered config carries
  env **names**, never values. Secret env values are redacted from logs, and any secret env name is
  stripped from `engine.forwardEnv` (with a warning) so it never reaches opencode's clean-slate env.

### Added
- **Keyed engine pool.** The opencode engine pool is keyed by `session_key` — one agente ⇄ one
  `session_key` (1:1). Distinct keys run parallel agentes; the same key reuses one, serialized by
  the router lane. Per-session opencode config via `OPENCODE_CONFIG` over a shared HOME.
- **Warm engine reuse + `channel.session: auto|none`.** Configurable idle TTL keeps an agente warm
  for session continuity; `auto` reuses the opencode session across turns, `none` starts fresh.
- **Engine resilience.** Bounded, health-gated SSE reconnect on the live invocation path;
  mid-invocation liveness fails fast when opencode dies during a turn; a single owner enforces
  `maxInvocationSeconds` with force-kill on timeout and reserved-port release on stop.
- **Step-budget abort.** Runaway turns are bounded by tool-call count, followed by a wrap-up
  correction turn so the agent still produces a terminal response.
- **Per-turn observability.** Structured logs for the prompt, each tool call (once, completed/error),
  and a per-turn summary (tools, tokens, cost, duration).
- **Configurable GitLab event routing.** `webhook.gitlabEvents` selects which events route
  (MR / issue / comments), ignores the rest, and never 422s on non-routable notes; per-kind default
  prompts (MR / issue / comment). GitLab dual-key dedup adds a logical-content composite as a
  secondary dedup key. Webhook 202 responses carry a correlation `X-ACH-Task-Id`.
- **Memory backend package.** Per-`memory.type` boot-static tool spec in the system prompt;
  `codemem.project` / `hindsight.bank` are `{{ }}`-templated from the event; strict nested
  `memory.<type>.*` schema.
- **Frozen config JSON Schema.** Published `docs/schemas/agent-config-v1.schema.json` with a drift
  guard and `make schema` target.
- **`ach-stats` service (sub-project A).** A standalone dashboard service reading the harness
  `ach:sessions` redis stream and serving a usage leaderboard (FastAPI + React SPA). The harness gains
  a non-blocking `StatsSink` (supervised redis writer with `XADD`+`MINID` trim, Prometheus counters).

### Changed
- **BREAKING — `auth.secretPath` removed; `auth.secret` is a `{env: NAME}` source.** File-backed
  secrets are gone: a `{file}` source is rejected at config load. Operators must inject the secret as
  an environment variable and reference it by name. Lockstep change with `ach-runtime`.
- Webhook config key `gitlab_events` renamed to camelCase `gitlabEvents`.

### Fixed
- **HTTP server always boots** so `healthz`/`readyz`/`metrics` are reachable for every config —
  including cron-only / queue-only agents with no inbound HTTP channel (CONTRACT §4). Previously the
  server was gated on a webhook/a2a channel, so k8s liveness/readiness probes could kill those pods.
- Messages are accepted independently of engine readiness (the acceptance path no longer blocks on a
  warm engine), and the warm engine pool is stopped on graceful shutdown (no orphaned opencode).
- `CODEMEM_PROJECT` is pinned so codemem cross-session recall works; the engine HOME layout uses the
  contract-correct prompt path and an off-volume tmp dir.

## [0.4.1] - 2026-07-01

### Added
- **`prompt.system` typed source: `text` | `file` | `ach`.** The persona can be inline
  (`{type: text, text: "…"}`), a hydrated prompt file addressed by path
  (`{type: file, file: "prompts/<name>/<f>.md"}`), or a hydrated prompt addressed by name
  (`{type: ach, ach: "<name>", file?: "<subpath>"}`, the preferred form — the harness
  resolves the prompt dir's sole file, or the given subpath). Paths resolve under
  `<home>/.ach-state`; absolute or `..` is rejected at load and re-checked (real path) at
  read time; a missing file/dir is a hard boot failure (never fail-open).
- **`memory.type` backend union: `hindsight` | `codemem`.** `hindsight` (default) keeps the
  existing `endpoint`/`bank`/`mentalModels` shape; `codemem` is a local stdio-MCP,
  model-managed backend taking an absolute `dbPath`. A legacy memory block with no `type`
  defaults to `hindsight`. codemem fails open (PATH probe) and is wired into opencode.json.

### Changed
- **BREAKING — `prompt.system` is no longer a plain string.** The bare-string form is
  rejected; the operator (and hand-authored configs) must render the object form above.
  Lockstep change with `ach-runtime`.
- **Hydrated `prompts`/`artifacts` relocated to `<home>/.ach-state/{prompts,artifacts}/<name>`**
  (was `<mountPath>/{kind}`); a `<workDir>/.ach-state` symlink gives the agent one path.
  Skills are unchanged (`<home>/.config/opencode/skills/<name>`).

### Removed
- **Helm chart + Kustomize base (`deploy/`)** and the chart-publish step in the release
  workflow. The harness is deployed by the `ach-runtime` operator, which owns the
  `Deployment` (CONTRACT §1); the standalone chart was a redundant, unexercised second
  deployer. Released container images are still published to `ghcr.io/ackstorm/ach-agent`.

## [0.4.0] - 2026-06-30

### Added
- **`engine.forwardEnv`** (config) — a list of extra env var NAMES to forward from the
  harness env into the opencode subprocess. Defaults to empty.
- **`capability.filter.exclude.mcpServers` + `.skills`** — withhold MCP servers and ACH
  skills before they reach the model (governance gate); plus existing `.tools` now enforced
  via opencode.json tool-disable.
- **`header_token` webhook auth** — static shared secret in a configurable header.
- **Per-channel `concurrency`** — each channel's `concurrency` is now a real sub-cap under
  the global `maxConcurrentInvocations`.
- **`maxSteps` and `terminalOutputRetries`** are now honored (were parsed but ignored).
- **`engine.home`** (config) — the opencode HOME (config, hydrated skills, sessions,
  node_modules). Definable; defaults to `<persistence.mountPath>/home` when persistence is
  enabled, else `/tmp/ach-home`. `engine.workDir` now defaults to `<home>/workspace`.

### Changed
- **`memory.scope` renamed to `memory.bank`** — the static memory bank_id (the agent's
  mission namespace, e.g. `gitlab-pr-review`). Per-event tag-based partitioning is a
  separate future layer and does not affect this field.
- **`channel.prompt` is now rendered** through a zero-dependency `{{ }}` substitution
  engine. Namespaces: `payload.*` (inbound JSON body) and `internal.*` (`channel.name`/
  `type`/`source`, `agent.name`, `memory.bank`, `event.id`, `session.key`); one filter,
  `| default("x")`. There is no `env` namespace — process env (the `ek_`) is structurally
  unreachable from a template (ek-hygiene at the template layer). Channels without a
  `prompt` keep the previous built-in instruction behavior unchanged.
- **Config reshape:** `workDir` + `startupTimeoutSeconds` moved under `engine`;
  `prompt.base` → `prompt.system`.
- **`--tui` now attaches to opencode's native TUI** via `opencode attach` against the
  harness-prewarmed `serve` (egress hygiene preserved — model + MCP still flow through the
  localhost proxies that inject the `ek_`). `--debug` remains the plain stdin/stdout REPL.
- **ACH skills now load in opencode** — hydrated skills extract flat into
  `<home>/.config/opencode/skills/<name>/SKILL.md` (the directory opencode scans), instead of
  `persistence.mountPath/skills/<qualified-name>/<name>/` (which opencode never read).
- **opencode HOME is now a single stable dir** (`engine.home`) instead of a fresh per-server
  `mktemp`, so sessions and node_modules persist. `tui-attach.log` moved to a volatile
  `/tmp/ach-harness/`.

### Removed
- **`agent.namespace`, `agent.generation`, top-level `governed`, `channels[].session`,
  `channels[].expire`, `engine.idleTtlSeconds`** — inert or redundant; dropped to close the
  contract.
- **`ACH_OPENCODE_BIND_HOST` and `ACH_OPENCODE_PORT`** — opencode `serve` now always binds
  loopback (`127.0.0.1`) on a free ephemeral port. The off-host web-UI exposure they enabled
  is obsolete now that `--tui` uses `opencode attach` (co-located, loopback); dropping the
  `0.0.0.0` bind also removes an unauthenticated-API footgun.
- **Legacy direct-gateway model mode** — opencode.json no longer falls back to
  `{env:ACH_API_KEY}` / `{env:ACH_BASE_URL}` when no localhost proxy is configured. opencode
  always reaches the model through the localhost model-proxy, so **`ACH_TOKEN` (the `ek_`) is
  now required**: the harness hard-fails at boot without it (no model endpoint). Removes the
  one path where opencode read a key directly from its env.
- **`EngineConfig.session_dir`** — dead field (set, never read); opencode persists sessions
  under `<home>/.local/share/opencode`.

### Security
- **opencode's subprocess env is now built clean-slate** instead of inheriting the full
  harness environment. opencode gets only a small base allowlist (`PATH`, `SHELL`, `LANG`,
  …) plus any names in `engine.forwardEnv`; `HOME`/`TMPDIR` are pinned to its ephemeral
  home. This enforces CONTRACT §3 — the `ek_` (`ACH_TOKEN`/`ACH_API_KEY`) never reaches
  opencode in proxy mode (previously the subprocess inherited `**os.environ`, including the
  bearer). Legacy local-dev mode (no localhost proxy) still forwards `ACH_API_KEY`/
  `ACH_BASE_URL`, which opencode.json dereferences directly.

## [0.3.3] - 2026-06-27

### Added
- **`ACH_OPENCODE_PORT`** pins the opencode `serve` port (default `0` = ephemeral). Set a
  fixed port so a container can publish it (`ports:`) and the opencode web UI is reachable
  from the host. Pair with `ACH_OPENCODE_BIND_HOST=0.0.0.0`. Dev/test only — collides if two
  harness instances share the port.

### Fixed
- **Queue consumers are stopped before the graceful drain** so in-flight redelivery can't
  re-enqueue work mid-drain.
- **Redis `xack` failures are guarded** in the queue consumer (a failed ack no longer
  escapes the consume loop).
- **Dedup mark reads the clock once** in the router, removing a read-time skew between the
  TTL stamp and the window check.
- **Removed the 300s cap on MCP/model proxy upstream calls** — long-running upstream
  requests (e.g. a slow MCP tool) are no longer truncated by the localhost proxy.

## [0.3.2] - 2026-06-27

### Added
- **`ACH_OPENCODE_BIND_HOST`** controls the interface opencode `serve` binds to (default
  `127.0.0.1`). Set to `0.0.0.0` to expose the opencode HTTP API + web UI on all interfaces
  (e.g. to open the web UI from your host's browser). Binding a non-loopback interface logs a
  loud security warning — the opencode API runs without authentication, dev/test only. The
  harness HTTP client always connects via loopback regardless.
- **`--tui` pre-warms opencode at boot**: the console now launches the opencode server up
  front (instead of lazily on the first prompt) and holds it for the whole REPL, so there is
  no idle TTL between prompts — only Ctrl-C / EOF ends the session.

### Changed
- **Engine idle TTL is now a per-channel constant** (`_CHANNEL_IDLE_TTL_S`), `0` for all v1
  channels, so the opencode server stops as soon as a conversation ends. Replaces the global
  `ACH_ENGINE_IDLE_TTL_SECONDS` env / `engine.idleTtlSeconds` resolution. The
  `engine.idleTtlSeconds` config field is retained for back-compat but no longer has any effect.

## [0.3.1] - 2026-06-26

### Changed
- **opencode runtime bumped `1.16.0` → `1.17.11`** (`OPENCODE_VERSION` in the Dockerfile).
  Verified live against ACH (streaming console + tool chrome + calendar MCP flow).

## [0.3.0] - 2026-06-26

### Added
- **Live streaming console**: the `--tui`/`--prompt` console now streams the assistant's
  text as it is produced and shows one-line tool-lifecycle chrome (`⚙ running` / `⚠ error`),
  so a long-blocking tool (e.g. a calendar `auth_wait`) is no longer dead air. Text comes
  from opencode's cumulative `message.part.updated` snapshots (suffix-diffed per part).
- **Model-proxy upstream override (dev/test only)** via `ACH_MODEL_BASE_URL` /
  `ACH_MODEL_HEADER` / `ACH_MODEL_TOKEN`: swap just the model backend (hydration + MCP stay
  on `ACH_BASE_URL`) to A/B a different gateway. The token is injected verbatim as the header
  value. Uses a raw provider key, not the `ek_` — bypasses ACH governance; never for production.
- **`ACH_DEBUG_SSE=1`** raw per-event SSE trace and **`ACH_LOG_LEVEL`** console verbosity knob.
- **Boot-time MCP tool probe**: lists the tools each hydrated MCP server exposes and warns when
  a server returns zero (surfaces an unauthorized/unconsented server at boot, not mid-invocation).

### Changed
- **`capability.ach.baseUrl` is now optional and overridable via `ACH_BASE_URL`**: the env var,
  when set, wins over the contract's `baseUrl` (and supplies it when the contract omits it).
  `load_config` hard-fails only if neither provides a host. The shipped `docker/` sample +
  quickstart configs no longer carry a hardcoded ACH host — they expect `ACH_BASE_URL` at runtime.

### Fixed
- **Clear boot failure on an unresolvable model endpoint**: when neither an `ek_` (`ACH_TOKEN`)
  nor `ACH_BASE_URL` is set, the harness now exits at boot with an actionable message instead
  of letting opencode fail every invocation with the opaque
  `"/chat/completions" cannot be parsed as a URL`. Also warns when `ACH_MODEL_TOKEN` carries an
  empty credential (e.g. an unexpanded `${...}` var).

## [0.2.1] - 2026-06-26

### Fixed
- **Proxy teardown latency**: bounded the localhost MCP/model proxy `shutdown_timeout` to 1s.
  aiohttp's 60s default made shutdown hang ~60s after the reply when a long-lived MCP/SSE
  stream was still open.

### Changed
- **Slimmer image (−21MB, 339→318)**: dropped `uvicorn[standard]` (the uvloop/httptools/
  websockets/watchfiles extras aren't needed for this single-replica surface) and pruned
  `__pycache__`/`*.pyi`/bundled test suites from deps.
- **`capability.ach.environment` is now optional** (defaults to `"platform"`): the EK scopes
  the environment server-side, so hand-authored configs can omit it.

### Added
- `docker/quickstart/` — standalone `docker-compose.yaml` + `config.yaml` using the public
  image (`docker compose run --rm agent`).

## [0.2.0] - 2026-06-26

The v1.1 milestone — engine rescoped to **opencode** with localhost-proxy ek-hygiene,
full v3-contract alignment, and a zero-friction local dev-loop.

### Added
- **YAML-authored contracts**: `load_config` accepts `.yaml`/`.yml`, validated against the
  same schema as the rendered JSON contract.
- **`--prompt TEXT`** one-shot launch modifier (non-interactive sibling of `--tui`): boot,
  run a single free-form prompt, print the reply, exit.
- **`--tui`** launch modifier: boot engine/proxies/hydration and run a console REPL,
  ignoring configured channels (the typed line is the prompt).
- **Localhost reverse-proxy** fronting model + MCP that injects the ACH key (`ek`), so
  opencode never sees it (§6.10 ek-hygiene).
- **Hydration** via `POST /platform/hydrate`; context (skills/prompts/artifacts) downloaded
  and extracted (path-traversal guarded).
- **Channels**: webhook (gitlab|github|generic + auth), cron, queue (redis Streams), a2a (async).
- **Runnable container**: opencode baked in, default contract baked, `ENTRYPOINT` so
  `docker run -it -e ACH_TOKEN=ek-... IMAGE --tui` works with zero mounted files.
- `prompt.base` wired into opencode's append-mode `instructions` (inline agent persona).

### Changed
- Engine reverted to **opencode** (`opencode serve` + SSE); single-object terminal contract
  (`{action,text,thoughts}`), harness-validated (extract + Pydantic + ≤1 repair).
- ACH auth via the **`x-ach-key`** header; `ek-` keys; `runtime.models` are objects
  `{id,endpoint}` and the model proxy uses the model's real endpoint path.

### Removed
- slack/telegram channels + the Hermes dependency; harness-side delivery (egress is
  model-initiated via external MCP, never posted on the model's behalf).

## [0.1.0] - 2026-06-22

First public release — the v1.0 MVP of the ACH execution-plane harness.

### Added

- **Engine bridge** — drive the opencode engine over HTTP/SSE (`opencode serve`): subprocess
  launch with a startup deadline (`sys.exit(1)` on timeout), SSE text-accumulation action
  extraction, the `maxInvocationSeconds` watchdog, a bounded repair turn, a shared/TTL engine
  pool, and `ek_` bearer redaction.
- **Router (core IP)** — per-session FIFO lanes enforcing the pinned `dedup → backpressure →
  lane` order with three always-enforced finite bounds (`maxConcurrentInvocations`,
  `maxInvocationSeconds`, `maxQueuedTotal`).
- **Channels** — `webhook` (GitLab MR, HMAC body auth, real dedup from `X-Gitlab-Event-UUID`),
  `slack`, `telegram`, `a2a`, and `cron` (croniter with deterministic dedup keys), all
  normalized to a canonical `MessageEvent`.
- **Actions** — `reply` and `gitlab_comment` delivery (synchronous and out-of-band); a
  consent-gated, dry-run `sideEffect` path with an audit trail.
- **Durability** — dedup store with a split fail policy, graceful drain on `SIGTERM` preserving
  in-flight invocations, and the A′ proven-start admission gate.
- **Memory** — fail-open memory adapter: degrade and continue when the backend is unreachable.
- **HTTP surface** — inbound channel events, `/healthz`, `/readyz`, and `/metrics`.
- **Conformance** — the authoritative CONTRACT §6 conformance suite (`make conformance`),
  chained into `make verify`.

[unreleased]: https://github.com/ackstorm/ach-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ackstorm/ach-agent/releases/tag/v0.1.0
