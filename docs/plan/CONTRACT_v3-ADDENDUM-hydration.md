# CONTRACT_v3 / harness-spec — ADDENDUM: Hydration via `ach-cli` (the real mechanism)

**Status:** ADDENDUM — 2026-06-24. **Additive only.** This document does NOT modify
`CONTRACT_v3.md` or `agent-harness-spec.md`; where it conflicts with their text, the deltas in
§5 below are the correction and take precedence for the hydration boundary.

**Why:** §3 of `CONTRACT_v3.md` described hydration as "the harness self-hydrates … and writes
Codex's `config.toml`". Hands-on with the real tool (`ach-cli env hydrate`, v0.4.5) shows the
hydration is a richer, already-built engine the harness must **invoke, not reimplement**, and that
the model/MCP/a2a config it produces differs from what §3/§9 assumed.

---

## 1. The mechanism — invoke `ach-cli`, do not reimplement

Hydration is a single CLI call against the agent's **workDir**:

```
ach-cli env hydrate <ACH_ENVIRONMENT> --target codex --include-runtime --output <workDir>
```

- It runs a **14-step commit sequence under a workspace lock**: `POST /platform/hydrate` →
  state.json v2/v3 reconciliation → drift detection (4-outcome truth table) → manifest fetch →
  safe tar extraction (bomb-defense caps) → 3-tier auto-claim collision cascade → adapter dispatch
  (closed set: claude-code / codex / gemini-cli / opencode / pimono) → atomic state write.
- **Decision: the harness shells out to `ach-cli`.** Reimplementing the drift truth table, the
  deep-TOML-merge-by-keys, safe-tar, the collision cascade and `state.json` schema 3 in Python would
  be a large, fragile fork. `ach-cli` is the source of truth.
- **`ach-cli` (~10 MB static binary) becomes an image dependency** of the harness (multi-stage
  Dockerfile: explicit `COPY` of the binary; never `COPY . .`).
- Exit codes are meaningful (`0` ok, `2` drift refused, `3` 401/403, `4` env guard, `5` schema
  mismatch, `6` transport, `7` collision, `8` config write). The harness maps a non-zero hydrate to
  an ordinary **startup failure** (exit within `startupTimeoutSeconds` → pod not-ready), per the
  existing §3/§8.5 invariant.

### `--include-runtime` is MANDATORY for governed agents
Without it you get **only context** (plugins/skills). The Environment's **direct** runtime
(models / mcpServers / a2aAgents) — the governed-ACH egress the whole v3 design rests on — is
projected **only** under `--include-runtime`. So it is part of the harness's hydrate contract, not
an option.

---

## 2. What hydration writes into `<workDir>`

Observed tree (`frontend-dev`, `--target codex --include-runtime`):

```
<workDir>/
├── .ach/<env>/
│   ├── lock                          # workspace lock
│   ├── state-codex.json              # drift/merge state, schemaVersion "3" (see §3)
│   ├── runtime-codex/mcp.json        # runtime mirror: [{id, endpoint}, …] (== /platform/hydrate runtime.mcpServers)
│   └── tmp/
├── .agents/skills/<name>/SKILL.md    # downloaded skills (auth flow + tool tables + workflows)
│   └── …/ (LICENSE, lib/, package.json — skill payloads can be multi-file)
├── .codex/config.toml                # codex adapter: model+providers (future) + [mcp_servers.*] + [a2a_agents]
└── .gitignore                        # auto-managed ach-cli block (see §4)
```

**Skills are first-class context** (not mentioned in the original contract). Each runtime/plugin MCP
ships a `SKILL.md` that tells Codex how to authenticate and which tools/workflows exist. They land in
`.agents/skills/` and are read by the engine.

---

## 3. Two classes of MCP server in `.codex/config.toml` — different auth

This corrects §9 ("Streamable-HTTP with `bearer_token_env_var`"). Real output has **two** kinds:

