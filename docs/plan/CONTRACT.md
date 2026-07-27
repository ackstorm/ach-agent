# ACH Agent Runtime — Shared Contract (the seam)

This document is the **single frozen interface** between `ach-runtime` (operator, Go)
and `ach-agent` (harness, Python). Both repos depend on this; neither may change it
unilaterally. Source of truth lives in `ach-runtime`; `ach-agent` pins a version.

Spec reference: `ach-agent-runtime-spec-v1_4_2.md` (API group `runtime.ackstorm.ai/v1alpha1`).

---

## 1. Direction of dependency (non-negotiable)

```
ach-runtime  ──renders──▶  rendered runtime config  ──read──▶  ach-agent
                          + ACH_* env (governed)
ach-agent    ── NEVER reads CRDs, NEVER talks to the API server, NEVER writes Agent.status
```

The harness has **no Kubernetes RBAC**. Status is the operator's job, derived from
`pod.status` only (spec §36, §39). This is what makes the two repos independently testable:
the harness is tested against a hand-written rendered config; the operator is tested by
"does it render the correct config?".

---

## 2. The rendered runtime config (operator writes → harness reads)

The operator collapses the four CRDs into **one flat config** mounted into the pod
(file at `/etc/ach-agent/config.json`, or equivalent). The harness reads only this.
This schema is the contract; freeze it before parallel work starts.

> NOTE: this shape is **derived by us**, not copied from the spec — the spec defines the
> CRDs and the env contract; this is the projection the harness consumes. Keep it minimal:
> only what the harness needs to run. Resolved, not referenced (no CRD refs leak through).

```jsonc
{
  "schemaVersion": "1",
  "agent": {
    "name": "gitlab-ackstorm",
    "namespace": "engineering",
    "generation": 4                       // for trace correlation
  },
  "engine": {
    "type": "opencode",
    "binaryPath": "opencode",
    "workDir": "/workspace",
    "sessionDir": "/var/lib/ach-agent/opencode/sessions",
    "thinkingLevel": "medium",
    "steps": 50,
    "startupTimeoutSeconds": 30,          // §8.5 deadline: exceed → process exits
    "shared": { "enabled": true, "ttlSeconds": 120 }
  },
  "model": {
    "default": "gemini-3-flash",
    "provider": "gemini"                  // → ACH_PROVIDER_TYPE / PROVIDER_TYPE
  },
  "governed": true,                       // derived from presence of ach (§4); informs harness routing
  "prompt": {
    "base": "…resolved agent persona text…",   // already composed from source+compose
    "compose": "append"
  },
  "memory": {                             // null if not configured; fail-open (§31)
    "endpoint": "http://hindsight.engineering.svc:8080",
    "mission": "AI code reviewer…",
    "scope": "{project_id}",
    "mentalModels": ["architecture", "conventions", "recurring-issues"]
  },
  "limits": {                             // §18.6 — all finite, always enforced
    "maxConcurrentInvocations": 8,
    "maxInvocationSeconds": 1800,
    "maxQueuedTotal": 100,
    "idempotencyWindowSeconds": 3600
  },
  "persistence": { "enabled": true, "mountPath": "/var/lib/ach-agent" },  // → durable lane/dedup
  "health": { "host": "0.0.0.0", "port": 8000 },
  "channels": [
    {
      "name": "gitlab-mr-review",
      "type": "webhook",                  // webhook | slack | telegram | a2a | cron
      "concurrency": 4,
      "expire": 120,
      "session": { "mode": "auto", "continuity": "durable", "ttlSeconds": 604800 },
      "response": { "mode": "actionRequired", "fallback": "fail" },
      "prompt": "Review this merge request: {{ .object_attributes.url }}",  // layer 5b (§12.3)
      "webhook": {
        "auth": { "type": "hmac", "secretPath": "/etc/ach-agent/secrets/gitlab-webhook/secret" },
        "deliver": { "type": "gitlab_comment",
                     "config": { "tokenPath": "/etc/ach-agent/secrets/gitlab-token/token" } },
        "deliverOnly": false
      },
      "responseActions": [
        { "name": "channel_message", "kind": "reply",
          "inputSchema": { "type": "object", "required": ["text"],
                           "properties": { "text": { "type": "string" } } } },
        { "name": "create_issue", "kind": "sideEffect",
          "consentTier": "consent",    // NEW (optional, default "consent")
                                       // "auto" | "consent"
                                       // Absent → treated as "consent" (safe default)
          "inputSchema": { "type": "object", "required": ["title"],
                           "properties": {
                             "title": { "type": "string" },
                             "body": { "type": "string" }
                           } } }
      ]
    }
  ]
}
```

