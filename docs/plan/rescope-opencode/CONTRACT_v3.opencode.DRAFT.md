# ACH Agent Runtime — Shared Contract v3 (the seam) — opencode re-scope DRAFT

> **PROPOSAL.** Full revised contract for review. Diff against `docs/plan/CONTRACT_v3.md`.
> Changes vs. the Codex draft are marked `▲CHANGED` / `▲NEW` / `▲REMOVED` in section headers.
> Unchanged sections (1, 4, 5, router invariants) are reproduced verbatim for a clean diff.

This document is the **single frozen interface** between `ach-runtime` (operator, Go)
and `ach-agent` (harness, Python). Both repos depend on this; neither may change it
unilaterally. Source of truth lives in `ach-runtime`; `ach-agent` pins a version.

Spec reference: `ach-agent-runtime-spec-v1_4_7.md` (API group `runtime.ackstorm.ai/v1alpha1`).

> **STATUS: DRAFT.** v3 is a deliberate simplification of v2. Structural changes that ripple
> into this seam:
> 1. **Egress is external MCP tools, not channel delivery.** `responseActions`, `inputSchema`,
>    `consentTier`, `webhook.deliver`, and `response` are **removed**. The agent acts by calling
>    **external MCP tool servers** (e.g. `gitlab-mcp`); the harness no longer dispatches actions.
> 2. **Channel set redrawn.** v1 = `webhook` (`source`-selected), `cron`, `queue`, `tui`, `a2a`.
>    No `slack`/`telegram`/`openai-compatible`. `gitlab` = `webhook` + `source: gitlab`.
> 3. **▲CHANGED — Engine is opencode (`opencode serve` + SSE), hardcoded; the `engine` block is
>    removed; `model` stays.** opencode is a complete, provider-agnostic agent with a config-driven
>    MCP client and structured output (`format: json_schema`). The harness owns the `opencode serve`
>    lifecycle and writes `opencode.json` at hydration. The opencode bridge already exists in the
>    harness (`src/ach_agent/engine/`) and is reused, not rebuilt.
> 4. **▲CHANGED — Structured output is a fixed terminal contract** validated by the harness
>    (Pydantic + ≤1 backstop retry). opencode returns best-effort structured JSON; the harness is
>    the enforcer (§8). Not a per-channel config schema.
> 5. **▲NEW — The harness fronts the model + MCP via a localhost proxy.** opencode points only at
>    `http://localhost/...`; the proxy injects the `ek_` toward ACH. The `ek_` never appears in
>    opencode's config or environment (§3/§9).
> 6. **▲NEW — ACH context is skills / prompts / artifacts only (no plugins).** Each is a `tar.gz`
>    decompressed into a directory at hydration (§3).
>
> The router (§6) — dedup → backpressure → lane, the three finite bounds — is **unchanged**. It is
> the repo's IP. Harness language stays **Python**.

---

## 1. Direction of dependency (non-negotiable) — UNCHANGED

```
ach-runtime  ──renders──▶  rendered runtime config  ──read──▶  ach-agent
                          + ACH_* env (governed)
ach-agent    ── NEVER reads CRDs, NEVER talks to the API server, NEVER writes Agent.status
```

The harness has **no Kubernetes RBAC**. Status is the operator's job, derived from `pod.status`
only. The harness is tested against a hand-written rendered config.

---

## 2. The rendered runtime config (operator writes → harness reads) — ▲CHANGED

One flat **JSON** file mounted at `/etc/ach-agent/config.json`. Machine→machine seam; the harness
validates with Pydantic v2 (`extra='forbid'`, `strict=True`) and hard-fails (`sys.exit(1)`) on any
mismatch.