**(A) ACH-direct runtime MCPs** — the governed v1 path (from `runtime.mcpServers`, needs
`--include-runtime`):
```toml
[mcp_servers.mcp-context7]
  url = "https://ach.ackstorm.ai/mcp/mcp-context7"      # the ACH Forwarder
  [mcp_servers.mcp-context7.http_headers]
    x-ach-environment = "frontend-dev"
    x-ach-key = "ek-…"          # the ACH key, written LITERALLY (pk- in dev, ek- for Environment workloads)
```
Header pair is `x-ach-key` + `x-ach-environment` — **not** `Authorization: Bearer` /
`bearer_token_env_var`. This is the §9 "Forwarder fronts egress with the `ek_`" path.

**(B) Plugin-contributed MCPs** — bundled with a plugin, always project (context, no flag needed):
```toml
[mcp_servers.google-oauth-calendar-ro]
  url = "https://api.ackstorm.ai/mcp/mcp-google-calendar-ro"   # litellm passthrough, NOT the ACH Forwarder
  [mcp_servers.google-oauth-calendar-ro.http_headers]
    x-litellm-api-key = "Bearer ${LITELLM_API_KEY}"            # env interpolation, not the ACH key
```
These are **not** governed-ACH egress; they carry their own auth model. (Earlier confusion: the
`x-litellm-api-key` servers were mistaken for the ACH path — they are plugin MCPs.)

`pk-` warning: the CLI warns `pk-` is not Environment-scoped; **Environment workloads (the harness)
use an `ek-`** (= `ACH_TOKEN`).

---

## 4. Security delta — the `ek_` is materialized on disk

`CONTRACT_v3.md §2` says "the config carries paths, never values; the harness reads the file at use
time" and §3 says the `ek_` "is never logged". Hydration **writes the `ek_` in plaintext** into
`<workDir>/.codex/config.toml` (the `x-ach-key` value for ACH-direct MCPs). Consequences:

- `ach-cli` auto-manages `<workDir>/.gitignore` excluding `.ach/ .agents/ .codex/`
  ("agent config carries credentials") — never commit them.
- **The workDir holding the hydrated config must be ephemeral and non-persistent.** Recommended:
  `emptyDir` (ideally `medium: Memory` / tmpfs), **not** the `persistence` volume (§ CONTRACT
  `persistence.mountPath`). Re-hydrate on every boot; do not snapshot the workDir.
- Redaction still applies to logs; this delta is only about on-disk materialization, which is
  inherent to how Codex consumes its config.

---

## 5. Models now come from the Environment (litellm coupling) — selection rule

**Change (incoming):** ACH runtime is coupling to litellm and will **discover the models** present
in each Environment, so `runtime.models[]` (today `[]`) will be populated alongside `mcpServers[]`
and `a2aAgents[]`. Therefore:

- `ach-cli env hydrate … --include-runtime` becomes the **single source** for all three runtime
  axes — **models + MCP + a2a** — projecting `model` + `[model_providers.*]` into
  `<workDir>/.codex/config.toml`.
- **The harness STOPS hand-writing the model/provider block.** This supersedes §3's "the harness
  writes Codex's `config.toml` (`model_provider`→`ACH_BASE_URL`…)" — hydrate does it from the env.

**Model-selection rule (decided — option A):**
- The Environment returns the **menu** of available models (the litellm-discovered set).
- The operator-rendered `config.model.selected` (CONTRACT §2) **picks one** of that menu, and
  `config.model.reasoningEffort` overlays `model_reasoning_effort`.
- **Fail-closed:** if `config.model.selected` is **not** in the Environment's model set, the harness
  **hard-fails at startup** (exit within `startupTimeoutSeconds`). The operator chooses the model;
  the Environment bounds the choice.

---

## 6. Boot sequence (supersedes the §3 "self-hydrate" sketch)

