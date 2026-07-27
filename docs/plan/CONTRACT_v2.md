# ACH Agent Runtime — Shared Contract (the seam)

This document is the **single frozen interface** between `ach-runtime` (operator, Go)
and `ach-agent` (harness, Python). Both repos depend on this; neither may change it
unilaterally. Source of truth lives in `ach-runtime`; `ach-agent` pins a version.

Spec reference: `ach-agent-runtime-spec-v1_4_7.md` (API group `runtime.ackstorm.ai/v1alpha1`).

> **Aligned to spec v1.4.7.** Two changes ripple into this seam: (a) identity (the `ek_`) moved from the `CapabilityProfile` to the `Agent` (`capability.identity.secretRef`) — see §3; (b) the operator→init hydration seam is **gone** — there is no init container; the harness self-hydrates at boot from the single rendered config, so the former §3b is removed and its coordinates fold into §2's `capability` block.

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
(file at `/etc/ach-agent/config.json`). The harness reads only this — it is the **single**
seam now (the former separate hydration spec is folded in as the `capability` block below).
This schema is the contract; freeze it before parallel work starts.

> NOTE: this shape is **derived by us**, not copied from the spec — the spec defines the
> CRDs and the env contract; this is the projection the harness consumes. Keep it minimal:
> only what the harness needs to run. Resolved, not referenced (no CRD refs leak through).
> Its `schemaVersion` is an **independent integer** tracking *this config shape* — NOT the
> `runtime.ackstorm.ai/v1alpha1` API group. Bumped `1`→`2` in this revision because the shape
> changed (capability block added, `model.provider` dropped, hydration folded in).