```jsonc
{
  "schemaVersion": "1",                     // RESOLVED (D-03): harness validates "1"; ach-runtime renders "1".
  "agent": { "name": "gitlab-ackstorm", "namespace": "engineering", "generation": 5 },

  // NO "engine" block — engine is opencode, hardcoded. The harness writes opencode.json at
  // hydration, pointing at the localhost proxy (§3/§9). No "model.provider" (retired in v2).
  "model": {                                // ▲CHANGED — new shape
    "name": "openai.gpt-5",                 // model id, passed verbatim; MUST be in hydrated models
    "type": "openai",                       // openai | gemini | anthropic — picks the ACH compat
                                            //   endpoint the localhost proxy exposes
    "params": { "temperature": 1 }          // OPEN, UNVALIDATED dict, splatted to the model client
                                            //   (temperature, thinking_level, …). User's fault if it breaks.
  },
  "workDir": "/workspace",                  // where opencode operates (baseDir)
  "startupTimeoutSeconds": 30,              // §8.5 deadline: exceed → process exits
  "governed": true,                         // derived from capability.type == "ach"
  "capability": {                           // §3 — folds in hydration
    "type": "ach",                          // ach only in v1 (direct is out — §7)
    "ach": { "baseUrl": "https://ach.ackstorm.ai", "environment": "engineering-prod" },
    "filter": {                             // ▲CHANGED — gate ABOVE the model (withhold before offering)
      "exclude": {
        "tools": ["gitlab_merge_merge_request"],   // withhold named MCP tools
        "mcpServers": ["dangerous-admin"],         // ▲NEW — withhold whole MCP servers
        "skills": ["send-email"]                   // ▲NEW — withhold hydrated skills
      }
    }
  },
  "prompt": { "base": "…agent persona (markdown ok)…", "compose": "append" },
  "memory": {                               // null if not configured; fail-open (§31)
    "endpoint": "http://hindsight.engineering.svc:8080",
    "mission": "AI code reviewer…", "scope": "{project_id}",
    "mentalModels": ["architecture", "conventions", "recurring-issues"]
  },
  "limits": {                               // §18.6 — all finite, always enforced
    "maxConcurrentInvocations": 8,
    "maxInvocationSeconds": 1800,
    "maxQueuedTotal": 100,
    "idempotencyWindowSeconds": 3600,
    "maxSteps": 50,                         // max agent steps per invocation (opencode step cap)
    "terminalOutputRetries": 1              // §8 — harness validates + ≤1 backstop retry
  },
  "persistence": { "enabled": true, "mountPath": "/var/lib/ach-agent" },
  "health": { "host": "0.0.0.0", "port": 8000 },
  "channels": [
    { "name": "gitlab-mr-review", "type": "webhook", "source": "gitlab",
      "concurrency": 4, "expire": 300,
      "session": { "mode": "auto", "continuity": "durable", "ttlSeconds": 604800 },
      "prompt": "Review this merge request: {{ .object_attributes.url }}",
      "webhook": { "auth": { "type": "gitlab_token",
                             "secretPath": "/etc/ach-agent/secrets/gitlab-webhook/secret" } } },

    { "name": "daily-security", "type": "cron", "concurrency": 1,
      "cron": { "schedule": "0 8 * * 1-5", "timezone": "Europe/Madrid" },
      "prompt": "Scan main for new CVEs; open an issue via your tools if any are critical." },

    { "name": "ticket-triage", "type": "queue", "concurrency": 2,
      "queue": { "type": "redis", "key": "ach:triage", "ackMode": "onComplete" },
      "prompt": "Triage this ticket and act via your tools." },

    { "name": "local-cli", "type": "tui" },     // free-form, no terminal contract (§8)

    { "name": "peer-intake", "type": "a2a", "concurrency": 2,
      "a2a": { "mode": "async",                 // v1 = async-only; callback target is CALLER-supplied
               "auth": { "header": "x-a2a-custom-api-key",
                         "secretPath": "/etc/ach-agent/secrets/a2a/key" } } }
  ]
}
```

**Removed vs v2 (do not render):** the entire **`engine` block**, `channels[].responseActions`,
`channels[].response`, `channels[].webhook.deliver`/`deliverOnly`, any `inputSchema`/`consentTier`,
`model.provider`. **▲ Also removed: `agentEnv`** — opencode runs in-process behind the localhost
proxy; there is no subprocess credential env to inject (the proxy holds the `ek_`).

**Secrets:** operator mounts referenced Secrets as files; the config carries **paths**, never
values. The GitLab token lives in the `gitlab-mcp` server's config (egress is the MCP's job, §9),
not in ach-agent.

---

## 3. The ACH_* env contract + hydration — ▲CHANGED (was Codex config.toml)

When `governed: true`, the operator materializes these into the main container env:

| env var            | value                                                  | notes |
|--------------------|--------------------------------------------------------|-------|
| `ACH_BASE_URL`     | `CapabilityProfile.ach.endpoint`                       | fronts ALL egress — model, MCP, outbound A2A |
| `ACH_TOKEN`        | the `ek_` (from `Agent.capability.identity.secretRef`) | **bearer**; held by the harness proxy; **never** logged, **never** reaches opencode |
| `ACH_ENVIRONMENT`  | `CapabilityProfile.ach.name`                           | which ACH Hub Environment; used at boot self-hydration |

`ek_` is **only** ever in `ACH_TOKEN`, held by the harness. Rotation by secret-hash restart.

**Hydration (no init container).** At boot the harness self-hydrates from the single config:

1. **Hydrate** the Environment from ACH (manifest: `runtime.models[]`, `runtime.mcpServers[]`,
   `runtime.a2aAgents[]`, `context.{skills,prompts,artifacts}[]`). **The harness calls**
   `POST {ACH_BASE_URL}/platform/hydrate` with `x-ach-key: ek_` (no CLI in the agent).