**Secrets:** the operator mounts referenced Secrets as files; the rendered config carries
**paths**, never secret values. The harness reads the file at use time.

**Open contract question to settle before freeze:** config delivery mechanism —
mounted file (recommended: survives restart, no size limit, simple) vs env var
(size-limited) vs the operator hitting a harness admin endpoint (adds RBAC surface, avoid).
Recommendation: **mounted file**, hash in a pod annotation so a config change rolls the pod.

---

### ⚠ Deviation D-12: GITLAB_TOKEN via environment variable (Phase 2 — gitlab_comment channel)

> **Scope:** `gitlab_comment` delivery adapter only. All other secrets keep the mounted-path model.

**Decision (user-approved, 2026-06-20):** The GitLab API token used by `GitlabCommentAdapter`
to post MR notes is provided via a `GITLAB_TOKEN` environment variable, **not** via a mounted
file path (`deliver.config.tokenPath`). This is an intentional local exception to the
"secrets are mounted paths, never values" rule described above.

**What this means:**
- (a) **Deliberate exception, gitlab_comment only:** `GITLAB_TOKEN` is read from `os.environ`
  at each `deliver()` call — never stored as an instance attribute and never logged
  (redact_gitlab_token_processor scrubs any occurrence from structlog output). This is a local,
  ackbot-style exception; other secrets (ek_, webhook HMAC secret) keep their mounted-path model.
- (b) **HMAC webhook secret unchanged (D-03):** The `auth.secretPath` mounted file for HMAC
  verification of inbound webhooks is NOT affected. That secret still follows the standard
  "read from mounted file per request" pattern (SEC-02).
- (c) **Token read at call time, never logged:** `GITLAB_TOKEN` is read inside `deliver()`
  on every call and discarded immediately; it never crosses a log boundary (SEC-03 e2e test
  asserts this with a sentinel value).
- (d) **Contract-conformant fallback:** If this deviation is reversed in a future revision,
  the fallback is `deliver.config.tokenPath` — read per-use from the mounted file path carried
  in the rendered config's `webhook.deliver.config.tokenPath` field (already modeled in the
  `WebhookDeliverBlock` schema). No schema change is needed to adopt this fallback.
- (e) **ach-runtime coordination required:** `ach-runtime` (the Go operator) must render the
  `GITLAB_TOKEN` env var into the pod environment (from a `SecretKeyRef` or equivalent)
  in addition to the standard `ACH_*` env. This is a deliberate deviation from the
  "operator-rendered env is `ACH_*` only" invariant in §3 above.

**Why `GITLAB_TOKEN` env var and not `tokenPath`:**
Operational simplicity in the first deployment (ackbot-style); avoids requiring an additional
secret mount. Reversed if the operator team prefers strict path-only secrets.

---

### ⚠ Phase 5 Additive Revision: consentTier on responseActions entries

> **Scope:** responseActions[] entries of kind: sideEffect only. reply entries do not
> carry consentTier (consentTier gates side effects, not replies — spec §23).

**Decision (user-approved, 2026-06-21):** An OPTIONAL `consentTier` field ("auto" | "consent",
default "consent") is added to each responseActions[] entry. It is additive and backward-compatible:
existing configs that omit it resolve to "consent" (the safe default). The harness treats absent
the same as "consent".

**What this means:**
- (a) Additive, backward-compatible: existing Phase 1–4 configs still validate.
- (b) ach-runtime coordination: the operator MAY render consentTier into the responseActions
  array from the Agent CRD (Agent.spec.channels[].responseActions[].consentTier). Until the
  operator renders it, the harness default ("consent") governs — no breaking change.
- (c) The field is the only Phase 5 CONTRACT change. Consent semantics (how consent is actually
  decided) are deferred to a dedicated V1.1 CR and are NOT part of this revision.
- (d) schema.py: ResponseActionBlock gains consent_tier: Literal["auto","consent"] = "consent"
  (alias "consentTier"). extra="forbid" still holds.

---

## 3. The ACH_* env contract (governed only) — spec §13.1, FROZEN

When `governed: true`, the operator materializes these into the pod env
(both the init/hydrator container and the main container), **after** any `envFrom`,
so the contract always wins on collision (§11.4):

| env var            | value                                            | notes |
|--------------------|--------------------------------------------------|-------|
| `ACH_BASE_URL`     | `CapabilityProfile.ach.endpoint`                 | the Forwarder/Hub coordinate |
| `ACH_API_KEY`      | the `ek_` (from `ach.secretRef`)                 | presented as **bearer**; never logged |
| `ACH_PROVIDER_TYPE`| `Agent.model.provider` (default `openai`)        | dialect the harness builds |
| `ACH_ENVIRONMENT`  | `CapabilityProfile.ach.name`                     | which ACH Hub Environment (used by hydrator) |