1. Load + validate rendered config (Pydantic hard-fail). Resolve **`workDir`** (must be known first).
2. Resolve the `ek_` credential (`ACH_TOKEN` / `ACH_ENVIRONMENT`).
3. `ach-cli env hydrate $ACH_ENVIRONMENT --target codex --include-runtime --output <workDir>`
   (drift policy → §7). Non-zero exit = startup failure.
4. Validate `config.model.selected` ∈ hydrated model set (§5, fail-closed).
5. Apply `capability.filter.exclude.tools` — withhold tools from the projected Codex toolset
   (the gate above the model, CONTRACT §9). Provisioning, not validation.
6. Start the Codex app-server pointed at `<workDir>` (it reads `.codex/config.toml` + `.agents/skills/`).
7. Start channel adapters → `/readyz` green when adapters listen (engine warmup is not the gate, but
   must reach ready within `startupTimeoutSeconds`).

The §6 invariants (idempotency, dedup→backpressure→lane, three bounds, fail-open memory, etc.) are
unchanged — hydration sits **before** the router path, at boot.

---

## 7. Open items (need a decision before locking implementation)

- **Drift policy on reboot.** With an ephemeral workDir (§4) every boot is a clean hydrate (no
  drift). If a workDir is ever reused, choose: `--force` (operator/env authoritative — recommended,
  matches "config is rendered by the operator") vs default `ConflictPreserve` + `--sync` (preserve
  agent-touched files, purge removed entries). **Recommendation: ephemeral workDir + `--force`.**
- **`ek_` injection into `ach-cli`.** Confirm how the harness passes the `ek_`: `--api-key ek-…`,
  `--env-key <label>`, or `ACH_ENVIRONMENT` + profile. Avoid putting the raw key on the argv
  (process listing); prefer env/profile.
- **`a2a_agents` projection shape** — observed empty (`[a2a_agents]`); confirm the populated TOML
  shape when an Environment has direct A2A agents, for the outbound A2A-client config (§9).
- **Codex version pin** — spike used codex-cli 0.142.0 (#15451 verified there only); `ach-cli`
  v0.4.5. Pin both; re-test on upgrade.

---

## 8. Credential exposure — why the `ek_` cannot live in the agent's config

`ach-cli env hydrate` writes the `ek_` **literally** into `<workDir>/.codex/config.toml` as the
`x-ach-key` value for every ACH-direct MCP (and, once litellm coupling lands, into
`[model_providers.*]` for the model path). Codex is a **code-executing agent** with shell access to
its workDir, so it can read that file. Two threats, only one still open:

- **At-rest leak** (commit / snapshot / layer / logs) — **mitigated**: auto `.gitignore`,
  ephemeral tmpfs workDir (re-hydrate every boot, never on the `persistence` volume), structlog
  redaction.
- **Exfiltration of a replayable credential by a compromised agent** (prompt injection → read the
  `ek_` → post it off-box → attacker replays it after the pod dies) — **open**. This is what §9
  addresses.

**Env interpolation is rejected.** Forcing `x-ach-key = "${ACH_TOKEN}"` instead of a literal gives
**no** protection against a code-executing agent: it reads its own `/proc/self/environ`. Env-interp
only helps at-rest (already covered). Do not spend effort on it under the belief it isolates the key.

**What the agent can do regardless** (do not over-claim): a compromised agent can still *call any
tool it is allowed to call* — that blast radius is bounded by `capability.filter` (provisioning gate,
§9/§5), not by hiding the key. The value of hiding the key is narrower but real: **the credential
never leaves the pod.** A leaked, portable `ek_` is far worse than a short-lived in-pod agent calling
its own tools.

---

## 9. Decision — the harness runs a local egress forwarder

**Decision (2026-06-24):** the harness exposes a single **local egress forwarder** that fronts all
three egress axes — **model, MCP, a2a** — injects the auth headers, and forwards to `ACH_BASE_URL`.
The agent points at the forwarder with **no credential in its config or env**.

```
Codex (agent)                  harness local forwarder                 remote ACH Forwarder
  model_provider.base_url ─┐
  mcp_servers[].url        ├─► 127.0.0.1:PORT (or unix socket) ──(adds x-ach-key + x-ach-environment)──► ach.ackstorm.ai
  a2a_agents[].url        ─┘    ek_ read from the mounted secret FILE at use time
```

- **Post-hydrate rewrite:** after `ach-cli` hydrate, the harness rewrites the projected
  `.codex/config.toml` — swap each `url` / `base_url` to the local forwarder and **strip the
  `x-ach-key` / `x-ach-environment` lines**. The `ek_` then exists only inside the forwarder. The
  ephemeral workDir (§4) means this rewrite never fights `ach-cli`'s drift/state engine (clean
  hydrate every boot).