2. **Resolve the model**: `model.name` must be in `runtime.models[]` → else hard-fail.
3. **▲ Start the localhost proxy** (§9): a local reverse-proxy exposing
   `http://localhost:<p>/v1` (and `/gemini`, `/anthropic`) for the model, and local MCP routes for
   the provisioned servers. The proxy injects `Authorization: Bearer ek_` toward `ACH_BASE_URL`.
4. **▲ Fetch context**: download each `skills/prompts/artifacts` `tar.gz` and decompress into its
   directory under `workDir`/`mountPath`.
5. **Write `opencode.json`** pointing the model `baseURL` and the `mcp` servers at **localhost**
   (no `ek_`, no real ACH URLs), apply `capability.filter.exclude`, then start `opencode serve`.

A hydration failure is an ordinary startup failure (exit within `startupTimeoutSeconds` → pod
not-ready), not a dedicated condition.

---

## 4. HTTP endpoints the harness MUST expose — UNCHANGED

```
POST /channels/{channelName}/events    # inbound for HTTP-delivered channels (webhook, a2a)
GET  /healthz                          # liveness
GET  /readyz                           # readiness = all enabled channel adapters listening
GET  /metrics                          # Prometheus
```

`readyz`: Ready when adapters listen. Engine warmup is NOT a readiness gate, but if the engine
does not reach ready within `startupTimeoutSeconds` the process exits.

---

## 5. Status conditions — operator-only — UNCHANGED

The harness populates **none**. Post-start / per-invocation failures are **telemetry**, never status.

---

## 6. Behavioral invariants the harness MUST honor — ▲ (router unchanged; 8/9 reworded, 10 new)

1. **Idempotency-key derivation:** per channel type — webhook header chain → ms-timestamp
   fallback; queue message id; a2a task id; cron `{channel}:{scheduled_tick_time}`. NEVER a
   shared/empty key.
2. **Pre-lane order: dedup → backpressure (maxQueuedTotal) → lane.**
3. **Three finite bounds always enforced:** maxConcurrentInvocations, maxInvocationSeconds, maxQueuedTotal.
4. **`expire` exhaustion / full queue is never silent:** 503 sync / NACK-redelivery / drop-log.
5. **Memory is fail-open:** backend down → run without memory context, log it, never fail.
6. **Startup deadline:** engine/hydration not ready within startupTimeoutSeconds → exit.
7. **Proven-start gate A′:** during first warmup, NACK/503 instead of buffering.
8. **▲ Self-hydration at boot:** resolve Environment + provision MCP set + start localhost proxy +
   write opencode.json before opencode serves; failure = startup exit, never a silent half-start.
9. **▲ Egress is the agent's via MCP, not the channel's:** the channel never posts on the model's
   behalf; it only ingests events and (for call channels) returns the terminal result.
10. **▲NEW — Secret hygiene:** the `ek_` is held only by the harness/proxy. It MUST NOT appear in
    `opencode.json`, opencode's env, logs, or any model/MCP request opencode can observe.

---

## 7. What is NOT in v1 (declared, do not build) — ▲ (added plugins)

- Channels: `slack`, `telegram`, `openai-compatible`, sync `a2a`, board, hooks.
- **Hermes dependency dropped** from `pyproject.toml` (was only for slack/telegram).
- **▲NEW — Plugins.** ACH context in v1 is **skills / prompts / artifacts only**. Plugin bundles
  (skills + subagents + mcps + hooks) are **not supported** — no plugin explosion.
- O2/O4/O7/O8, `/v1/responses` facade, per-channel `rateLimit`.
- **Consent / tool-limiting (O9, v1.1):** the `consent` terminal action (§8) is reserved but
  non-executable in v1. Real enforcement is tool provisioning (`capability.filter`, §9).
- **`queue`: redis only in v1.**

Keep the channel→router boundary a **named in-process seam**.

---

## 8. Structured output — the terminal contract — ▲CHANGED (harness-enforced, not Codex-native)

**v1 = Option A (text-based extraction).** The prompt asks the model to end with a terminal JSON
object; opencode streams its answer as **text** over SSE (the model may also emit free prose — see
the `text` field). **The harness is the enforcer:** it extracts the JSON from the accumulated text,
validates it against the channel-class Pydantic model, and on a miss does at most one backstop retry
(`terminalOutputRetries`), then follows the table. This is the **existing** `validator.py` path
(`extract_actions` → `validate_actions` → `repair_turn`). opencode's native `format: json_schema`
(StructuredOutput tool) is a **future optimization**, not used in v1, and would still require the
same harness validate-+-retry backstop.

**Tools vs terminal output are different.** During the run the agent calls **MCP tools** (egress,
§9). The terminal JSON is only the **end-of-turn signal**, not how the agent "talks".