Security invariants (normative, §13.1): `ek_` as bearer; rotation by secret-hash restart
(§11.6); no leak to logs or downstream tool backends. The harness owns *translation*
(building the engine client, routing model vs MCP vs A2A) — the spec does not specify "how".

Non-governed agents: no `ACH_*` set; access comes from `CapabilityProfile.envFrom`
(rendered as ordinary pod env), and `model.provider` still emits `PROVIDER_TYPE`.

---

## 4. HTTP endpoints the harness MUST expose — spec §16.1, FROZEN

```
POST /channels/{channelName}/events    # inbound for HTTP-delivered channels (webhook, a2a, sync http)
GET  /healthz                          # liveness
GET  /readyz                           # readiness = all enabled channel adapters listening (§8.5)
GET  /metrics                          # Prometheus
```

`readyz` semantics (§8.5): Ready when adapters listen. Engine warmup is NOT a readiness gate,
**but** if the engine does not reach ready within `startupTimeoutSeconds` the process exits
(→ substrate marks pod not-ready). The operator derives `WorkloadReady` from the pod passing
`readyz`; it never asks the harness for status.

---

## 5. Status conditions — operator-only, derived from pod.status (§36) — FROZEN

The harness populates **none** of these. Listed here so the harness team knows what is and
isn't its job (it is NOT).

`RuntimeResolved`, `CapabilityResolved`, `MemoryResolved`, `Hydrated`
(from `pod.status.initContainerStatuses`), `WorkloadRendered`, `WorkloadReady`
(pod passed `readyz`), `ServiceReady`, `Ready`, `SessionContinuityWarning`.

Post-start / per-invocation failures are **telemetry** (metrics/audit/trace), never status
(§O12 closed: "status is the pod's, not the agent's").

---

## 6. Behavioral invariants the harness MUST honor (these become conformance tests)

1. **Idempotency-key derivation (§18.4.0):** per channel type — webhook/http header chain
   (`X-GitHub-Delivery` / `X-Gitlab-Event-UUID` / `svix-id` / `X-Request-ID`) → ms-timestamp
   fallback; slack `ts`; telegram `update_id`; cron `{channel}:{scheduled_tick_time}`.
   **Invariant: unique-per-distinct-event; degrade to unique-per-arrival (process),
   NEVER to a shared/empty key (drop).**
2. **Pre-lane order (§18.8/§29): dedup → backpressure (maxQueuedTotal) → lane.**
   Duplicates discarded before they consume a queue slot.
3. **Three finite bounds always enforced (§18.6):** maxConcurrentInvocations,
   maxInvocationSeconds (1800), maxQueuedTotal (100).
4. **`expire` exhaustion / full queue is never silent (§18.4.1):** 503 sync /
   NACK-redelivery async-retriable / drop-log async-no-retry.
5. **Memory is fail-open (§31):** backend down → run without memory context, log it,
   never fail the invocation (read and write).
6. **Startup deadline (§8.5):** engine not ready within startupTimeoutSeconds → exit process.
7. **Proven-start gate A′ (§8.5):** during the pod's first warmup, NACK/503 instead of
   buffering; accept-and-buffer only after the engine has been ready once.
8. **Model never talks to channels (§15):** adapters execute only accepted, validated actions.
9. **Dual delivery (§20.1):** synchronous reply + out-of-band delivery, both supported.

> **Conformance suite note (Phase 5, 2026-06-21):**
> The `tests/conformance/` suite tests all nine invariants above (§6.1–§6.9) **plus**
> two additional invariants from the implementation roadmap: FIFO per session key (SC#2)
> and secret-never-logged (SC#2). These two extras are not enumerated in §6 because they
> are routing and security invariants modeled elsewhere in this contract (§18.8 and §3
> respectively), not behavioral invariants in the harness action contract. The conformance
> suite covers the union (11 tests). No contract behavior changes with this note.

---

## 7. What is NOT in v1 (declared, do not build)

O2 managed runtimes, O4 shared Channel CRD, O7 external durable substrate (multi-replica,
NATS/queue, the channel↔harness network seam), O8 jobPerInvocation, `/v1/responses` facade
(O14), per-channel `rateLimit`. Keep the channel→router boundary a **named in-process seam**
so O7 is a later swap, not a rewrite — that is the only forward-prep that earns its keep.