- **Satisfies `CONTRACT §2` literally:** the secret is read from the mounted **file path at use
  time**, never embedded in agent-readable config or env.
- **It is the natural enforcement + observability choke point:** enforce `capability.filter` *at the
  proxy* (reject a withheld MCP tool call → real enforcement, complementing provisioning); emit
  per-invocation egress **telemetry** (§9 failure surface); rate-limiting later.

### 9.1 This is a conscious reversal of D1 / §9 — frame it correctly
`agent-harness-spec D1` and `CONTRACT §9` state in bold: *"the harness never wraps or re-implements
egress."* The forwarder is a transport wrap, so this **reverses** that decision. The reframe that
keeps the original intent: the local forwarder is a **credential-injection shim in front of the
*remote* ACH Forwarder** (two forwarders in series; the local one exists *only* to keep the `ek_`
out of the agent). It does **not** re-implement egress semantics (no Hermes `send_*`, no in-process
MCP, no posting on the model's behalf) — it is a dumb header-injecting passthrough. Record this
reversal explicitly when locking implementation so it does not read as a contract violation.

### 9.2 Isolation is what makes it a *real* boundary — v1 vs v1.1
Same-process is **not** a hard wall: if the forwarder runs at the same uid as Codex, the agent's
shell can `cat /proc/<pid>/environ` or read the secret-mount file directly. The hard boundary needs
the forwarder + secret mount isolated where the agent cannot reach them (separate **sidecar
container** or a different uid; secret mounted *only* there). This is the one place v1's
single-process **topology A (§15)** needs an exception.

- **v1:** ship the forwarder **same-process** — strictly better than embedding the `ek_` in
  agent-readable config, and satisfies §2 — while documenting the residual `/proc` exposure.
- **v1.1:** isolate the forwarder into a **sidecar** (or distinct uid) for the true boundary.

### 9.3 Spikes required before locking
1. **Streaming model proxy** — the model path is the OpenAI `responses` wire API with **SSE
   streaming on the hot path of every token**. The forwarder MUST proxy streaming transparently (no
   buffering, correct keepalive/timeouts, connection pooling) and add ~zero latency. A bug here
   breaks the engine entirely; MCP/a2a are low-frequency and trivial by comparison.
2. **Codex localhost acceptance** — verify Codex accepts an `http://127.0.0.1` (or unix-socket)
   `model_provider.base_url` **and** `mcp_servers[].url` (it may require https or a localhost flag;
   cf. `ach-cli --insecure` for plaintext localhost).

### 9.4 Strategic alternative (raise with the ACH team)
The cleanest fix is **upstream, not a harness proxy**: ACH mints a **short-lived, pod-scoped,
audience-bound** `ek_`-equivalent per hydrate (minutes-long TTL, valid only for that Environment's
endpoints). Then an exfiltrated credential is near-worthless — no local forwarder, no §15 topology
exception, no config rewrite. The forwarder is the workaround we build **if** ACH cannot issue
short-lived scoped tokens. Open question for ACH: **can it?**