| Channel class | Channels | Terminal contract | If invalid after retries |
|---------------|----------|-------------------|--------------------------|
| **async (no result expected)** | webhook, cron, queue | `{"action":"none","text":"…","thoughts":"…"}` | log + **ignore** (work already done via tools) |
| **call — async result** | a2a (async-only) | `{"action":"a2a_reply","text":"…"}` | **callback FAILED** to the caller (`TaskStatusUpdateEvent(state=failed)`) |
| **call — free** | tui | **none** — stream text to the terminal | n/a |

Terminal action models (Pydantic discriminated union — **single object, NOT a list**). Every action
carries a free-text `text` field so the model always has a place to "finish" with natural language
(some models must emit a closing statement even when there is no structured result):

```python
class NoneAction(BaseModel):      action: Literal["none"];      text: str = ""; thoughts: str = ""
class A2AReply(BaseModel):        action: Literal["a2a_reply"]; text: str;      thoughts: str = ""
class ConsentRequest(BaseModel):  action: Literal["consent"]    # RESERVED, v1.1
    tool: str; args: dict = {}; reason: str = ""
# TerminalAction = NoneAction | A2AReply | ConsentRequest | …
```

> ▲ **Code alignment:** the current `validator.py` extracts `{"actions": [...]}` (a v2 list). It
> must be changed to the single-object terminal above (fields: `action` + `text` + optional `thoughts`).

---

## 9. Tools / egress — external MCP via the localhost proxy — ▲CHANGED

The agent acts by calling **external MCP tool servers** (e.g. `gitlab-mcp`). opencode is a
config-driven MCP client — **but it points only at the harness's localhost proxy.** The proxy
fronts the ACH-fronted MCP servers, injecting the `ek_`. opencode never sees the `ek_` or the real
ACH URLs.

**Where the tool set comes from (governed `type: ach`, the only v1 path):**
- Hydration returns `runtime.mcpServers[{id, endpoint}]`. The harness creates a **localhost route**
  per server and writes it into `opencode.json` (`mcp.<id> = {type: remote, url: http://localhost/…}`).
- The ACH Forwarder fronts all egress (model, MCP, outbound a2a) — **no per-MCP credentials, no
  real ACH URLs in `opencode.json`.**

**▲ a2a egress = harness-hosted MCP tools.** Peer agents (`runtime.a2aAgents`) are surfaced to the
model as MCP tools `a2a_{name}` / `a2a_{name}_async` / `a2a_{name}_status` (ported from ackbot
`handlers/a2a/{tools,client,notification_store}.py`, a2a-sdk client). This is the **only**
harness-hosted MCP; everything else is a proxied remote server. (Distinct from the inbound `a2a`
**channel**, which receives calls — `channels/a2a.py`.)

**The tool-limiting / consent gate is provisioning, not validation.**
`capability.filter.exclude.{tools,mcpServers,skills}` **withholds** capabilities **before** they
are offered to opencode — the model literally cannot call/see them. The reserved `consent`
terminal action is the agent **requesting** an unlock (v1.1).

**Failure mode (honest):** a down MCP server means the agent cannot perform that action. Unlike
memory (fail-open), tool egress is not fail-open — surface it as a per-invocation telemetry failure.

---

## Resolved / re-scoped (2026-06-25) — ▲CHANGED

1. **queue** — redis only in v1; idempotency key = redis message id.
2. **MCP servers** — provisioned from hydration, fronted by the localhost proxy; no explicit
   server list and no real URLs in the config.
3. **a2a async callback** — caller-supplied (rides in the inbound request). Our config only
   validates the inbound caller (`a2a.auth`). a2a **egress** peers come from `runtime.a2aAgents`.
4. **cron timezone** — IANA `timezone` field.
5. **Forwarder** — fronts MCP, A2A-client, and models uniformly, via the harness localhost proxy.
6. **▲ Engine = opencode (`opencode serve` + SSE), hardcoded.** The bridge already exists in
   `src/ach_agent/engine/` and is reused. `engine` block removed; `model{name,type,params}` stays.
   Structured output is harness-validated (`format: json_schema` + Pydantic + ≤1 retry). Router IP
   + tests kept.
7. **▲ Secret hygiene** — the harness fronts model + MCP on localhost; the `ek_` never reaches
   opencode (§3/§6.10/§9).
8. **▲ Context** — skills / prompts / artifacts only (tar→dir at hydration); no plugins.

**All decisions resolved (2026-06-25):** `schemaVersion: "1"` (D-03); hydration = harness calls
`POST /platform/hydrate` (no CLI); structured output = Option A (text extract + `repair_turn`,
harness-validated); terminal = single object with `action` + `text` + optional `thoughts`; a2a
egress through the proxy (no `ek` exposure). See `00-RESCOPE-SUMMARY.md`.