```jsonc
{
  "schemaVersion": "2",
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
    "selected": "gemini-3-flash"          // Agent.model — scalar selection; NO provider (dialect discovered at hydration, §3)
  },
  "governed": true,                       // derived from capability.type == "ach"; informs harness routing
  "capability": {                         // §11.5 — folds in the former hydration seam; the harness self-hydrates from this
    "type": "ach",                        // CapabilityProfile.type
    "ach": {                              // coordinates only — NO secret (the ek_ rides ACH_TOKEN, §3).
                                          // Mirrors ACH_BASE_URL/ACH_ENVIRONMENT (§3); env is authoritative on conflict.
      "baseUrl": "https://ach.ackstorm.ai",
      "environment": "engineering-prod"
    },
    "filter": { "exclude": { "skills": ["draft-email"] } }   // Agent.capability.filter, verbatim; harness applies it AFTER resolving the Environment
  },
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
                     // D-12 (RECONSTRUCTED from the review note — this deviation was NOT in the
                     //        CONTRACT received; CONFIRM against the canonical deviation log before freeze):
                     //        gitlab_comment reads the token from the GITLAB_TOKEN env var, NOT config.tokenPath.
                     //        ach-runtime MUST render GITLAB_TOKEN into the pod env from the gitlab-token Secret.
                     "config": {} },
        "deliverOnly": false
      },
      "responseActions": [
        { "name": "channel_message", "kind": "reply",
          // NOTE: a `reply` action does NOT carry consentTier. Per spec §20.2/§22.1 and §1790
          // ("response.mode gates replies; consentTier gates side effects"), consentTier is
          // reserved for `kind: sideEffect` entries only (non-executable in v1; resolved by a
          // ConsentProfile gate in v1.1, O9). It is omitted here because this action is a reply.
          "inputSchema": { "type": "object", "required": ["text"],
                           "properties": { "text": { "type": "string" } } } }
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

## 3. The ACH_* env contract (governed only) — spec §13.1, FROZEN

When `governed: true`, the operator materializes these into the main container env
(there is no init container in v1.4.7 — the harness self-hydrates), **after** any `envFrom`,
so the contract always wins on collision (§11.4):

| env var            | value                                            | notes |
|--------------------|--------------------------------------------------|-------|
| `ACH_BASE_URL`     | `CapabilityProfile.ach.endpoint`                 | the Forwarder/Hub coordinate |
| `ACH_TOKEN`        | the `ek_` (from `Agent.capability.identity.secretRef`) | **bearer**; access + identity, bound by the Forwarder to its minted identity; never logged |
| `ACH_ENVIRONMENT`  | `CapabilityProfile.ach.name`                     | which ACH Hub Environment; used by the harness at boot self-hydration |

**Authoritative source (precedence).** `baseUrl`/`environment` appear in *both* §2's `capability.ach` and
here as env — by design (the operator renders both from the same profile coordinate, so they cannot
diverge). **The env is authoritative for the connection**; `capability.ach` mirrors them for inspection and
local-dev parity (a CLI reads it to set the same env). On any conflict, env wins (§11.4). The `ek_` is **only**
ever in `ACH_TOKEN`, never in the config.

**Freeze semantics.** FROZEN means frozen *within a spec version*; a spec bump may legitimately change it.
**Migration → v1.4.7:** `ACH_API_KEY` was renamed to **`ACH_TOKEN`** and its source moved from
`CapabilityProfile.ach.secretRef` to `Agent.capability.identity.secretRef`; **`ACH_PROVIDER_TYPE` was retired**
(dialect discovered at hydration for `ach`, declared by the `direct` profile). Operator team: rename the env key
and re-point the source; no other behavior change.

Security invariants (normative, §13.1): `ek_` as bearer (access + identity); **never present in the
rendered config of §2 — only in `ACH_TOKEN`**; rotation by secret-hash restart (§11.6); no leak to
logs or downstream tool backends. The CEO-hole is closed at issuance (ACH mints an `ek_` only for an
identity in the Environment's `authorizedTeams`; the Forwarder binds it), not in this seam. The harness
owns *translation* (building the engine client, routing model vs MCP vs A2A) and *dialect discovery* —
the spec does not specify "how".

Non-governed agents (`type: direct`): no `ACH_*` set; access comes from `CapabilityProfile.model`
(provider + credential/baseURL) and/or `envFrom`, rendered as ordinary pod env; the dialect is the
profile's own `model.provider`. **`ACH_PROVIDER_TYPE` / `PROVIDER_TYPE` is retired** (v1.4.7) — the
`Agent` never declares a dialect.

---

## 3b. Hydration seam — **REMOVED in v1.4.7**

The separate operator→init-container hydration spec (`/etc/ach/hydration-spec.json`) and the
`hydrators` Helm `type → image` mapping **no longer exist**. The operator injects **no init
container** and does not know *how* anything hydrates. Hydration coordinates (`type`, ACH
`baseUrl`/`environment`, `filter`) now live in §2's `capability` block, and **the harness
self-hydrates at boot** by reading the single rendered config — `ach env hydrate`, writing
`.opencode/`, configuring native platforms, or nothing, inside its own image (spec §11.5, §8.5).
A failed self-hydration is an ordinary startup failure (exit within `startupTimeoutSeconds` →
pod not-ready → `WorkloadReady: False`), not a separate `Hydrated` condition.

A new source family (e.g. OpenPackage) is now purely a harness/contract concern — a new
`capability.type` the harness knows how to hydrate. **No operator change, no new CRD, no Helm
mapping.**

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

`RuntimeResolved`, `CapabilityResolved`, `MemoryResolved`, `WorkloadRendered`,
`WorkloadReady` (pod passed `readyz`), `ServiceReady`, `Ready`, `SessionContinuityWarning`.

**`Hydrated` was removed in v1.4.7** (no init container; the harness self-hydrates, so a
hydration failure surfaces as `WorkloadReady: False`, not a dedicated condition).

Post-start / per-invocation failures are **telemetry** (metrics/audit/trace), never status
("status is the pod's, not the agent's"). Former O12 (hydration visibility) is dissolved into
startup-failure visibility.

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
10. **Self-hydration at boot (§8.5, spec §11.5):** the harness reads the rendered config and
    hydrates its own workspace (Environment via `ach env hydrate`, source clone, native platform
    config) **before** the engine serves. A hydration failure must surface as a startup failure
    (exit within `startupTimeoutSeconds`), never a silent half-start. The operator injects no init
    container; the harness owns the whole hydration step.

---

## 7. What is NOT in v1 (declared, do not build)

O2 managed runtimes, O4 shared Channel CRD, O7 external durable substrate (multi-replica,
NATS/queue, the channel↔harness network seam, **the durable pending-consent store**), O8
jobPerInvocation, `/v1/responses` facade (O14), per-channel `rateLimit`. Keep the
channel→router boundary a **named in-process seam** so O7 is a later swap, not a rewrite —
that is the only forward-prep that earns its keep.

**Consent (O9, v1.1):** `consentTier` is reserved in §2 above but **non-executable in v1** —
sideEffects are rejected at runtime (spec §22.1). When enabled, consent is resolved by a
harness-side gate configured by a referenced `ConsentProfile` CRD (fail-closed / default deny;
per-resolver timeout; fast=synchronous, slow=asynchronous via the durable store). The harness
must NOT implement consent as a tool the model calls — it is a gate above the model.
