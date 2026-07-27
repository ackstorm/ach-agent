# ACH Agent Runtime — Technical Specification

**Version:** v1.4.3 (design draft)  
**API group:** `runtime.ackstorm.ai/v1alpha1`  
**Companion specs:** ACH Hub, ACH CLI, LiteLLM Operator  
**Status:** Active design. Bake-ready draft. Not frozen.  
**Reference workload:** `ackbot-process` / opencode-style resident agent runtime

---

## Changelog

- **v1.4.3** — Two operational details surfaced by the multiple-channels model (neither architectural):
  - **platform webhook fan-out stated (§16).** Because each channel listens on its own `/channels/{channelName}/events` and "one channel per event type" is the model (no `routes[]`), a platform that POSTs all event types to one URL (GitLab, GitHub) must be configured with **N separate webhook entries**, one per channel URL, each filtered to its event type. Checking all event boxes on a single webhook pointed at one channel URL silently drops the unmatched types. Stated so implementers configure the platform correctly;
  - **`deliverOnly: true` requires `channel.prompt` (§34).** Under `deliverOnly`, the rendered prompt *is* the delivery; without a `channel.prompt` it would deliver an empty payload. Now an admission rule;
  - **changelog note:** the v1.3.1 entry's `promptTemplate` and `rateLimit` are annotated as renamed/removed in v1.4.1 for clarity.
- **v1.4.2** — Idempotency and pre-lane ordering hardening (the "day-one implementer" pass):
  - **idempotency-key derivation specified per channel type**, modeled on Hermes `webhook.py` (`delivery_id` chain + TTL cache, whose `3600s` matches this spec's default). The governing invariant — the lesson of the broad-key dedup bug — is normative: **the key must be unique per distinct event; when one cannot be derived, it degrades to unique-per-arrival (process), never to a shared/empty key (drop).** Webhook/http: header chain (`X-GitHub-Delivery` / `X-Gitlab-Event-UUID` / `svix-id` / `X-Request-ID`/`Idempotency-Key`) → millisecond-timestamp fallback. Slack: `ts`; Telegram: `update_id`; cron: `{channel}:{scheduled_tick_time}` (the scheduled instant, not `now()`). No new CRD field — the caller's `Idempotency-Key`/`X-Request-ID` covers synchronous http (§18.4);
  - **pre-lane ordering pinned: dedup → backpressure → lane.** A duplicate is discarded *before* it counts against `maxQueuedTotal`, so a flood of redeliveries cannot fill the queue and NACK legitimate traffic into a redelivery loop (§18.8, §29);
  - **queue-starvation declared as an accepted trade**, symmetric with slot-starvation (§18.2): `maxQueuedTotal` is a global pod cap, so one hot session's backlog can refuse other channels' intake. Named honestly; per-channel/per-lane depth cap is the future escape hatch (parallel to `minReserved`) (§18.8);
  - **§30.1 gains the shared-`CapabilityProfile` rotation blast radius** — rotating the `ek_` changes the secret hash and rolls every referencing Agent (more frequent than the AgentRuntime case, since a profile is more widely shared).
- **v1.4.1** — Consistency migration + the last bake decisions. Fixes pre-refactor leftovers and closes the two remaining open threads:
  - **flagship examples migrated to the channel model.** §10.1 (`type: gitlab` + `gitlab:` block) and §10.2 (`type: http`) used channel types that do not exist post-Hermes-refactor and would fail admission. Both rewritten to `type: webhook`. §28 envelope `"type":"gitlab"` → `"webhook"`. No `gitlab`/`http` aliases — one form only;
  - **§25 leftover removed:** `Agent.spec.hydration` is no longer a workload input (it moved to `CapabilityProfile` in v1.3.0);
  - **webhook routing model resolved: multiple channels, not `routes[]`.** Each event type (MR notes, pushes, issues) is its own `webhook` channel with its own `prompt`/`session`/`response`. Mirrors how cron multi-task is multiple cron channels;
  - **one `channel.prompt` (templated) unifies per-event prompts.** Closes **O13**: cron per-task prompt and webhook `promptTemplate` are the same field — `channel.prompt`, optional, payload-templated, fed into a new prompt-stack layer (5b). Append-only, ephemeral, per Hermes `channel_prompt`. No code change to Hermes channels (the field is native);
  - **accept-and-buffer proven-start gate (A′):** during a pod's *first* warmup, inbound is NACK'd/503'd (source retries) instead of buffered; accept-and-buffer engages only after the engine has reached ready once. Closes the §30.1 loss mode the source could have recovered from (terminal startup failure after a 200);
  - **defaults trap addressed (C):** `expire` default raised `30 → 120`; concurrency defaults stay `1` (safe floor for small pods); published runtime profiles ship workload-appropriate caps; long-running channels documented as MUST-raise;
  - **trimmed for v1:** `/v1/responses` facade moved to Open Items (no slice, `automatic`-only); per-channel `rateLimit` removed (OOM is bounded by `maxQueuedTotal`, compute by `concurrency`; not needed on an internal Service in v1);
  - **O12 in-run closed by design:** post-start failures (model stops answering, Forwarder drops mid-run) are per-invocation telemetry (metrics/audit/trace), never `Agent.status`. Status is the pod's, not the agent's. No runtime self-report channel in v1.
- **v1.4.0** — Bake-readiness hardening (the "into the oven" pass). Closes correctness/availability holes found in adversarial review:
  - **status hole closed.** A harness/engine that fails to start now actually surfaces: the engine has `startupTimeoutSeconds` to reach ready; exceeding it terminates the runtime process, so the substrate (k8s/CloudRun/…) marks the pod not-ready and restarts it. O12/§36's "harness failure → `WorkloadReady: False`" is now true via the substrate, with no runtime self-report. A substrate-agnostic `health` block is added to `AgentRuntime`;
  - **watchdog default set.** `maxInvocationSeconds` defaults to **1800** and is always enforced (was "no default + warning" — the one unset bake-default, and the deadlock-prevention one). §18.6 loses the "unset" branch;
  - **queue-depth backpressure added.** New `maxQueuedTotal` (default **100**) caps buffered events per pod; a full queue admits no new event and treats it as immediate `expire` (non-silent, §18.4.1). Compatible with `expire: 0` (acts at admission, not on the wait). Third resource bound alongside concurrency and run-time;
  - **dedup clarified.** Idempotency is **router-level and independent of `session.mode`** — one key-set per agent (key `{channel}:{event.id}`), durable ⟵ `persistence.enabled`, covering `mode: none` channels (e.g. the canonical GitLab one-shot redelivery). `mode: none` disables session continuity, not dedup. §18.4 rewritten, §29 aligned;
  - **memory-down behavior specified.** A reachable-failure of the memory backend is **fail-open**: the invocation proceeds without memory context (read and write), and logs it. Memory is enrichment, not a gate; no fail-closed in v1;
  - **diagram fixed.** Added the non-governed model-access edge (`Main → Providers` directly via `envFrom` creds, bypassing the Forwarder); `envFrom` no longer mislabeled as init hydration (only `sources` feeds the init container);
  - **RBAC fix:** `CapabilityProfile` added to the operator watch and the cross-namespace-rejection list (§39 copy-paste miss);
  - **envFrom collision is now visible:** a non-blocking reconcile **warning** when an `envFrom` key collides with a reserved `ACH_*` var (security behavior unchanged — ACH still wins; §11.4/§35).
- **v1.3.1** — Channel layer detailed and contrasted against NousResearch Hermes (`gateway/platforms/`, MIT):
  - **adapter contract** (`connect`/`disconnect`/`send`/`edit_message`/`send_typing`/`handle_message`) and the in-process seam (`set_message_handler`) documented; confirms §15 is stock-Hermes shape;
  - **`MessageEvent`** formalized as the canonical inbound envelope, with raw payloads kept out of the cross-boundary contract and media as references (so the seam can become a network/queue transport later, O7, without changing channels);
  - **five v1 channel types:** `webhook`, `slack`, `telegram`, `a2a`, `cron`. The `type` encapsulates transport + normalization + write-back + sync/async trait. `gitlab`/`github`/etc. are **routes** of the generic `webhook` type, not separate types (matches Hermes `webhook.py`); the synchronous `http`/manual trigger is `webhook` used synchronously;
  - **`webhook` routes:** `auth` (hmac/bearer/none), `promptTemplate` (payload→prompt; *renamed to `channel.prompt` in v1.4.1*), `deliver` (platform write-back lives here), `deliverOnly` (skip the agent), `rateLimit` (default 30/min; *removed in v1.4.1*);
  - **`slack`** = Socket Mode (outbound ws, no Service needed), **`telegram`** = long-polling/webhook; both interactive (`edit_message` streaming, `expire: 0`);
  - **`a2a`** = LiteLLM A2A inbound, receiver-only in v1, header auth `x-a2a-custom-api-key`; registration in LiteLLM is out-of-band (no new operator role);
  - **dual delivery** (synchronous reply + out-of-band) formalized in the action contract (§20.1) — the out-of-band path is the future split-pod delivery route;
  - **`enabled`** is optional, default `true`, with defined "validated but not materialized" semantics for the GitOps toggle;
  - channel secrets remain **inline on the `Agent`** (1:1 with the agent; promote to CRD via O4 if shared).
- **v1.3.0** — Fourth CRD and scope separation. Extracts the access + hydration contract out of `Agent` into a new reusable, reference-only object, so three teams own three objects:
  - new **`CapabilityProfile`** CRD (owned by AI engineers): the package of capabilities an agent consumes — composable optional blocks `ach?` (governed access + governed hydration of an ACH Environment), `sources?` (extra/own context), `envFrom?` (raw env). No `mode` discriminator: "governed" is **derived from the presence of `ach`**. At least one of `ach`/`envFrom` is required. `ach` is an **external coordinate** (`endpoint` + `name` + `secretRef`), not a reference to an in-cluster CR, because the Hub may live outside the cluster;
  - `Agent` loses `access` and `hydration` (they move to `CapabilityProfile`); gains `capabilityProfileRef`. `Agent` keeps **choice**: `model.default` + `model.provider` (emits `PROVIDER_TYPE`, applies whether or not `ach` is present);
  - `AgentRuntime` loses the `directExotic` escape hatch entirely (deleted, not moved) — it now holds nothing about access/credentials;
  - **RBAC-boundary principle:** objects that cross an RBAC boundary (`CapabilityProfile`, `MemoryProfile`) are **referenced, never inlined**, so the consumer who writes an `Agent` cannot author access credentials or memory backends;
  - **scope ownership** documented: `AgentRuntime` → SRE/platform; `CapabilityProfile` + `MemoryProfile` → AI engineers; `Agent` → consumer/business;
  - **envFrom precedence:** when `ach` is present, the operator materializes the reserved ACH contract env (`ACH_BASE_URL`, `ACH_API_KEY`, `ACH_PROVIDER_TYPE`) **after** `envFrom`, so the contract always wins; colliding `envFrom` keys are overwritten (not an error) — keeps governance intact without admission-time collision checks;
  - removes the `access.mode: ach | direct` axis and `directExotic` as concepts.
- **v1.2.6** — Bake defaults and two fixes:
  - **concurrency defaults defined** (were "if set, ≥1"): `maxConcurrentInvocations` default `1`, `channel.concurrency` default `1`, `channel.expire` default `30`. `expire: 0` (never) is reserved for interactive channels. Conservative for first bake; `expire: 30` does not lose events because exhaustion is non-silent (§18.4.1: NACK-for-redelivery on retrying sources);
  - **idempotency window is a number:** `execution.limits.idempotencyWindowSeconds` default `3600` (covers typical webhook redelivery backoff). Without persistence the effective window is bounded by pod uptime (§18.4, §30.1);
  - **fix:** the `Agent.status` example no longer shows the removed `hydration.source` field; it shows the resolved `access.mode` and `hydration` timestamps only.
- **v1.2.5** — Access-contract concretization, status honesty, and robustness pass:
  - **access contract concretized.** `access.ach.provider` (optional, default `openai`) and `access.direct.provider` (required) name the dialect the harness builds; the harness materializes the **env-var contract** to the engine (`ACH_BASE_URL`, `ACH_API_KEY`, `ACH_PROVIDER_TYPE`). The spec defines the env vars and the security invariants (`ek_` presented as bearer, rotation by secret-hash, no downstream leak); the harness owns translation and routing — we do not specify the "how". **O1 is now genuinely closed**, not conceptually deferred;
  - **status feedback simplified.** Only control-plane-observable conditions are populated; the runtime never self-reports to `Agent.status` (no new RBAC, no sidecar). `PromptResolved`, `ProviderDialectResolved` and `ChannelsListening` are removed. Content/harness failures live in pod logs and `WorkloadReady: False`, not in fabricated conditions. O12 rewritten honestly: no runtime→status channel in v1;
  - **invocation watchdog added (availability fix).** `execution.limits.maxInvocationSeconds` hard-caps a single run; on overrun the harness kills it and frees the global slot, channel slot and session lane. Coupled to the §8.6 drain;
  - **durable idempotency.** The dedup key-set inherits `persistence.enabled`: durable under the PVC when persistence is on, in-process otherwise (redelivery-after-restart double-fire then a declared loss mode);
  - **`expire`-in-lane is never silent.** Resolution derives from the channel's source trait: `503` to synchronous sources, NACK-for-redelivery to async sources that retry, drop-with-log only for async sources that do not;
  - **Declared loss modes** section: SIGKILL-during-queue (existing), cron misfire across restart (no catch-up), and shared-`AgentRuntime` rollout blast radius (N simultaneous no-HA windows);
  - **`model.small` removed** as an Agent field; an auxiliary model, if the engine supports one, arrives via hydrate/engine config, not as an author-facing field.
- **v1.2.4** — Access-axis split. Separates the two orthogonal concerns that `hydration.source` overloaded — *how the agent accesses models/MCP/A2A at execution time* vs *where workspace context is hydrated from*:
  - introduces `Agent.spec.access.mode: ach | direct` as the **master access axis** (named `access`, not `governance`, because `direct` is explicitly not ACH-governed);
  - `access.ach.{environmentRef, keySecretRef}` moves here from `hydration.ach`;
  - `access.direct.{provider, baseUrl, keySecretRef}` is the direct binding; **`provider` is a free string** — not validated in the control plane; the harness resolves it at startup and, if it does not implement that dialect, the workload simply does not become ready and the cause is in the pod logs (no fabricated status condition);
  - hydration becomes a **derived follower**: `hydration.sources[]` (the `hydration.source` enum and the `local` nesting are removed); required when `access.mode: direct`, additive/optional when `access.mode: ach`;
  - the governed-hydration-with-ungoverned-access footgun is made **structurally unrepresentable**;
  - merges **O1 (Forwarder credential contract)** and **O11 (local provider wiring)** into a single **harness access translation contract**: the Agent declares a binding, the harness materializes engine config; the Agent never writes raw provider env vars;
  - moves the exotic-provider escape hatch to `AgentRuntime` (`directExotic`, platform-owned `envFrom`), reduced to providers with no harness dialect at all;
  - formalizes the recurring **API-modeled / not-yet-executable** pattern (used by `sideEffect` §20.3 and the additive `ach` + `sources[]` hybrid §11.4) as a first-class concept with a status reason, instead of explaining it ad-hoc each time.
- **v1.2.3** — Hydration and startup-sequence pass:
  - hydration runs as an **init container**; the agent container starts only after the init succeeds, so the runtime does not (and need not) self-report hydration — the gate is structural (Kubernetes), not runtime-controlled;
  - `Hydrated` status is **derived from `pod.status.initContainerStatuses`** (terminated, exit 0), not from a runtime self-report;
  - redefines `readyz`: Ready when **all channel adapters are listening** (hydration is already guaranteed by the init gate, so it is removed from the readiness definition);
  - defines the explicit **main-container startup order**: channels listen first → `readyz` Ready (accept and **buffer in lanes**) → engine becomes ready → lane draining toward the engine begins;
  - adopts **accept-and-buffer (option A)** for the cold-start window: events that arrive after channels are listening but before the engine is ready are accepted (200) and held in the session lane; `expire` runs from lane entry;
  - updates the loss model accordingly: the lane-loss window now includes **engine warmup**, not only restart — buffered pre-engine events are lost on a hard crash before draining (same risk as any lane entry; O7 closes it);
  - refines O12: "hydration failed" is now cleanly observable via init-container status; only "init exit 0 but empty/incorrect content" remains a main-container concern.
- **v1.2.2** — Correctness-hardening pass after design review. Collapses the execution model to a single replica and removes the replica/workload-type surface entirely, which structurally closes three correctness blockers (session-state corruption, lane racing, cron double-firing):
  - removes `replicas` and `workload.type` (`Deployment | StatefulSet`) from the API; the operator selects the Kubernetes primitive and runs exactly **one replica per Agent** in v1;
  - replaces workload-shape selection with a single `persistence` knob on `AgentRuntime.execution.kubernetes`;
  - makes runtime capabilities **derived** by the operator from concrete configuration, never declared as a matrix, to avoid capability drift; `durableSessions` derives from `persistence.enabled`;
  - adds `session.continuity` (`durable | bestEffort`) as explicit author intent; a non-durable runtime under `session.mode != none` produces a **warning, not corruption**, because single-replica guarantees a single writer;
  - recovers per-session serialization (former R8) as a first-class **three-layer concurrency model**: a global `maxConcurrentInvocations` ceiling with overcommit, per-channel `concurrency`/`expire` caps, and implicit FIFO serialization per session key;
  - adds idempotency deduplication by `event.id` (router-level; refined in v1.4.0);
  - eliminates cron double-firing structurally (single scheduler under single-replica); **no leader election** in v1;
  - defines **strict readiness**: `readyz` reports Ready only when hydration completed AND all channel adapters are listening, so the inbound gap is a retryable 503, not a swallowed event;
  - adds **graceful drain** on `SIGTERM` (preStop delay, stop intake, drain session lanes within `terminationGracePeriodSeconds`);
  - declares **no ingress HA in v1** as an explicit design property;
  - merges the external-substrate open items (external session store, durable lane, multi-replica, on-demand) into a single future direction (O7).
- **v1.2.1** — Bake-readiness pass: namespace-local `AgentRuntime`; first-class `hydration.source: local`; `MemoryProfile`; `reply`/`sideEffect` clarification and first-bake behavior; Service-only inbound exposure; stable internal endpoints; deterministic resource naming; generated resource status; Forwarder credential contract left as an open item.
- **v1.2** — Two CRDs plus `MemoryProfile`; complete reusable `AgentRuntime`; simple product-facing `Agent`; removed `onDemand` and `capabilities`; `prompt.source` + `prompt.compose`; `session.mode = none | auto | custom`; inline channels; collapsed runtime responsibilities.
- **v1.1** — First consolidated global specification: two-CRD model, hydration, prompt stack, sessions, memory, runtime components, invocation envelope, execution flows.
- **0.1** — Implementation-oriented design draft.

---

# Part I — Concept

## 1. Purpose and Thesis

ACH Agent Runtime is the operator and runtime profile system that runs managed AI agents which consume either:

1. the governed ACH ecosystem; or
2. directly declared local/non-ACH content sources.

> **ACH empowers the agent; the Agent Runtime operates it.**

ACH Hub defines what an agent may use through an `Environment`: models, MCP servers, A2A agents, prompts, plugins, artifacts, identity, budget and governance.

ACH Agent Runtime owns the part that the Hub does not own: how a concrete agent is run, hydrated, activated through channels, resumed through sessions, serialized under concurrency, and constrained through a channel/action contract.

The central split is four objects owned by three teams:

```text
AgentRuntime       = how it runs (engine, pod, persistence)              [SRE / platform]
CapabilityProfile  = what it consumes (access + hydration), reference-only [AI engineers]
MemoryProfile      = memory integration, reference-only                   [AI engineers]
Agent              = behavior + choice (prompt, channels, response,       [consumer / business]
                     model, provider, memory intent)
```

The design goal is that SRE/platform teams define `AgentRuntime`, AI-engineering teams define `CapabilityProfile` and `MemoryProfile` (which carry credentials and therefore sit behind an RBAC boundary), and product/business teams define `Agent` objects — referencing the three by name, with a small YAML surface, and eventually through a UI. A consumer who writes an `Agent` cannot author access credentials or memory backends; those are referenced, not inlined (Principle: §4).

In v1 every Agent runs as a **single replica**. This is a deliberate simplification: it is the structural guarantee behind a single writer to engine session state, the correctness of the in-process session lane, and a single cron scheduler. Multi-replica execution is deferred to an external durable substrate (O7).

## 2. What the Agent Runtime Is Not

ACH Agent Runtime is not:

- a model gateway;
- a governance engine;
- a LiteLLM operator;
- a replacement for ACH Hub;
- a general-purpose agent framework;
- a per-message workflow engine;
- a universal plugin marketplace;
- a generic memory database provisioner.

When the `CapabilityProfile` has an `ach` block, model, MCP and A2A traffic exits through the ACH Forwarder using an `ek_`. The runtime never configures a workload to call LiteLLM, model providers, MCP backends or A2A backends directly.

When the `CapabilityProfile` has no `ach` block (access via `envFrom` only), the agent reaches a provider directly and runs outside ACH Hub governance. This is useful for standalone, non-ACH, development, private or bootstrap deployments. It must not be described as ACH-governed: it does not inherit ACH Hub budgets, access groups, Environment policy or LiteLLM enforcement unless separately configured outside this spec. "Governed" is derived from the presence of `ach`, not a declared mode.

## 3. Position in the ACH Ecosystem

| Plane | Owner | Relationship |
|---|---|---|
| Capability definition | ACH Hub `Environment` | Addressed by `CapabilityProfile.spec.ach` (`endpoint` + `name`), an external coordinate |
| Access + governed hydration | `CapabilityProfile` (`ach`) | Governed access and Environment hydration come together |
| Extra / own hydration | `CapabilityProfile` (`sources[]`) | Additive over `ach`, or the sole context without `ach` |
| Raw env passthrough | `CapabilityProfile` (`envFrom`) | Optional; ACH contract env wins when `ach` present |
| Models / MCP / A2A | ACH Forwarder + LiteLLM | Consumed through Forwarder when `ach` present |
| Identity / budget / governance | ACH Hub + LiteLLM | Inherited through `ek_` + Forwarder when `ach` present |
| Runtime profile | `AgentRuntime` | Engine, execution substrate, pod template, persistence |
| Memory integration | `MemoryProfile` | Backend type, endpoint, auth and reusable memory defaults |
| Functional agent | `Agent` | `capabilityProfileRef`, prompt, channels, model+provider choice, memory intent, redaction, execution limits |
| Runtime sessions | Runtime data | Not Kubernetes objects |
| Invocations/events | Runtime data | Not Kubernetes objects |

```mermaid
flowchart TD
    Env[ACH Hub Environment<br/>models, MCP, A2A, prompts, plugins, artifacts, policy]
    Direct[Own content sources<br/>GitHub, Git, HTTP, future stores]
    Fwd[ACH Forwarder]
    Lite[LiteLLM]
    Providers[Model / MCP / A2A backends]

    subgraph NS[Kubernetes namespace]
        AR[AgentRuntime<br/>how it runs -- SRE]
        CP[CapabilityProfile<br/>access + hydration -- AI eng]
        MP[MemoryProfile<br/>memory -- AI eng]
        A[Agent<br/>behavior + choice -- consumer]
        Op[ACH Agent Runtime Operator]

        AR --> Op
        CP --> Op
        MP --> Op
        A --> Op

        Op --> Workload[Generated single-replica workload<br/>operator-selected primitive]

        subgraph Pod[Agent Pod -- single replica]
            Init[initContainer: hydrate workspace]
            Main[main runtime process]

            subgraph Runtime[Collapsed v1 runtime responsibilities]
                Ch[Channel adapters]
                Router[Invocation router + session lanes]
                Sess[Session manager]
                Harness[Harness]
                Mem[Memory adapter]
                Act[Action adapters]
            end

            Init --> Main
            Main --> Runtime
        end

        Workload --> Pod
    end

    Env -->|CapabilityProfile.ach: endpoint + name + ek_| Init
    Direct -->|CapabilityProfile.sources| Init
    Main -->|governed (ach): model/MCP/A2A via ek_| Fwd
    Main -->|non-governed: provider directly via envFrom creds| Providers
    Fwd --> Lite
    Lite --> Providers
    Mem --> MemoryBackend[External memory backend<br/>Hindsight, Redis, RAG, MCP memory, etc.]

    External[External channels<br/>GitLab, Slack, Telegram, Cron, HTTP] --> Ch
    Act --> External
```

## 4. Design Principles

1. **Small CRD surface in v1.** `AgentRuntime`, `CapabilityProfile`, `MemoryProfile` and `Agent` are the only v1 CRDs. The two profiles exist because access and memory carry credentials and have separate ownership, so they must sit behind their own RBAC boundary.
2. **Objects that cross an RBAC boundary are referenced, never inlined.** `CapabilityProfile` and `MemoryProfile` carry credentials (`ek_`, provider keys, backend tokens). An `Agent` references them by name; it cannot inline access or memory config. The reference *is* the security control: to get access, the `Agent` must point at an object a differently-permissioned team created.
3. **Scope ownership.** `AgentRuntime` → SRE/platform (how it runs). `CapabilityProfile` + `MemoryProfile` → AI engineers (what it may access, with credentials). `Agent` → consumer/business (behavior + choice). Three objects, three RBAC scopes.
4. **Profiles are reusable and impersonal.** `AgentRuntime` holds engine/execution only; `CapabilityProfile` holds access + hydration only; `MemoryProfile` holds memory integration only. None holds persona, prompt, channels or business-specific intent.
5. **Agents are behavior + choice.** An `Agent` declares prompt, channels, response, memory intent, and the *choice* of `model`/`provider` — definable by someone who understands the business purpose, without knowing Kubernetes storage primitives, access credentials or memory plumbing.
6. **Single replica per Agent in v1.** The operator runs exactly one replica. `replicas` and `workload.type` are not part of the API. The only storage choice exposed is `persistence` (disk or no disk).
7. **Capabilities are derived, not declared.** The operator computes runtime capabilities (e.g. `durableSessions`) from concrete configuration. "Governed" is likewise derived — from the presence of a `CapabilityProfile.ach` block — never a declared mode. Declared matrices drift from actual behavior.
8. **One invocation per session key at a time.** Per-session serialization is a correctness property, implicit and not configurable.
9. **Concurrency is overcommit.** Per-channel caps plus one global ceiling, no reservation. `sum(channel.concurrency)` may exceed the global ceiling.
10. **No ingress HA in v1.** A single replica means a brief inbound gap on every restart/rehydration. Inbound sources must retry; the runtime degrades this to a retryable 503 and drains gracefully on shutdown.
11. **All CRDs are namespace-local in v1.** `runtimeRef`, `capabilityProfileRef` and `memory.profileRef` resolve only within the `Agent` namespace. Cross-namespace references are rejected. (Note: `CapabilityProfile.ach` addresses the Hub as an *external coordinate*, not a CR reference — the Hub may live outside the cluster.)
12. **Channels are inline in v1.** A channel is a per-agent binding and adapter configuration, not a standalone CRD.
13. **Sessions and invocations are runtime data.** Neither a session nor a message/event becomes a Kubernetes object.
14. **No `onDemand` in v1.** v1 runs a resident single-replica workload generated from `AgentRuntime + CapabilityProfile + Agent`.
15. **Explicit-action default.** When the `CapabilityProfile` is governed (`ach` present), visible output must be emitted through validated channel actions.
16. **Single owner per plane.** ACH Hub owns capability governance; Agent Runtime owns workload execution, hydration wiring, memory integration, concurrency, and channel activation.
17. **`CapabilityProfile` composes optional blocks.** `ach?` (governed access + governed hydration), `sources?` (extra/own context), `envFrom?` (raw env) are independent and composable. At least one of `ach`/`envFrom` is required (there must be a path to a model). With `ach`, `sources[]` is additive; without `ach`, `sources[]` is the sole context and the profile is non-governed.
18. **Service-only inbound exposure in v1.** The operator creates internal Services where needed. Ingress, Gateway API, DNS and external webhook registration are platform responsibilities.

---

# Part II — API Surface

## 5. CRDs

```text
AgentRuntime       # how it runs: engine + execution substrate (SRE/platform)
CapabilityProfile  # what it consumes: access + hydration, reference-only (AI engineers)  [§11]
MemoryProfile      # memory integration, reference-only (AI engineers)
Agent              # behavior + choice; references the three (consumer/business)
```

## 6. Not CRDs in v1

| Concept | Why not a CRD in v1 |
|---|---|
| `Channel` | Inline and usually 1:1 with an Agent. Promote only if shared across agents or owned by a separate platform/RBAC lifecycle. |
| `AgentSession` | Runtime data keyed by channel/session key. No per-session compute is provisioned. |
| `AgentInvocation` | Per-event runtime payload. Creating Kubernetes objects per message is an anti-pattern. |
| `AgentExecutionEnvironment` | Execution substrate lives inside `AgentRuntime.execution` to keep the CRD surface minimal. |
| `HydrationSource` | Sources are declared inline under `CapabilityProfile.spec.sources[]`. Promote only if source catalogs become reusable/owned separately. |

---

# Part III — `AgentRuntime`

## 7. `AgentRuntime`

`AgentRuntime` defines a reusable runtime profile. It combines:

- the engine type and engine-specific configuration;
- how the engine is invoked;
- where its working directory and session directory are;
- the execution substrate;
- whether the workload has durable persistence;
- pod template, lifecycle and operational defaults.

It is usually owned by platform/engineering teams.

`AgentRuntime` is namespace-local in v1. An `Agent` can only reference an `AgentRuntime` in its own namespace.

The operator never exposes a Kubernetes workload type or replica count through this CRD. It runs a single replica and selects the primitive itself (see §8).

### 7.1 `AgentRuntime` example — opencode persistent runtime

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: AgentRuntime
metadata:
  name: opencode-persistent-small
  namespace: engineering
spec:
  engine:
    type: opencode
    opencode:
      binaryPath: opencode
      workDir: /workspace
      sessionDir: /var/lib/ach-agent/opencode/sessions   # lives under the persistent volume
      thinkingLevel: medium
      steps: 50
      startupTimeoutSeconds: 30
      shared:
        enabled: true
        ttlSeconds: 120

  health:                                    # substrate-agnostic health endpoint (§7.5)
    enabled: true
    host: "0.0.0.0"
    port: 8000

  execution:
    type: kubernetes
    kubernetes:
      persistence:
        enabled: true
        size: 10Gi
        storageClassName: standard
        mountPath: /var/lib/ach-agent
        retainPolicy: Retain
      terminationGracePeriodSeconds: 120     # sized against typical invocation duration

      podTemplate:
        spec:
          serviceAccountName: agent-runtime
          containers:
            - name: agent
              image: ghcr.io/ackstorm/agent-runtime-opencode:latest
              resources:
                requests:
                  cpu: "200m"
                  memory: "512Mi"
                limits:
                  cpu: "1"
                  memory: "2Gi"
```

### 7.2 `AgentRuntime` example — opencode ephemeral runtime

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: AgentRuntime
metadata:
  name: opencode-ephemeral
  namespace: engineering
spec:
  engine:
    type: opencode
    opencode:
      binaryPath: opencode
      workDir: /workspace
      sessionDir: /tmp/opencode/sessions
      thinkingLevel: low
      steps: 30
      startupTimeoutSeconds: 30
      shared:
        enabled: true
        ttlSeconds: 120

  execution:
    type: kubernetes
    kubernetes:
      persistence:
        enabled: false
      terminationGracePeriodSeconds: 60

      podTemplate:
        spec:
          serviceAccountName: agent-runtime
          containers:
            - name: agent
              image: ghcr.io/ackstorm/agent-runtime-opencode:latest
              resources:
                requests:
                  cpu: "200m"
                  memory: "512Mi"
                limits:
                  cpu: "1"
                  memory: "2Gi"
```

Note: neither example contains `replicas` or `workload.type`. The only durability choice is `persistence.enabled`.

### 7.3 `AgentRuntime` ownership boundary

`AgentRuntime` MAY contain:

- engine type;
- engine-specific parameters;
- binary path / SDK module / invocation protocol;
- work directory;
- session directory;
- execution substrate;
- persistence definition (disk or no disk);
- termination grace period;
- pod template;
- service account;
- resource requests/limits.

`AgentRuntime` MUST NOT contain:

- persona;
- user-facing prompt;
- `model.default` / `model.provider` selection;
- channels;
- channel secrets;
- memory mission;
- redaction policy specific to one agent;
- **anything about access or credentials** — no ACH coordinate, no `ek_`, no provider binding, no `envFrom` for provider access (all of that lives on `CapabilityProfile`);
- a Kubernetes workload type or replica count (operator-selected, single replica).

### 7.4 Engine types

v1 implementation targets:

```text
opencode
pi
```

Future engine types may include:

```text
claudeCode
codex
cryoAI
custom
awsAgentCore
bedrockAgent
```

Future engine types may carry their own sub-objects. They should not force fields into the generic engine shape before implementation experience justifies them.

### 7.5 Health endpoint

The runtime exposes liveness/readiness on a configurable endpoint. It is declared on `AgentRuntime` (it is "how it runs", SRE-owned) and is **substrate-agnostic** — the same block works whether the substrate is Kubernetes, CloudRun or another host, each of which maps it to its own health-check mechanism.

```yaml
spec:
  health:
    enabled: true
    host: "0.0.0.0"
    port: 8000
```

`healthz` (liveness) and `readyz` (readiness; adapters-listening, §8.5) are served here. The engine startup deadline (`startupTimeoutSeconds`, §8.5) governs when a failed engine turns the pod not-ready through this endpoint / process exit.

### 7.6 Execution types

v1 implementation target:

```text
kubernetes
```

Future execution types may include:

```text
awsManaged
externalHttp
claudeManaged
```

In v1, only `execution.type: kubernetes` is normative.

---

# Part IV — Kubernetes Execution

## 8. Kubernetes execution

When `AgentRuntime.spec.execution.type: kubernetes`, the operator materializes a **single-replica** workload from `AgentRuntime + CapabilityProfile + Agent`. The operator selects the Kubernetes primitive; the API does not expose workload type or replica count in v1.

### 8.1 Single-replica execution model

- Exactly one running replica per Agent in v1.
- The operator chooses the primitive. The recommended implementation is a `Deployment` with `replicas: 1` and `strategy: Recreate`, plus an optional PVC when persistence is enabled. This is an implementation detail and not part of the API; the operator may change it without a CRD change.
- `Recreate` (terminate the old pod before starting the new one) preserves the single-writer guarantee on the engine session store and avoids the StatefulSet single-replica volume-detach failure mode.
- Single-replica is the structural guarantee behind three correctness properties:
  - a **single writer** to engine session state (no JSONL corruption);
  - correctness of the **in-process session lane** (§18);
  - a **single cron scheduler** (no double-firing).
- **Consequence, declared:** there is no ingress high-availability in v1. Every restart/rehydration is a brief window with no ready endpoint. Inbound sources must retry. The runtime degrades this to a retryable 503 (§8.5) and drains gracefully on shutdown (§8.6).
- Horizontal scale-out — for ingress HA, stateful sharding, or on-demand workers — requires an external durable substrate and is deferred (O7).

### 8.2 Persistence

Persistence is the only storage knob. It controls whether the engine work/session directory survives pod restart.

```yaml
execution:
  type: kubernetes
  kubernetes:
    persistence:
      enabled: true            # the single durability choice
      size: 10Gi               # required when enabled
      storageClassName: standard
      mountPath: /var/lib/ach-agent
      retainPolicy: Retain     # Retain | Delete
```

Rules:

- `persistence.enabled: true` → the operator provisions a PVC and mounts it at `mountPath`. The engine `sessionDir` should live under `mountPath` for durable resume.
- `persistence.enabled: false` → no durable volume; engine session state is ephemeral and lost on restart.
- `size` is required when `enabled: true`.
- `retainPolicy` defaults to `Retain`. The operator must avoid accidental deletion of session stores.
- The operator derives `durableSessions = persistence.enabled` (see §8.3).

### 8.3 Derived capabilities

v1 does not declare a capability matrix on `AgentRuntime`. The operator **derives** runtime capabilities from concrete configuration and validates Agent requirements against them. A declared matrix is avoided because declared capabilities drift from actual behavior.

Derived in v1:

| Capability | Derived from |
|---|---|
| `durableSessions` | `persistence.enabled` |
| `singleWriterPerSession` | always `true` in v1 (single replica) |

Requirement side (Agent): `session.mode` and `session.continuity` (§17.4) imply whether durable sessions are needed. Reconcile compares the requirement against the derived capability and emits a **warning** (not a hard failure) when a non-durable runtime is used under `session.mode != none` with `continuity: durable`. Because single-replica guarantees a single writer, the mismatch degrades continuity (sessions lost on restart) but never corrupts state.

Only capabilities that are not structurally derivable should ever be declared explicitly (none in v1).

### 8.4 Volume retain policy

`retainPolicy` controls what happens to the PVC when the generated workload is deleted.

```text
Retain  # default, safer for session/history preservation
Delete  # allowed only when explicitly configured
```

### 8.5 Startup sequence and readiness

Hydration runs as an **init container** (§11.4). Kubernetes guarantees the agent (main) container starts only after the init terminates successfully; if the init fails, the main container never starts and the Pod stays in `Init:Error` / `CrashLoopBackOff`. The runtime therefore does not — and need not — self-report or gate on hydration: **the gate is structural**. The operator derives the `Hydrated` status from `pod.status.initContainerStatuses` (terminated, exit 0), not from a runtime self-report.

Main-container startup order:

```text
init container: hydrate workspace   (Kubernetes gate; main does not start until this exits 0)
  → main container:
      1. channel adapters start listening      → readyz reports Ready
         (inbound events are accepted and BUFFERED in their session lane)
      2. engine becomes ready (internal state)  → lane draining toward the engine begins
```

`readyz` reports Ready when **all enabled channel adapters are listening**. Hydration is already guaranteed by the init gate, so it is not part of the readiness definition. Engine readiness is **internal state** that governs when the lane begins draining toward the engine; it is **not** a readiness gate.

Cold-start window — **accept and buffer (option A), with a proven-start gate (A′):** between "channels listening" and "engine ready", an inbound event is accepted (`200`) and held in its session lane (§18.3) — **but only after the engine has reached ready at least once in this pod's life.** During a pod's **first** warmup, inbound is instead NACK'd / `503`'d (non-silent, §18.4.1) so the source retries, rather than buffered. The reason: returning `200` tells the source the event is delivered (it will not redeliver); if the engine then *terminally* fails startup (§8.5 deadline → process exit), already-`200`'d buffered events are lost and unrecoverable — and terminal failure is most likely precisely during a fresh deploy / config change, i.e. the first warmup. A′ keeps those events retryable when failure is likely, and reverts to the minimal-gap accept-and-buffer for later transient warmups (e.g. a Forwarder blip after a healthy start). Cost: one boolean of pod state ("has the engine been ready once?"). This closes the §30.1 loss mode the source could otherwise have recovered from on its own.

**Engine startup deadline — `startupTimeoutSeconds` (the warming-vs-failed line).** Engine non-readiness is only "warming" *within* the deadline. If the engine does not reach ready within `engine.<type>.startupTimeoutSeconds`, that is not warmup — it is a **terminal startup failure** (unknown dialect, unreachable Forwarder, missing required prompt, bad credential). On exceeding the deadline, **the runtime process exits with error**; the substrate (Kubernetes, CloudRun, …) then treats it as any failed container — the pod becomes not-ready and is restarted (CrashLoopBackOff on k8s). This is what makes the O12/§36 promise true *without* a runtime self-report: a harness that cannot start is observable as `WorkloadReady: False` via `pod.status`, and the specific cause is in the logs. A transient cause (a Forwarder blip) self-heals on restart; a terminal one (a dialect that does not exist) crash-loops loudly, which is the correct signal. Events buffered during a warmup that ends in startup-failure follow §30.1 / §18.4.1 (non-silent loss).

During startup/rehydration, before adapters are listening, the endpoint is absent or NotReady, so inbound webhooks receive connection refused / `503` and the source retries.

`healthz` is liveness only; `readyz` is the adapters-listening gate above. The `health` endpoint location/binding is configured on `AgentRuntime` (§7.5) so it is substrate-agnostic.

### 8.6 Graceful drain

On `SIGTERM` the runtime drains rather than dropping in-flight and queued work:

1. A `preStop` hook delays briefly (e.g. 3–5s) so the pod is removed from the Service endpoints **before** intake stops (endpoint deprovisioning is not instantaneous).
2. `readyz` flips to NotReady; the runtime stops accepting new events.
3. The runtime drains the session lanes (§18) — running queued-but-not-started invocations — within `terminationGracePeriodSeconds`.

`terminationGracePeriodSeconds` (`AgentRuntime.execution.kubernetes`) must be sized against typical invocation duration, not the 30s default. It composes with the invocation watchdog (§18.7): each in-flight run gets at most `min(remaining grace period, maxInvocationSeconds)` before being killed, so the grace period never waits unbounded — a run that would exceed `maxInvocationSeconds` is killed by the watchdog regardless.

Drain is unaffected by the accept-and-buffer choice in §8.5: option A widens the *cold-start* loss window, not the *shutdown* path. Planned shutdowns still drain buffered work cleanly.

**Known limitation:** a hard crash or `SIGKILL` loses queued-but-not-started lane entries (and, with accept-and-buffer, events buffered during engine warmup). Planned restarts drain cleanly; a durable lane (O7) closes it fully. This and the other accepted gaps are catalogued in §30.1 (Declared loss modes).

---

# Part V — `MemoryProfile`

## 9. `MemoryProfile`

`MemoryProfile` defines a reusable memory integration profile.

It abstracts the agent author away from:

- backend type;
- backend URL;
- protocol details;
- auth secret references;
- default mental models;
- backend-specific connection settings.

It is usually owned by platform/engineering teams.

`MemoryProfile` is namespace-local in v1. An `Agent` can only reference a `MemoryProfile` in its own namespace. (Cluster-scoped reuse is deferred — O6.)

### 9.1 Memory model

Memory and sessions are separate.

| Concept | Scope | Keyed by | Mechanism |
|---|---|---|---|
| Session state | Short-term conversation/thread state | channel session key | engine session store |
| Long-term memory | Cross-session learned knowledge | agent/channel/scope | external memory backend |

A runtime may use both:

- engine-native session resume for short-term continuity;
- external memory for long-term cross-session knowledge.

### 9.2 `MemoryProfile` example — Hindsight

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: MemoryProfile
metadata:
  name: hindsight-code-review
  namespace: engineering
spec:
  type: hindsight
  hindsight:
    mcp:
      url: http://hindsight-api.hindsight.svc:8888/mcp
      auth:
        secretRef:
          name: hindsight-auth
          key: token
  defaults:
    mentalModels:
      - architecture
      - conventions
      - recurring-issues
      - team-reviewer
```

### 9.3 Future memory profile types

v1 implementation target:

```text
hindsight
```

Future memory profile types may include:

```text
redis
rag
vectorStore
mcpMemory
custom
```

### 9.4 `MemoryProfile` ownership boundary

`MemoryProfile` MAY contain:

- memory backend type;
- backend endpoint;
- protocol configuration;
- auth secret references;
- reusable default mental models;
- backend-specific connection options;
- timeout/retry defaults.

`MemoryProfile` MUST NOT contain:

- agent persona;
- agent prompt;
- channel configuration;
- channel secrets;
- business-specific memory mission;
- per-agent memory scope expression;
- per-agent redaction policy.

---

# Part VI — `Agent`

## 10. `Agent`

`Agent` is the user-facing definition of a concrete agent.

It is intentionally simple. It references three objects (`runtimeRef`, `capabilityProfileRef`, `memory.profileRef` — all reference-only) and declares behavior and choice:

- prompt;
- `model` choice (`default` + `provider`);
- channels;
- memory intent (mission/scope/mentalModels);
- redaction;
- agent-specific execution limits (concurrency).

It does **not** contain access or hydration (those live in the referenced `CapabilityProfile`).

### 10.1 Agent example — governed GitLab reviewer with memory

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: Agent
metadata:
  name: gitlab-ackstorm
  namespace: engineering
spec:
  runtimeRef:
    name: opencode-persistent-small        # SRE
  capabilityProfileRef:
    name: engineering-prod-ach              # AI eng (governed: has ach)

  execution:
    limits:
      maxConcurrentInvocations: 8        # global ceiling: opencode processes in the pod (default 1)
      maxInvocationSeconds: 1800         # hard cap on a single run; overrun is killed (§18.7)
      maxQueuedTotal: 100                # max buffered events in the pod (default 100; §18.8)
      idempotencyWindowSeconds: 3600     # dedup window (default 3600; §18.4)

  prompt:
    source:
      type: file
      file:
        path: prompts/agents/pr-reviewer.md
        baseDir: workspace
        required: true
    compose: append

  model:
    default: gemini-3-flash
    provider: gemini                       # choice; emits PROVIDER_TYPE. Default openai.

  memory:
    profileRef:
      name: hindsight-code-review          # AI eng (reference-only)
    mission: "AI code reviewer focused on architecture, conventions and recurring bugs."
    scope: "{project_id}"
    mentalModels:
      - architecture
      - conventions
      - recurring-issues

  channels:
    # webhook channel: MR review (one channel per event type — §14.4)
    - name: gitlab-mr-review
      type: webhook
      enabled: true
      concurrency: 4                     # per-channel cap (QoS), not a reservation
      expire: 120                        # default; raised here only illustratively
      session:
        mode: auto
        continuity: durable
        ttlSeconds: 604800
      response:
        mode: actionRequired
        fallback: fail
      responseActions:
        - name: channel_message
          kind: reply
          inputSchema:
            type: object
            required: ["text"]
            properties:
              text:
                type: string
      webhook:
        auth:
          type: hmac
          secretRef: { name: agent-gitlab-webhook, key: secret }
        prompt: |                        # ephemeral per-event prompt (§12.3 layer 5b)
          Review this merge request: {{ .object_attributes.url }}
        deliver:
          type: gitlab_comment
          config:
            tokenSecretRef: { name: agent-gitlab, key: token }

    # webhook channel: push (separate event type → separate channel, its own prompt)
    - name: gitlab-push
      type: webhook
      enabled: true
      concurrency: 2
      expire: 120
      session: { mode: none }
      response: { mode: disabled }
      webhook:
        auth:
          type: hmac
          secretRef: { name: agent-gitlab-webhook, key: secret }
        prompt: |
          A push landed on {{ .ref }} by {{ .user_username }}. Summarize notable changes.
        deliver:
          type: log

    # cron channel: each task is its own cron channel with its own prompt
    - name: daily-security
      type: cron
      enabled: true
      concurrency: 1
      cron:
        schedule: "0 8 * * 1-5"
        timezone: Europe/Madrid
      prompt: |                           # cron per-task prompt (same field as webhook; §12.3 layer 5b)
        Run today's security review: scan main for new CVEs and open an issue if any are critical.
      session: { mode: custom, key: cron:daily-security }
      response: { mode: disabled }
```

> The conservative global defaults (`maxConcurrentInvocations: 1`, `concurrency: 1`, `expire: 120`) are a **safe floor**, not tuned for minutes-long runs. A code-review agent like this one **must** raise `concurrency` (and may raise `expire`) so concurrent MRs are not bounced; published runtime profiles for such workloads should ship appropriate caps (§7, §18.2).

### 10.2 Agent example — non-governed, referencing a custom CapabilityProfile

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: Agent
metadata:
  name: direct-deepseek-reviewer
  namespace: engineering
spec:
  runtimeRef:
    name: opencode-persistent-small
  capabilityProfileRef:
    name: dev-direct-deepseek               # AI eng (non-governed: no ach, access via envFrom; §11)

  prompt:
    source:
      type: inline
      inline: |
        You are a local engineering assistant.
        You help with code review, GitOps, Terraform and incident analysis.
    compose: append

  model:
    default: deepseek-chat                   # choice; resolved against whatever the profile's envFrom points at
    provider: openai-compatible              # emits PROVIDER_TYPE

  channels:
    - name: manual
      type: webhook                          # synchronous use: caller holds the connection for the reply
      enabled: true
      concurrency: 2
      expire: 120
      session:
        mode: none
      response:
        mode: automatic
      webhook:
        auth:
          type: bearer
          secretRef: { name: manual-channel-token, key: token }
        deliver:
          type: reply                        # answer on the triggering connection
```

The access and the sources live in the referenced `CapabilityProfile` (`dev-direct-deepseek`, §11), authored by AI engineers — not in the `Agent`. The consumer only chooses `model`/`provider` and declares behavior. `model`/`provider` are the agent's choice within what the profile makes reachable; a mismatch (e.g. a provider the engine cannot speak against the profile's endpoint) surfaces at runtime, not at apply.

### 10.3 Agent ownership boundary

`Agent` MAY contain:

- `runtimeRef`, `capabilityProfileRef`, `memory.profileRef` (all reference-only);
- agent-specific execution limits (`maxConcurrentInvocations`, `maxInvocationSeconds`, `idempotencyWindowSeconds`);
- `model` choice (`default` + `provider`);
- prompt source and composition;
- channels (including per-channel `concurrency`/`expire`);
- session policy per channel (`mode`, `continuity`, `ttlSeconds`, `key`);
- channel response/action contract;
- agent-specific memory intent (`mission`, `scope`, `mentalModels`);
- redaction.

`Agent` MUST NOT contain:

- pod template, engine binary path, session/work directory, runtime image, platform service account, persistence/PVC config (all on `AgentRuntime`);
- **any access or hydration config** — no ACH coordinate, no `ek_`, no `envFrom`, no `sources[]` (all on `CapabilityProfile`);
- memory backend URL or credentials (on `MemoryProfile`).

---

# Part VII — `CapabilityProfile`, Prompt and Model

## 11. `CapabilityProfile`

`CapabilityProfile` is the reusable, reference-only object that defines **the package of capabilities an agent consumes**: how it accesses models/MCP/A2A and what context is hydrated into its workspace. It is owned by AI engineers and carries credentials, so it sits behind its own RBAC boundary — an `Agent` references it by name and can never inline its contents (Principle 2).

It has no `mode` discriminator. Instead it composes three optional blocks; "governed" is **derived** from whether `ach` is present.

```text
spec:
  ach?        # governed access + governed Environment hydration (external coordinate)
  sources?    # extra/own context (additive over ach, or the sole context without ach)
  envFrom?    # raw env passthrough into the pod
```

**Rule:** at least one of `ach` / `envFrom` is required — there must be a path to a model. `sources` alone (no way to reach a model) is invalid.

### 11.1 `ach` — governed access + governed hydration

A single block brings **both** governed access and governed hydration, because an ACH Environment is one package (models/MCP/A2A *and* prompts/plugins/artifacts, all governed). It is addressed as an **external coordinate** — `endpoint` + `name` + `secretRef` — not as a reference to an in-cluster CR, because the Hub may run outside the cluster (another cluster, SaaS).

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: CapabilityProfile
metadata:
  name: engineering-prod-ach
  namespace: engineering
spec:
  ach:
    endpoint: https://ach.ackstorm.ai      # where the Hub/Forwarder is (may be off-cluster)
    name: engineering-prod                  # which ACH Hub Environment
    secretRef:
      name: pr-reviewer-ek                  # the ek_
      key: ek
  sources:                                  # OPTIONAL, additive over the Environment (see §11.3)
    - source: git:https://git.ackstorm.com/blueprints/agents/extra-house-style.git
      plugins: [security-baseline]
      secretRef: { name: internal-git-token, key: token }   # per-source
  envFrom:                                  # OPTIONAL raw env (see §11.4); ACH contract wins on collision
    - secretRef: { name: some-debug-env }
```

When `ach` is present:

- model/MCP/A2A traffic exits through the ACH Forwarder using the `ek_`; provider keys are never exposed to the workload.
- ACH Hub Environment policy, budgets and access constraints apply via the Forwarder and LiteLLM.
- the Environment hydrates context; `sources[]` is additive on top.
- the profile is **governed** (this is what "governed" means — it is derived from this block's presence).

### 11.2 No `ach` — non-governed

A profile with no `ach` block reaches its model through `envFrom` and hydrates only from `sources[]`. It is **non-governed**: no ACH budget, attribution or no-leak guarantee from the system. This is the standalone/dev/private case.

```yaml
apiVersion: runtime.ackstorm.ai/v1alpha1
kind: CapabilityProfile
metadata:
  name: dev-direct-deepseek
  namespace: engineering
spec:
  sources:
    - source: github:ackstorm/engineering-agent-skills
      plugins: [code-review, terraform, gitops]
    - source: git:https://git.ackstorm.com/blueprints/agents/private-skills.git
      plugins: [internal-platform-review]
      secretRef: { name: internal-git-token, key: token }
  envFrom:
    - secretRef: { name: deepseek-dev-env }   # OPENAI_BASE_URL, OPENAI_API_KEY, etc.
```

The `envFrom` secret is the source of truth for the endpoint+credential; the system does not govern it.

### 11.3 `sources[]` — hydration

`sources[]` declares where workspace context (prompts/plugins/skills/artifacts) is materialized from. Its role is **derived** from whether `ach` is present:

| `ach` present? | `sources[]` |
|---|---|
| yes | **additive** over the Environment's governed hydration |
| no | the **sole** context |

The governed-hydration-with-ungoverned-access footgun is structurally unrepresentable: governed hydration only exists inside the `ach` block, which also routes access through the Forwarder. There is no field for the bad combo.

**Additive `sources[]` with `ach` is API-modeled / not-yet-executable (§22.1):** the v1 runtime MAY reject a non-empty `sources[]` alongside `ach` until additive governed hydration is implemented.

Supported v1 source prefixes: `github:<owner>/<repo>`, `git:<url>`. Future: `http:`, `s3:`, `gcs:`, `oci:`. A source MAY select a subset of `plugins`, and a private source MAY carry a per-source `secretRef`. Source secrets mount only into the hydrator/init process. Cross-namespace source secret references are rejected.

### 11.4 `envFrom` — raw env passthrough

`envFrom` injects environment variables into the pod. It is not "access" — it is a generic passthrough that happens to be the way a non-governed profile supplies provider credentials. It is allowed in **both** cases (with or without `ach`).

**Precedence (the security rule):** when `ach` is present, the operator materializes the reserved ACH contract env **after** `envFrom`, so the contract always wins. Reserved names are `ACH_BASE_URL`, `ACH_API_KEY`, `ACH_PROVIDER_TYPE` (and future `ACH_*`). An `envFrom` key colliding with a reserved name is overwritten — **not an error**, by design: this keeps a governed profile from being subverted through `envFrom` (you cannot point the engine at a raw provider while claiming `ach`), without admission-time collision validation. The overwrite is not silent to the author: reconcile emits a non-blocking warning naming the colliding key (§35). Without `ach`, there is no reserved set and `envFrom` is unconstrained.

### 11.5 Hydrator init container

For Kubernetes execution, the operator injects a hydrator init container whose configuration derives from the referenced `CapabilityProfile`.

For a governed profile (`ach` present):

```yaml
initContainers:
  - name: ach-hydrator
    image: ghcr.io/ackstorm/ach:<pinned>
    workingDir: /workspace
    args: [hydrate, --environment=$(ACH_ENVIRONMENT), --include-runtime, --output=/workspace]
    env:
      # the operator materializes these from the referenced CapabilityProfile.ach
      - { name: ACH_ENVIRONMENT, value: <CapabilityProfile.ach.name> }      # e.g. "engineering-prod"
      - { name: ACH_BASE_URL,    value: <CapabilityProfile.ach.endpoint> }  # e.g. "https://ach.ackstorm.ai"
      - name: ACH_API_KEY
        valueFrom: { secretKeyRef: <CapabilityProfile.ach.secretRef> }      # the ek_ bearer
    volumeMounts: [{ name: workspace, mountPath: /workspace }]
```

`ACH_BASE_URL`/`ACH_ENVIRONMENT`/`ACH_API_KEY` are **derived from the external coordinate** (`ach.endpoint` / `ach.name` / `ach.secretRef`), never hardcoded — the same coordinate the harness uses at runtime (§13.1). Values shown above are placeholders for what the operator substitutes.

For a non-governed profile, the hydrator receives the rendered `sources[]` and any per-source credentials. Result either way: a workspace with `.ach/` / runtime-local state, engine config dirs (`.opencode/`, `.claude/`…), and the materialized prompts/plugins/artifacts.

### 11.6 Rehydration

Rehydration happens on Pod restart: manual rollout, runtime image change, relevant secret/config hash change, or a future ACH Hub notification. The operator does not poll the Hub or source repos in v1.

Rehydration re-runs the init container; the agent container does not restart until the new hydration exits 0 (§8.5). Because v1 is single-replica, **each rehydration is a window during which the inbound endpoint is absent/NotReady**: inbound sources receive a retryable 503 and must retry. Once adapters are listening, events are accepted and buffered while the engine warms up (§8.5, option A). Queued work is drained on shutdown (§8.6) for planned rehydrations; only abnormal termination loses queued or buffered work.

## 12. Prompt

Prompt definition has two independent axes:

```text
source      = where the agent prompt comes from
compose     = how the agent prompt composes with the engine base prompt
```

### 12.1 Inline prompt

```yaml
prompt:
  source:
    type: inline
    inline: |
      You are @ackbot, a senior AI engineer.
  compose: append
```

### 12.2 File prompt

```yaml
prompt:
  source:
    type: file
    file:
      path: prompts/agents/pr-reviewer.md
      baseDir: workspace
      required: true
  compose: append
```

Rules:

- `prompt.source.type` is `inline | file`.
- `prompt.compose` is `replace | append | prepend`.
- `prompt.source.type: inline` requires `inline`.
- `prompt.source.type: file` requires `file.path`.
- In v1, file prompts are resolved relative to the hydrated workspace.
- The operator does not need to read file prompts at reconcile time.
- Missing required prompt files mean the workload does not become ready; the cause is in the pod logs (no fabricated status condition; §22.1, O12).

### 12.3 Prompt stack

The final prompt is assembled in fixed layers:

```text
1. Platform guardrails / security instructions
2. ACH Environment governed context/instructions, if the CapabilityProfile is governed (`ach`)
3. Hydrated context from CapabilityProfile `sources[]`, if present
4. Engine base prompt
5. Agent prompt                          (persona; from Agent.spec.prompt)
5b. Channel prompt                       (ephemeral per-event task; from the active channel.prompt)
6. Channel/event contract and response/action contract
7. Event payload
```

`prompt.compose` controls how layer 5 composes with layer 4 only.

- `replace` — Agent prompt replaces engine base prompt.
- `append` — Agent prompt is appended to engine base prompt.
- `prepend` — Agent prompt is prepended before engine base prompt.

Layers 1, 2 and 3 are never overridden by `Agent.spec.prompt`.

**Layer 5b — the channel prompt (`channel.prompt`).** This is the single, unified per-event prompt field carried by the active channel (Hermes `channel_prompt`, an "ephemeral per-channel system prompt"). It is **optional** and **append-only** in v1 — it adds a task instruction on top of the agent persona (layer 5), never replaces it. It is the same field for every channel type:

- `webhook`: `channel.prompt` may be **templated** with the inbound payload (`{{ … }}`), giving a per-event prompt; with `deliverOnly: true` the rendered prompt is the delivery itself.
- `cron`: `channel.prompt` is a **static** per-task instruction. Multiple cron tasks = multiple cron channels, each with its own `prompt`/`schedule`/`session.key`.

This one field closes **O13**: cron per-task prompts and webhook per-event prompts are not two mechanisms but one `channel.prompt`, injected at layer 5b. No engine/channel code change is required — the field maps to Hermes's native `channel_prompt` (and the cron job's native `prompt`).

## 13. Model resolution

`Agent.spec.model` carries the agent's **choice**, not provider configuration.

```yaml
model:
  default: gemini-3-flash
  provider: gemini            # optional, default openai. Emits PROVIDER_TYPE; chooses the dialect.
```

`model.default` is the model name. `model.provider` is the dialect the harness builds; it is the agent's choice and applies whether the referenced `CapabilityProfile` is governed (`ach`) or not. There is no author-facing `model.small`.

When the referenced `CapabilityProfile` is **governed** (`ach`):

- the name must resolve through the addressed ACH Environment;
- access is enforced by ACH Forwarder / LiteLLM; provider endpoint and credentials are absent from both the Agent and the Profile (only the `ek_` is present).

When the referenced `CapabilityProfile` is **non-governed** (no `ach`):

- the model is reached through whatever the profile's `envFrom` provides;
- access is not governed by ACH Hub unless external controls are configured.

There is no control-plane validation that `model`/`provider` agree with what the profile can actually serve — a mismatch surfaces at runtime, not at apply (§13.1).

## 13.1 Harness access translation contract

This is the single seam that closes former open items O1 (main-runtime → ACH Forwarder credential contract) and O11 (provider wiring).

**Invariant — the Agent never writes raw engine/provider environment variables.** The `CapabilityProfile` supplies access values (the `ach` coordinate + `ek_`, or `envFrom`); the `Agent` supplies the choice (`model`/`provider`); the harness materializes the concrete engine configuration.

**Demarcation — the spec defines the env-var contract; the harness owns translation and routing.** We specify *which env vars exist and what they mean*, plus the security invariants. How the harness uses them to build the engine client, or route model vs MCP vs A2A, is the harness's implementation and is intentionally **not** specified. Different harnesses may do it differently.

### Env-var contract (what the harness receives)

For a **governed** profile (`ach`), the operator materializes:

```text
ACH_BASE_URL       = CapabilityProfile.ach.endpoint
ACH_API_KEY        = the ek_ (from CapabilityProfile.ach.secretRef)
ACH_PROVIDER_TYPE  = Agent.model.provider        # absent ⇒ openai
```

For a **non-governed** profile, the engine env comes from the profile's `envFrom` (e.g. `OPENAI_BASE_URL`, `OPENAI_API_KEY`); `PROVIDER_TYPE` is still emitted from `Agent.model.provider`.

| Profile | Operator/harness materializes |
|---|---|
| governed (`ach`) | `ACH_BASE_URL` + `ACH_API_KEY` + `ACH_PROVIDER_TYPE`; builds the `provider` client against the Forwarder. ACH env is layered **after** `envFrom` and wins on collision (§11.4) |
| non-governed (`envFrom`) | the profile's `envFrom` as-is, plus `PROVIDER_TYPE`; builds the `provider` client against whatever the env points at. If the harness does not implement `provider`, the workload does not become ready (cause in logs, §22.1/O12) |

### Security invariants (normative, not harness-discretionary)

- **`ACH_API_KEY` is the `ek_`, presented as a bearer credential** to the Forwarder. The harness must not invent a different representation.
- **Rotation is by restart on secret-hash change** (§11.6) — no hot reload in v1.
- **No leak:** the harness must never write `ACH_API_KEY` / the `ek_` to logs, nor forward it downstream to MCP/A2A tool backends.
- **envFrom precedence:** with `ach`, reserved `ACH_*` env are materialized after `envFrom`; colliding keys are overwritten, not honored (§11.4).

MCP/A2A access also routes through the Forwarder with the `ek_`; if a separate base is needed it follows the same env-var pattern (e.g. `ACH_MCP_BASE_URL`). The representation is part of the env-var contract; the routing is the harness's.

---

# Part VIII — Channels, Sessions and Concurrency

## 14. Channels

A channel is an inline activation and delivery binding inside `Agent.spec.channels[]`. It is **behavior** (consumer-owned), so it lives on the `Agent`, including its secrets — a channel/account is normally 1:1 with the agent (its own webhook/bot), not a shared access credential, so inlining is consistent with the RBAC-boundary principle (§4). If a channel/account becomes shared across agents, promote it to a CRD (O4).

The v1 channel layer is modeled on **NousResearch Hermes** (`gateway/platforms/`, MIT), the way the engine layer is modeled on opencode. The adapter contract, the generic-webhook-with-routes shape, idempotency, and dual delivery below are all contrasted against that code.

### 14.1 Adapter contract

Every channel type implements the same adapter surface (Hermes `BasePlatformAdapter`, `base.py`):

```text
connect / disconnect           # bring the transport up/down
handle_message(MessageEvent)   # inbound: normalize → seam (§15)
send / edit_message            # outbound reply (edit_message = live update / streaming)
send_typing / send_image / …   # optional richer delivery
get_chat_info                  # platform metadata
```

A channel owns: inbound auth/signature validation; event normalization to `MessageEvent` (§14.2); session-key derivation; idempotency-key extraction; its delivery/write-back; its channel-specific secrets; and its `concurrency`/`expire` QoS (§18.2). A channel is not a CRD in v1.

### 14.2 `MessageEvent` — the canonical inbound envelope

The adapter normalizes any platform payload into one envelope that crosses the seam (Hermes `base.py` `MessageEvent`):

```text
text                       # the normalized prompt/content
messageType                # text | command | system | …
source                     # identity: platform, chatId, userId, threadId (session-key inputs)
mediaUrls / mediaTypes     # attachments — references, NOT inline bytes
replyToId / replyToText    # threading context
channelPrompt / channelContext   # the channel's ephemeral per-event prompt / context (§12.3 layer 5b)
internal                   # agent-originated vs external
timestamp
```

Two fields are deliberately constrained for the future seam upgrade (O7): the platform-specific raw payload is **not** part of the cross-boundary contract (it does not serialize cleanly), and `mediaUrls` are **references** (object-store/URL), never local paths — so the same envelope works if the channel↔agent seam ever becomes a network transport.

### 14.3 Channel types in v1

The `type` encapsulates transport + normalization + write-back + sync/async trait. Five types in v1; more (signal, matrix, discord, …) are added without core changes (Hermes proves this).

| type | transport | trait | write-back |
|---|---|---|---|
| `webhook` | inbound HTTP POST per route | async | per-route `deliver` (incl. platform comment) |
| `slack` | slack-bolt Socket Mode | async, **interactive** | `send`/`edit_message` (live) |
| `telegram` | long-polling (`getUpdates`) or webhook mode | async, **interactive** | `send`/`edit_message` (live) |
| `a2a` | LiteLLM A2A inbound (HTTP) | sync | A2A result |
| `cron` | internal scheduler (no platform) | async, no reply | none |

The `http`/manual/synchronous trigger is the `webhook` type used synchronously (the caller holds the connection for the reply); GitLab/GitHub/JIRA/Stripe are **routes** of the `webhook` type, not separate types (§14.4).

**One channel per event type (the routing model).** When a single platform emits several event kinds that need different handling — e.g. GitLab MR notes vs pushes vs issues, each wanting a different `prompt` and possibly a different `session` derivation — model each as **its own `webhook` channel**, not as a `routes[]` array inside one channel. This mirrors how cron multi-task is multiple cron channels: a channel is the unit of activation + its prompt + its session/response policy. `ackbot-process` (the first-bake target) maps its issues / MR-review / push handlers to three `webhook` channels.

Every channel carries common fields:

```yaml
channels:
  - name: gitlab-mr
    type: webhook
    enabled: true              # OPTIONAL, default true (see §14.7)
    concurrency: 4             # per-channel cap; default 1 (§18.2)
    expire: 120                # default 120; 0 = never (interactive) (§18.2)
    session: { mode: auto, continuity: durable, ttlSeconds: 604800 }
    response: { mode: actionRequired, fallback: fail }
    # …type-specific block below…
```

### 14.4 `webhook` (generic)

A single generic adapter (Hermes `webhook.py`) handles GitHub, GitLab, JIRA, Stripe and any HMAC-signed POST. The platform knowledge lives only in the **delivery** (`deliver.type`), not in the income, which is a generic payload→prompt template.

```yaml
    webhook:
      auth:
        type: hmac                       # hmac | bearer | none
        secretRef: { name: gitlab-webhook, key: secret }
      prompt: |                          # ephemeral per-event prompt; templated with the inbound JSON (§12.3 layer 5b)
        Review this merge request: {{ .object_attributes.url }}
      deliver:                           # write-back; platform specifics live here
        type: gitlab_comment             # gitlab_comment | github_comment | reply | <platform> | log
        config:
          tokenSecretRef: { name: gitlab-token, key: token }
      deliverOnly: false                 # true = skip the agent; the rendered prompt IS the delivery
```

- `auth.type: hmac` validates the signature (GitLab/GitHub style); `bearer` checks a token; `none` is rejected on a public bind unless explicitly acknowledged.
- `prompt` is the channel's **ephemeral per-event prompt** (§12.3 layer 5b) — the single, unified per-event prompt field shared by all channel types. For `webhook` it may use `{{ … }}` to template the inbound payload; for `cron` it is static; either way it is the same `prompt` field. (This is the field that closes O13.)
- `deliver.type: reply` answers on the triggering connection (synchronous use); `gitlab_comment`/`github_comment`/`<platform>` write back out-of-band (§14.6, dual delivery). `log` just records.
- `deliverOnly: true` skips the model entirely — the rendered `prompt` is delivered directly (push notifications where speed matters over reasoning).
- `gitlab` is therefore `type: webhook` + `deliver.type: gitlab_comment`. There is no `gitlab` type.

### 14.5 `slack`, `telegram` (interactive messaging)

These are not HTTP webhooks — they own a persistent transport:

```yaml
    # slack
    slack:
      botTokenSecretRef:  { name: slack-bot, key: xoxb }   # SLACK_BOT_TOKEN (API calls)
      appTokenSecretRef:  { name: slack-app, key: xapp }   # SLACK_APP_TOKEN (Socket Mode)
      mentionGated: true                                    # in channels, require @mention
    # telegram
    telegram:
      botTokenSecretRef:  { name: telegram-bot, key: token }
      mode: polling                                         # polling | webhook
```

- Slack uses **Socket Mode** (`slack-bolt`): an outbound websocket, so no inbound endpoint/Service is needed for Slack, and it self-heals on websocket drop. It has its own redelivery dedup.
- Telegram uses **long-polling** (`getUpdates`) by default, or webhook mode.
- Both are **interactive**: `edit_message` gives live/streaming updates, and `expire: 0` (wait indefinitely) is the natural choice — the user is waiting. Their source trait is sync-ish for `expire` purposes (the user expects a live answer), so set `expire: 0` and `delivery.streaming: true` where supported.

### 14.6 `a2a` (LiteLLM A2A inbound)

The agent is exposed as an A2A endpoint so other agents/systems can invoke it through LiteLLM (`https://docs.litellm.ai/docs/a2a`).

```yaml
    a2a:
      auth:
        header: x-a2a-custom-api-key     # the header the caller/LiteLLM sends
        secretRef: { name: a2a-inbound-key, key: key }
```

- **v1 = receiver only.** The channel exposes the A2A endpoint on the agent's existing internal Service (§16); the adapter validates the configured header against the secret. Registering the agent in LiteLLM is **out-of-band** (done by whoever runs LiteLLM / the platform), not by the operator — the operator gets no new write role in LiteLLM.
- A2A **outbound** (this agent calling others) is unrelated to this channel; it is access, governed via the `CapabilityProfile.ach` Forwarder (§11). LiteLLM is the single in/out point for A2A, symmetric with models.

### 14.7 `enabled` (optional, default true)

`enabled` is optional and defaults to `true` — a defined channel is active, so the common case writes nothing. The field exists only for the GitOps toggle: flipping `enabled: false` disables a channel **without losing its config** (useful for heavy-credential channels like Slack/Telegram, and for auditable enable/disable PRs in a cluster-per-customer GitOps flow).

Semantics of `enabled: false`: the channel is validated structurally but **not materialized** — it does not listen, does not reserve an endpoint, does not require its secrets to exist at runtime, and does not count toward "at least one enabled channel required" (§34).

## 15. Channel topology

In v1, channel gateway, router, session manager, harness and action adapter are logical responsibilities collapsed into the generated single-replica runtime process — which is exactly stock Hermes's shape (gateway + agent in one process; the seam is the in-process `set_message_handler` callback, `base.py`). The ACH Agent Runtime sets that handler to the opencode/ACH harness.

```text
external event
  → channel adapter (transport + auth + normalize → MessageEvent)
  → derive session key
  → enter session lane (serialize per key, §18.3)
  → build invocation envelope
  → harness/model call
  → validate actions
  → action adapter (write-back / delivery)
  → external platform
```

The model does not interact with channels directly. The model receives event payload, session metadata, available actions and the response contract; it returns validated structured actions, or no actions. The adapter executes accepted actions.

The channel↔harness boundary is a **named seam with a pluggable transport**: default in-process (v1), upgradeable to a network/queue transport later (O7) without changing the channel layer — which is why `MessageEvent` keeps raw payloads out of the contract and media as references (§14.2), and delivery uses the out-of-band path (§14.6). v1 does not implement the network transport.

## 16. Inbound HTTP exposure

If any enabled channel requires **inbound HTTP** delivery (`webhook`, `a2a`, or synchronous `http` use), the operator creates an internal Kubernetes `Service` for the generated workload. Channels that own an **outbound** transport do not need a Service: Slack (Socket Mode websocket) and Telegram (long-polling) connect outward, and cron is internal — an agent with only those does not get a Service.

The operator does not create the following in v1: Ingress, Gateway API `HTTPRoute`, DNS records, external load balancers, external webhook registrations, Slack/GitLab/Telegram app registration, or LiteLLM A2A registration. Those are platform responsibilities.

**Platform webhook fan-out (one channel = one URL).** Each channel listens on its own `/channels/{channelName}/events`, and the v1 model is **one `webhook` channel per event type** (no `routes[]`, §14.4). Most platforms (GitLab, GitHub) POST **all** selected event types to a single configured webhook URL — so the consumer must register **N separate platform webhook entries**, one pointed at each channel's URL and filtered to that channel's event type (e.g. one GitLab webhook for "Merge request events" → `…/channels/gitlab-mr-review/events`, a second for "Push events" → `…/channels/gitlab-push/events`). Checking multiple event boxes on a *single* webhook pointed at *one* channel URL will deliver the unmatched types to a channel that does not expect them. This per-channel-webhook configuration is the operational cost of rejecting HTTP-layer `routes[]` demux, and is a platform-side setup step, not something the operator automates in v1.

### 16.1 Stable internal runtime endpoints

```text
POST /channels/{channelName}/events
GET  /healthz
GET  /readyz
GET  /metrics
```

Example:

```text
POST http://agent-gitlab-ackstorm.engineering.svc:8080/channels/gitlab/events
```

The channel adapter behind `/channels/{channelName}/events` owns signature validation, token validation, event normalization, idempotency-key extraction (per channel type, §18.4.0) and channel-specific error mapping.

`readyz` follows §8.5: it reports Ready when channel adapters are listening (hydration is already guaranteed by the init gate). Once Ready, events are accepted and buffered in their session lane while the engine warms up. The Service is only created when required by enabled channels; cron-only agents do not require a Service unless another enabled channel requires inbound HTTP.

## 17. Session modes

```yaml
session:
  mode: none | auto | custom
```

| Mode | Meaning | Example |
|---|---|---|
| `none` | Every event is a fresh execution. No resume. | stateless cron, one-shot GitLab review |
| `auto` | Adapter derives a session key from the natural external object. | Slack thread, GitLab MR, Telegram chat |
| `custom` | User provides an explicit stable session key. | cron with continuity, named recurring task |

### 17.1 Defaults

```text
non-cron channels: auto
cron channels:     none
```

Rules:

- If `session.mode` is omitted for a non-cron channel, it defaults to `auto`.
- If `session.mode` is omitted for a cron channel, it defaults to `none`.
- `custom` requires `session.key`.
- `auto` on cron is invalid in v1, because cron has no natural external conversation object.

### 17.2 Cron with continuity

A cron channel may opt into continuity with `custom`.

```yaml
channels:
  - name: daily-review
    type: cron
    cron:
      schedule: "0 8 * * 1-5"
      timezone: Europe/Madrid
    session:
      mode: custom
      key: cron:daily-review
      continuity: durable
      ttlSeconds: 2592000
```

### 17.3 Session key mapping

The runtime maintains a mapping:

```text
(namespace, agentName, channelName, sessionKey) -> runtimeSessionId
```

A session is runtime data, not a Kubernetes object.

For a local engine such as opencode, durable resume requires that the engine session directory survive pod restart. A runtime with `persistence.enabled: true` (volume mounted at `mountPath`, `sessionDir` under it) is the v1 mechanism for local durable engine state.

### 17.4 Session continuity

`session.continuity` declares author intent for cross-restart durability. It controls a warning, never a hard failure, because single-replica guarantees a single writer.

```yaml
session:
  mode: auto
  continuity: durable     # durable | bestEffort  (default: durable when mode != none)
  ttlSeconds: 604800
```

- `durable` (default when `mode != none`): the author expects sessions to survive restart. If the resolved runtime has `persistence.enabled: false`, reconcile emits a `BestEffortSessionContinuity` **warning** — it does not fail. Continuity is best-effort (sessions lost on restart), not corrupt.
- `bestEffort`: the author accepts ephemeral sessions; no warning.
- `mode: none` ignores `continuity`.

## 18. Concurrency and serialization

The runtime enforces three layers that bound and order work — a global resource ceiling, per-channel QoS caps, and per-session serialization — plus a per-invocation watchdog that bounds how long a single run may execute.

### 18.1 Global ceiling (resources)

`Agent.spec.execution.limits.maxConcurrentInvocations` is the only hard concurrency limit: the maximum number of concurrent engine invocations (e.g. opencode processes) in the pod. Always enforced. **Default `1`** (conservative for first bake — a single engine at a time cannot overrun the pod).

```yaml
execution:
  limits:
    maxConcurrentInvocations: 8        # default 1
    maxInvocationSeconds: 1800         # hard per-run cap; see §18.7
    idempotencyWindowSeconds: 3600     # dedup window; default 3600; see §18.4
```

### 18.2 Per-channel cap (QoS)

Each channel declares `concurrency` and `expire`.

```yaml
channels:
  - name: gitlab-mr
    concurrency: 5     # max concurrent invocations this channel MAY use — a cap, not a reservation; default 1
    expire: 120        # max seconds an event waits for a slot or its session lane; default 120; 0 = never
```

- `concurrency` **default `1`** (coherent with the global default of `1`). It is a **cap, not a reservation**. Channels are **overcommitted** by design: `sum(channel.concurrency)` MAY exceed `maxConcurrentInvocations`. A channel uses up to its cap only when it has traffic; idle channels reserve nothing.
- **Consequence (accepted):** under overcommit there is no guaranteed reservation, so a hot channel can consume the global budget and starve a quiet one. This is the correct v1 trade — simplicity plus maximum utilization. An optional per-channel `minReserved` (the Kubernetes `requests` analogue) is a future escape hatch if starvation is observed in practice. YAGNI in v1.
- `expire` bounds how long an event waits for a global/channel slot OR for its session lane. **Default `120`.** `0` = never expire, **reserved for interactive channels** (e.g. a TUI or synchronous caller that wants to wait indefinitely).
- **The defaults are a safe floor, not tuned for long runs.** With `concurrency: 1` and minutes-long runs (code review), a follow-up event waits behind the first run and is bounced once `expire` elapses; `expire: 120` survives one source-retry window better than a smaller value, but the real fix for such workloads is to **raise `concurrency`** (each slot is an engine process, so the default stays `1` to protect small pods). Long-running channels **must** raise `concurrency`/`expire`; published runtime profiles for review-type workloads should ship appropriate caps. The conservative defaults protect the unknown workload; they are not the right values for the code-review headline case (see §10.1).
- **`expire` exhaustion is never silent** (§18.4.1).

### 18.3 Per-session serialization (correctness)

At most one invocation runs per `(namespace, agentName, channelName, sessionKey)` at a time. Concurrent events for the same session key form a FIFO lane and run in order, each resuming the state the previous left. This is implicit and not configurable. It prevents two events on the same conversation (e.g. two notes on the same MR) from racing the same engine session and corrupting state.

Composition:

```text
event arrives → resolve sessionKey
  ├─ session already running   → enqueue in that key's lane (does NOT hold a slot; waits up to `expire`)
  ├─ session idle + slot free   → acquire channel slot AND global slot, run
  └─ session idle + no slot     → wait for a slot up to `expire`, then run
```

A lane-queued event does **not** hold a global/channel slot, so a hot session cannot exhaust the channel.

The in-process lane is correct **only because** single-replica (§8.1) guarantees a single writer. Externalizing the lane is the same work as an external session store and is the seam to multi-replica and on-demand execution (O7).

### 18.4 Idempotency

The channel adapter extracts an idempotency key (default: `event.id`, §16.1). Sources re-deliver (e.g. GitLab redelivers failed webhooks) and a retry must not run the invocation twice.

**Dedup is router-level, not lane-level, and independent of `session.mode`.** Deduplication happens at the **router**, *before* session-mode resolution and the lane (§29) — you must be able to discard a duplicate before resolving any session, and a `mode: none` channel has no persistent lane to hold the key. There is **one dedup key-set per agent**, keyed by `{channelName}:{event.id}` (channel-prefixed so events from different channels cannot collide), covering **all channels including `mode: none`**.

This separates two durabilities the spec previously conflated:

- **Session continuity** ("do I remember the conversation between events?") depends on `session.mode` — `mode: none` means a fresh session each event.
- **Event dedup** ("have I already processed this `event.id`?") does **not** depend on `session.mode` — it is a property of event ingress, not of conversation. `mode: none` disables continuity, **not** dedup.

This resolves the canonical case: the GitLab one-shot review is `mode: none` (each MR is a fresh session, no history), yet a GitLab webhook redelivery is still discarded by the router via the channel-prefixed `event.id` — because dedup lives above the (non-existent) session lane.

#### 18.4.0 Idempotency-key derivation (per channel type)

`{channel}:{event.id}` is only as good as `event.id`. The adapter MUST derive an `event.id` that is **unique per distinct event**. The governing invariant (the failure this prevents is the broad-key dedup bug — a key derived too widely, e.g. per-project, collapses distinct events and silently drops real work):

> **When a unique-per-distinct-event id cannot be derived, the adapter degrades to a unique-per-arrival id (so the event is *processed*), never to an empty or shared key (which would *drop* distinct events). The safe failure mode is "fail to dedup", never "over-dedup".**

Per channel type (webhook/http follow Hermes `webhook.py` `delivery_id`, whose TTL cache is `3600s` — the same as this spec's default window):

| channel | `event.id` derivation | no-id fallback |
|---|---|---|
| `webhook` | first present of `X-GitHub-Delivery`, `X-Gitlab-Event-UUID`, `svix-id` (Stripe), `X-Request-ID` | **millisecond timestamp** (unique-per-arrival → no effective dedup, never a shared key) |
| `http` (synchronous) | caller's `Idempotency-Key` or `X-Request-ID` header if sent | millisecond timestamp → no dedup (the caller holds the connection; retrying is the caller's choice) |
| `slack` | the message `ts` (Slack's unique per-message timestamp), scoped by team+channel | n/a (always present) |
| `telegram` | `update_id` (monotonic, unique per update) | n/a (always present) |
| `cron` | `{channelName}:{scheduled_tick_time}` — the **scheduled** instant, not `now()` | n/a (a tick is idempotent with itself, distinct across ticks) |

No new CRD field is required: synchronous callers opt into dedup by sending a standard `Idempotency-Key`/`X-Request-ID`; everything else uses the platform-native id.

### 18.4 Idempotency

The channel adapter extracts an idempotency key per §18.4.0. Sources re-deliver (e.g. GitLab redelivers failed webhooks) and a retry must not run the invocation twice.

**Dedup is router-level, not lane-level, and independent of `session.mode`.** Deduplication happens at the **router**, *before* session-mode resolution, backpressure admission, and the lane (the pre-lane order is pinned in §18.8/§29) — you must be able to discard a duplicate before resolving any session, and a `mode: none` channel has no persistent lane to hold the key. There is **one dedup key-set per agent**, keyed by `{channelName}:{event.id}` (channel-prefixed so events from different channels cannot collide), covering **all channels including `mode: none`**.

This separates two durabilities the spec previously conflated:

- **Session continuity** ("do I remember the conversation between events?") depends on `session.mode` — `mode: none` means a fresh session each event.
- **Event dedup** ("have I already processed this `event.id`?") does **not** depend on `session.mode` — it is a property of event ingress, not of conversation. `mode: none` disables continuity, **not** dedup.

**Window — `execution.limits.idempotencyWindowSeconds`, default `3600` (1h).** A key is remembered for this long; a redelivery within the window is dropped as a duplicate. One hour covers typical webhook redelivery backoff without the key-set growing unbounded; configurable per backoff profile. (Matches Hermes `webhook.py`'s `_idempotency_ttl`.)

**Durability inherits `persistence.enabled`** (for the whole agent key-set, all channels):

- `persistence.enabled: true` → the key-set is persisted under the PVC and survives restart/crash (covers the most likely double-delivery: pod crashed → source redelivers minutes later → durable key-set prevents the re-run). Full window effective.
- `persistence.enabled: false` → in-process key-set only; effective window is `min(idempotencyWindowSeconds, pod uptime)` — a restart resets it, so redelivery-after-restart can double-fire (declared loss mode, §30.1). This applies to `mode: none` channels too: without disk, the router key-set is lost on restart.

(A fully durable cross-replica dedup is the same external substrate as O7.)

#### 18.4.1 `expire` exhaustion is never silent

When an event's `expire` elapses (waiting for a slot or behind a slow run in its session lane), the outcome derives from the channel's source trait — it is never a silent drop:

| Source trait | On `expire` |
|---|---|
| synchronous (http, `/v1/responses`) | return `503` / explicit timeout to the caller, who retries or surfaces it |
| asynchronous, source retries (gitlab webhook, …) | **NACK / non-2xx so the source redelivers later** — the event is deferred, not lost |
| asynchronous, no retry (cron) | drop **with a log/metric** (the next tick will come) |

Silently dropping a user's follow-up (e.g. a second comment on a busy MR queued behind a slow first review) is incorrect for a reviewer; the NACK-for-redelivery path exists precisely to avoid it.

### 18.5 Cron concurrency

Cron is single-scheduler by construction (single replica, §8.1): no double-firing, no leader election in v1. A cron run overrunning its interval is bounded by the same per-channel `concurrency`/lane rules and `expire`. A future `concurrencyPolicy` (`Allow | Forbid | Replace`) MAY be added if overrun handling needs CronJob-style semantics; not in v1.

### 18.6 Concurrency validation and defaults

- `maxConcurrentInvocations`: default `1`; must be `>= 1`.
- `maxInvocationSeconds`: **default `1800`; always enforced**; must be `>= 1`. There is no "unset / no cap" state — the cap is the deadlock-prevention guarantee (§18.7) and cannot be disabled by omission.
- `maxQueuedTotal`: **default `100`; always enforced**; must be `>= 1`. Caps buffered events per pod (§18.8).
- `idempotencyWindowSeconds`: default `3600`; must be `>= 1` if set.
- per channel: `concurrency` default `1`, must be `>= 1`; `expire` default `120`, must be `>= 0` (`0` = never, for interactive channels).
- **No sum rule** — overcommit is allowed.
- Warning if a channel's `concurrency > maxConcurrentInvocations` (the global caps it first; the value is misleading).

### 18.7 Invocation watchdog (availability)

`expire` bounds *waiting*; `maxInvocationSeconds` bounds *running*. Without it, a hung engine invocation permanently holds a global slot **and** its channel slot **and** blocks its session lane forever; enough hung runs deadlock the single-replica agent. It is therefore **always enforced with a finite default (`1800`)** — being finite is the property that prevents the deadlock; the exact value is secondary and tunable.

```yaml
execution:
  limits:
    maxInvocationSeconds: 1800     # default; hard cap on a single run
```

- On overrun the harness **kills the run** and releases the global slot, the channel slot and the session lane; the invocation is recorded as failed (timeout) in audit.
- It composes with the drain (§8.6): on `SIGTERM`, in-flight runs get at most `min(remaining grace period, maxInvocationSeconds)` before being killed — the grace period never waits unbounded on a run. (Size `terminationGracePeriodSeconds` to the typical invocation duration, §8.6; otherwise drain kills runs at the grace period, not at the cap.)
- A per-channel `maxInvocationSeconds` override (a large MR review wants more than a cron tick) is a future extension; the global cap is required in v1.

### 18.8 Queue-depth backpressure (availability)

`maxInvocationSeconds` bounds run time; `maxQueuedTotal` bounds **queue depth**. Without it, the blessed `expire: 0` (never expire, for interactive channels) plus the default single global slot plus a slow engine lets the in-process lane (and warmup-buffered events) grow unbounded until OOM — in a config the spec explicitly permits. So it too is always enforced with a finite default.

```yaml
execution:
  limits:
    maxQueuedTotal: 100     # default; max buffered events across all lanes in the pod
```

- When the pod's buffered count is at `maxQueuedTotal`, a newly arrived event is **not buffered**; it is treated as immediate `expire` exhaustion with the **non-silent** semantics of §18.4.1 (`503` to synchronous sources, NACK-for-redelivery to async-retriable, drop-with-log to async-no-retry).
- This is **compatible with `expire: 0`:** `expire: 0` means "do not expire *while waiting* for a slot", and the cap acts at **admission to the queue**, not on the wait. An accepted event with `expire: 0` still waits indefinitely; a new event is simply refused entry when the pod is full.
- Together, `maxConcurrentInvocations` (concurrency), `maxInvocationSeconds` (run time) and `maxQueuedTotal` (queue depth) are the three finite resource bounds a single-replica agent needs.

**Pre-lane order is pinned: dedup → backpressure admission → lane.** Deduplication (§18.4) runs **before** the `maxQueuedTotal` admission check. A duplicate is a cheap discard that must not consume a queue slot — otherwise a flood of redeliveries would fill the queue, NACK legitimate traffic, the sources would redeliver the NACK'd events, and the loop would sustain itself. Discarding duplicates first means the queue only ever fills with distinct events.

**Queue-starvation is an accepted trade (symmetric with slot-starvation, §18.2).** `maxQueuedTotal` is a **global pod cap** while the lane is per-session-key, so one hot session with a large backlog can consume the global queue and refuse admission to other channels — i.e. a hot session can starve the pod's *ingress*, not only its slots (§18.2 named the slot version of this). This is the same v1 trade — simplicity over fairness — and is accepted for the same reason. The future escape hatch is a per-channel / per-lane depth cap (the queue analogue of `minReserved`), added only if ingress starvation is observed in practice. YAGNI in v1.

---

# Part IX — Response and Action Contract

## 19. Response modes

```yaml
response:
  mode: actionRequired | automatic | disabled
  fallback: fail | silent
```

| Mode | Meaning |
|---|---|
| `actionRequired` | Visible output only through structured, validated actions. Default and required when governed (`CapabilityProfile.ach`). |
| `automatic` | Harness may synthesize a `channel_message` from final text. Compatibility mode only. |
| `disabled` | No visible reply to the origin. Side effects may still occur if allowed. |

Rules:

- a governed `CapabilityProfile` (`ach` present) requires `response.mode: actionRequired` for visible output.
- `delivery.streaming: true` is valid only with `response.mode: automatic`.
- In v1, governed agents should default to `actionRequired`.
- In first bake, runtimes MAY implement only `reply` actions and reject `sideEffect` actions even if declared.

## 20. Action taxonomy

Every action has a `kind`.

```text
reply       # visible response through the originating channel
sideEffect  # external state change through a channel/platform API
```

### 20.1 `reply` actions

A `reply` action is the natural response to the triggering event: respond in a Slack thread, comment on the triggering GitLab MR, reply to a Telegram chat, or return a response in an HTTP/manual channel.

```yaml
responseActions:
  - name: channel_message
    kind: reply
    inputSchema:
      type: object
      required: ["text"]
      properties:
        text:
          type: string
```

**Dual delivery (from Hermes, §5 of the channel brief).** There are two outbound paths, and both are first-class:

1. **Synchronous reply** — the natural answer to the trigger, delivered on/through the originating channel (the `reply` action above; for a synchronous `webhook`/`http` channel this is the HTTP response).
2. **Out-of-band delivery** — the agent delivers to *any* channel independent of the trigger (a `send_message`-style tool, a `webhook` route's `deliver.type: <platform>`, or a cron job delivering to a platform). The agent does not have to answer on the connection it was triggered from.

Out-of-band delivery is what makes platform write-back (e.g. `gitlab_comment`) and cross-channel notifications work, and it is the path the seam keeps stable for a future network/queue transport (§15) — in a split-pod world it is how the agent would push results back. In v1 (co-located) both paths run in-process.

### 20.2 `sideEffect` actions

A `sideEffect` action changes external state beyond replying to the origin: create an issue, close a ticket, approve a merge request, assign reviewers, launch a pipeline, create a branch, make a commit, trigger PagerDuty, change incident priority, move a Jira card.

```yaml
responseActions:
  - name: create_issue
    kind: sideEffect
    consentTier: consent
    inputSchema:
      type: object
      required: ["title"]
      properties:
        title:
          type: string
        body:
          type: string
```

### 20.3 First bake side-effect behavior

In the first implementation slice:

- `reply` actions are executable.
- `sideEffect` actions are part of the API model.
- Runtimes MAY reject `sideEffect` actions until side-effect execution policy is implemented.

If a model emits an unsupported `sideEffect`, the harness should fail with a clear error:

```text
UnsupportedActionKind: sideEffect actions are declared but not executable in this runtime version
```

This avoids prematurely implementing consent, rollback, idempotency, external authorization and side-effect audit semantics.

## 21. Common `channel_message` action

One conceptual reply action is mapped by the channel adapter to the transport-specific API.

```json
{
  "name": "channel_message",
  "kind": "reply",
  "inputSchema": {
    "type": "object",
    "required": ["text"],
    "properties": {
      "text": { "type": "string" }
    }
  }
}
```

The channel adapter narrows or defaults channel-specific verbs (`send`, `reply`, `thread_reply`, `comment`, `update`) per invocation: when only one verb is valid it is collapsed (defaulted) so the model supplies only `text`. The model never receives broad platform tokens or generic channel write tools — only the effective actions for the current invocation. A non-default target is a separate, higher-permission capability.

## 22. Final output contract

In `actionRequired`, final model output must be valid JSON:

```json
{
  "actions": [
    {
      "name": "channel_message",
      "input": {
        "text": "Review complete. One follow-up opened."
      }
    }
  ]
}
```

No action:

```json
{ "actions": [] }
```

Validation pipeline:

```text
parse JSON
  → validate top-level shape
  → validate every action name against effective availableActions
  → validate each input against its JSON Schema
  → enforce response.mode for reply actions
  → enforce consentTier and side-effect support for sideEffect actions
  → repair prompt, max 2 attempts
  → apply fallback
  → execute only validated actions through adapter boundary
```

The harness is the sole enforcement point. Channel adapters execute only accepted actions.

### 22.1 API-modeled / not-yet-executable

Some fields are part of the API model — valid at admission, structurally meaningful, forward-compatible — but a given runtime version may not execute them yet. This is a deliberate, recurring pattern, not an oversight, and it appears in several places:

- `sideEffect` actions (§20.3): declared, schema-validated, but a first-bake runtime may reject them with `UnsupportedActionKind`.
- additive `CapabilityProfile.sources[]` alongside `ach` (§11.3): modeled, but v1 may reject non-empty sources in this combination.

Rules for any feature in this category:

- it is accepted at admission (the shape is valid) and never silently ignored;
- a runtime that does not implement it MUST reject it at execution with a clear, specific reason (e.g. `UnsupportedActionKind`, `UnsupportedHybridHydration`), never a cryptic engine error;
- the rejection is surfaced to `Agent.status` / invocation audit;
- the feature is documented as modeled-not-executable so authors are not surprised.

This keeps the API stable across versions while letting first bake stay small.

## 23. Consent tiers

For `sideEffect` actions:

```text
auto     # may execute if emitted and policy allows
consent  # requires explicit human request in the triggering event
```

`response.mode` gates visible replies. `consentTier` gates side effects. They are independent. Consent semantics are not required for first bake if `sideEffect` execution is rejected by the runtime.

---

# Part X — Runtime Behavior

## 25. Generated Kubernetes workload

For `execution.type: kubernetes`, the operator generates:

- a single-replica workload (operator-selected primitive; recommended `Deployment` `replicas:1` + `strategy: Recreate`);
- Service if inbound channels require HTTP/webhook access;
- ConfigMaps for rendered runtime configuration where appropriate;
- init container for hydration;
- volumes for workspace and optional durable state (PVC when `persistence.enabled`);
- pod labels and owner references;
- `preStop` hook and sized `terminationGracePeriodSeconds` for graceful drain;
- status conditions on the `Agent`.

The generated workload is based on:

```text
AgentRuntime.spec.engine
AgentRuntime.spec.execution
AgentRuntime.spec.execution.kubernetes.podTemplate
AgentRuntime.spec.health
CapabilityProfile.spec (ach / sources / envFrom)
MemoryProfile.spec, if referenced
Agent.spec.execution.limits
Agent.spec.prompt
Agent.spec.channels
Agent.spec.model
Agent.spec.memory
Agent.spec.redaction
```

## 26. Generated resource naming

For each `Agent`, the operator creates resources in the same namespace as the `Agent`.

Given:

```yaml
metadata:
  name: gitlab-ackstorm
  namespace: engineering
```

Default generated names:

```text
<operator-selected workload>/agent-gitlab-ackstorm   # single replica
Service/agent-gitlab-ackstorm                         # only when inbound HTTP is required
ConfigMap/agent-gitlab-ackstorm-runtime               # rendered runtime config, when needed
ConfigMap/agent-gitlab-ackstorm-channels              # rendered channel config, when needed
PersistentVolumeClaim/agent-gitlab-ackstorm           # only when persistence.enabled
```

Rules:

- Generated resources are namespace-local.
- Generated resources have owner references to the `Agent` where Kubernetes allows it.
- Generated names are deterministic.
- Generated resources include stable labels for selection, metrics and troubleshooting.

Recommended labels:

```yaml
app.kubernetes.io/name: ach-agent-runtime
app.kubernetes.io/managed-by: ach-agent-runtime-operator
runtime.ackstorm.ai/agent: gitlab-ackstorm
runtime.ackstorm.ai/runtime: opencode-persistent-small
```

## 27. Runtime process responsibilities

In v1 these are collapsed into one single-replica runtime process.

| Responsibility | Description |
|---|---|
| Channel adapters | Receive events, validate signatures, normalize payloads, extract idempotency keys, execute channel actions |
| Cron scheduler | Emits cron events for cron channels (single scheduler, single replica) |
| Session lanes | Serialize one invocation per session key (FIFO); deduplicate by idempotency key |
| Invocation router | Acquires global/channel slots, creates invocation envelopes, dispatches to harness |
| Session manager | Maps external session keys to runtime session IDs |
| Memory adapter | Connects to configured memory backend through `MemoryProfile` |
| Harness | Builds prompt stack, invokes engine/model, validates final actions |
| Action adapter boundary | Executes only validated actions |
| Lifecycle | Strict `readyz`; `SIGTERM` drain of lanes within the grace period |
| Audit/logging | Records event, session, action and failure metadata |

This may be split into separate services only if scale or operational ownership demands it in a future version (O7).

## 28. Invocation envelope

Invocation payload is runtime data, not a Kubernetes object.

```json
{
  "invocationId": "inv_01J...",
  "agent": { "namespace": "engineering", "name": "gitlab-ackstorm" },
  "runtime": { "name": "opencode-persistent-small", "engine": "opencode" },
  "channel": { "name": "gitlab-mr-review", "type": "webhook" },
  "event": {
    "id": "gitlab:project:123:mr:45:note:999",
    "source": "gitlab",
    "type": "merge_request_note",
    "conversationId": "gitlab:project:123:mr:45",
    "idempotencyKey": "gitlab:project:123:mr:45:note:999",
    "payload": {
      "projectId": 123,
      "mergeRequestIid": 45,
      "comment": "please review this MR"
    }
  },
  "availableActions": [
    {
      "name": "channel_message",
      "kind": "reply",
      "inputSchema": {
        "type": "object",
        "required": ["text"],
        "properties": { "text": { "type": "string" } }
      }
    }
  ],
  "session": { "mode": "auto", "continuity": "durable", "key": "gitlab:project:123:mr:45", "id": "opencode-session-abc123" },
  "memory": { "profile": "hindsight-code-review", "scope": "123" },
  "metadata": { "traceId": "trace_abc", "agentGeneration": 4, "routingSnapshotId": "route_01J..." }
}
```

## 29. Event flow

```text
External platform
  → channel adapter
  → event normalization
  → idempotency dedup (router-level, {channel}:{event.id} per §18.4.0; pre-session; durable ⟵ persistence.enabled, §18.4)
  → backpressure admission (maxQueuedTotal; AFTER dedup, so duplicates never consume queue, §18.8)
  → redaction
  → session mode resolution
  → session lane (serialize per session key)
  → session id resolution
  → memory context lookup, if configured
  → acquire global + channel slot
  → invocation envelope
  → prompt stack assembly
  → engine/model invocation
  → final JSON action validation
  → channel action execution
  → memory write/update, if configured
  → audit/status/metrics
```

## 30. Cron flow

Cron is a channel, not an execution mode.

```text
cron scheduler (single, single-replica)
  → emits channel event
  → session.mode none/custom
  → session lane
  → invocation envelope
  → harness
  → actions or no actions
```

Cron does not imply a Job-per-event execution model in v1.

## 30.1 Declared loss modes

v1 accepts a small set of loss modes. They are declared here honestly rather than hidden; each has a future closure path but is acceptable for v1.

| Loss mode | When | Why accepted in v1 | Future closure |
|---|---|---|---|
| Queued/buffered lane entries on hard crash | `SIGKILL`/crash before drain (§8.6) | planned restarts drain cleanly; first-warmup events stay retryable via A′ (§8.5), so this is narrowed to post-first-ready buffered work | durable lane (O7) |
| Redelivery-after-restart double-fire | `persistence.enabled: false` only; source redelivers after the dedup cache was lost on restart (§18.4) | durable when persistence is on; documented when off | durable dedup key-set (O7) |
| Missed cron ticks across a restart | a tick falls inside the no-pod window; **no misfire catch-up** | infrequent; next tick recovers steady state | a `startingDeadlineSeconds`-style catch-up window (future) |
| Shared-`AgentRuntime` rollout blast radius | editing an `AgentRuntime` referenced by N agents rolls all N at once; with no ingress HA (§8.1) that is **N simultaneous 503 windows** | platform-controlled change, infrequent | staggered/`maxUnavailable` rollout across referencing agents (future) |
| Shared-`CapabilityProfile` rotation blast radius | rotating the `ek_` changes the secret hash → rolls **every** Agent referencing that profile (§11.6); with no ingress HA, **N simultaneous 503 windows** — and more likely than the `AgentRuntime` case, since a profile (one ACH Environment) is typically shared by more agents and key rotation is routine | rotation is operator-controlled and schedulable | staggered rollout across referencing agents (future, same mechanism as the row above) |

`expire` exhaustion is **not** in this table: it is never a silent loss (§18.4.1).

---

# Part XI — Memory and Redaction

## 31. Agent memory

`Agent.spec.memory` references a `MemoryProfile` and declares the agent-specific memory intent.

```yaml
memory:
  profileRef:
    name: hindsight-code-review
  mission: "AI code reviewer focused on architecture, conventions, recurring bugs."
  scope: "{project_id}"
  mentalModels:
    - architecture
    - conventions
    - recurring-issues
```

Rules:

- `memory.profileRef.name` resolves in the same namespace as the `Agent`.
- Cross-namespace memory references are rejected.
- `memory.mission` is agent-specific.
- `memory.scope` is agent-specific and may use channel/event variables.
- `memory.mentalModels` may extend or narrow `MemoryProfile.spec.defaults.mentalModels`.
- Memory is invisible infrastructure; the agent should not mention memory mechanics in user-visible output.
- Memory backend lifecycle is external to Agent Runtime in v1; the operator configures access but does not provision the backend.
- **Memory failure is fail-open.** If the memory backend is unreachable or errors at invocation time, the harness proceeds in **degraded mode** — the invocation runs *without* memory context, and a failed write is logged but does not fail the invocation. Memory is enrichment, not a gate: a reviewer that cannot reach its history is more useful than one that refuses to respond. Both the read (context lookup) and write (memory update) sides are fail-open. There is no fail-closed option in v1; if a use case ever needs it (e.g. memory used as an access control, which is a misuse — access belongs to the `CapabilityProfile`), it would be a future opt-in.

## 32. Memory profile and agent split

```text
MemoryProfile = how memory is reached and what backend it uses
Agent.memory  = why this agent uses memory and how memory is scoped
```

## 33. Redaction

Inbound redaction is configured per Agent.

```yaml
redaction:
  enabled: true
  config:
    aws_keys: true
    credit_cards: true
    emails: false
    ssn: false
```

Rules:

- Redaction is applied after event normalization and before prompt assembly.
- Redaction is applied before memory writes.
- Redaction must not log original sensitive values.

---

# Part XII — Validation

## 34. Admission validation

The CRDs should reject invalid structure early.

### AgentRuntime

- `spec.engine.type` is required; the engine-specific block matching `engine.type` is required.
- `spec.execution.type` is required; for `execution.type: kubernetes`, `execution.kubernetes` is required.
- `execution.kubernetes.persistence.enabled` is required (bool).
- `persistence.size` is required when `persistence.enabled: true`.
- `persistence.retainPolicy` must be `Retain | Delete` if set.
- `execution.kubernetes` MUST NOT contain a workload type or replica count.
- `terminationGracePeriodSeconds`, if set, must be `>= 0`.
- `engine.<type>.startupTimeoutSeconds`, if set, must be `>= 1` (exceeding it terminates the process, §8.5).
- `health`, if set: `enabled` (bool), `host` (string), `port` (`1–65535`).
- Unknown fields should be rejected where feasible.

### CapabilityProfile

- at least one of `spec.ach` / `spec.envFrom` is required (there must be a path to a model).
- `spec.ach`, if present, requires `endpoint`, `name` and `secretRef` (`name`+`key`).
- `spec.sources[]`, if present: each item requires `source`; `secretRef` (per-source) requires `name`+`key`.
- `spec.envFrom`, if present: standard env-source shape.
- `spec.ach.provider` does not exist (provider is the Agent's choice); the profile holds no `model`/`provider`/`response`.
- Unknown fields should be rejected where feasible.

### MemoryProfile

- `spec.type` is required; the backend-specific block matching `spec.type` is required.
- For `type: hindsight`, `spec.hindsight.mcp.url` is required.
- If auth is configured, `secretRef.name` and `secretRef.key` are required.
- Unknown fields should be rejected where feasible.

### Agent

- `runtimeRef.name` and `capabilityProfileRef.name` are required; neither allows a `namespace` field (namespace-local in v1).
- `Agent` MUST NOT contain `access` or `hydration` (those live on `CapabilityProfile`); admission rejects them.
- `execution.limits.maxConcurrentInvocations`, if set, must be `>= 1`.
- `execution.limits.maxInvocationSeconds`, if set, must be `>= 1` (default `1800`, always enforced).
- `execution.limits.maxQueuedTotal`, if set, must be `>= 1` (default `100`, always enforced).
- `execution.limits.idempotencyWindowSeconds`, if set, must be `>= 1`.
- `model.default` is required; `model.provider` is optional (default `openai`), a free string, NOT value-validated.
- `prompt.source.type: inline` requires `prompt.source.inline`; `prompt.source.type: file` requires `prompt.source.file.path`.
- `prompt.compose` must be `replace | append | prepend`.
- At least one **enabled** channel is required (`enabled: false` channels do not count; §14.7).
- `channels[].type` must be `webhook | slack | telegram | a2a | cron`. The type-specific block matching `type` is required.
- `channels[].enabled`, if set, is bool (default `true`).
- per channel: `concurrency` default `1` (`>= 1`); `expire` default `120` (`>= 0`).
- `type: webhook` requires `webhook.auth.type` (`hmac | bearer | none`); `hmac`/`bearer` require `secretRef`. `deliverOnly: true` requires a real `deliver.type` (not `log`) **and** a non-empty `channel.prompt` (under `deliverOnly` the rendered prompt is the delivery; without it the payload would be empty).
- `type: slack` requires `slack.botTokenSecretRef` and `slack.appTokenSecretRef`.
- `type: telegram` requires `telegram.botTokenSecretRef`; `telegram.mode` must be `polling | webhook`.
- `type: a2a` requires `a2a.auth.header` and `a2a.auth.secretRef`.
- `session.mode` must be `none | auto | custom`; `session.mode: custom` requires `session.key`; `session.mode: auto` is invalid for cron channels.
- `session.continuity`, if set, must be `durable | bestEffort`.
- `response.mode` must be `actionRequired | automatic | disabled`.
- `delivery.streaming: true` requires `response.mode: automatic`.
- `memory.profileRef.name`, if set, resolves only in the same namespace.

## 35. Reconcile validation

Reconcile-time validation checks references and derived constraints:

- `runtimeRef` and `capabilityProfileRef` exist in the same namespace as the `Agent`.
- referenced `MemoryProfile`, if configured, exists in the same namespace.
- referenced Secrets and ConfigMaps exist in the same namespace; cross-namespace refs are rejected.
- on the resolved `CapabilityProfile`: if `ach` present, its `secretRef` (`ek_`) exists; `endpoint`/`name` are non-empty. Source `secretRef`s exist when `sources[]` present. (The `ach.endpoint` is an external coordinate — it is not resolved to an in-cluster CR.)
- **`model.provider` is not validated** — the operator cannot know which dialects the harness implements; resolved at runtime (§13.1).
- channel action schemas are valid JSON Schema; channel secrets required by enabled channels exist.
- generated workload can be rendered and has a deterministic hash for rollout triggers.
- **derived governed flag:** the operator derives "governed" from the presence of `CapabilityProfile.ach`. A governed agent requires `response.mode: actionRequired` for visible-output channels; a non-governed `automatic` channel is allowed.
- **derived-capability check:** emit `BestEffortSessionContinuity` warning when a channel has `session.mode != none` and `continuity: durable` but the resolved runtime has `persistence.enabled: false`. Warning, not failure.
- emit a warning when a channel's `concurrency > maxConcurrentInvocations`.
- **envFrom collision warning:** when `ach` is present and an `envFrom` Secret key collides with a reserved `ACH_*` var (§11.4), emit a **non-blocking warning** ("envFrom key `ACH_BASE_URL` collides with a reserved ACH var and will be overwritten; remove it"). The security behavior is unchanged — ACH still wins, still no admission error — but the otherwise-silent overwrite becomes visible. The operator already reads the Secret's keys here, so the check is free.
- **(v1 defer)** non-empty `CapabilityProfile.sources[]` alongside `ach` MAY be rejected (§11.3, §22.1).

Example status:

```yaml
status:
  conditions:
    - type: Ready
      status: "False"
      reason: InvalidChannelContract
      message: "channel gitlab: action channel_message has no valid inputSchema"
```

---

# Part XIII — Status

## 36. Agent status

The `Agent` status reports only what the **operator can observe from the control plane** — references it resolved, resources it generated, init-container outcome, and workload readiness. The runtime process never self-reports to `Agent.status` (no patch RBAC for the runtime, no sidecar). Failures that only the harness/engine can know (unknown provider dialect, a prompt file the engine cannot find, a model that does not answer) surface as `WorkloadReady: False` plus detail in the pod logs — not as fabricated conditions (see O12).

Example healthy status:

```yaml
status:
  observedGeneration: 4
  runtimeRef:
    name: opencode-persistent-small
  capabilityProfileRef:
    name: engineering-prod-ach
  memoryProfileRef:
    name: hindsight-code-review
  workload:
    kind: Deployment            # operator-selected; single replica
    name: agent-gitlab-ackstorm
    readyReplicas: 1
    desiredReplicas: 1
  persistence:
    enabled: true
    pvc: agent-gitlab-ackstorm
  service:
    name: agent-gitlab-ackstorm
    port: 8080
  hydration:
    lastAttemptTime: "2026-06-18T08:00:00Z"
    lastSuccessTime: "2026-06-18T08:00:10Z"
  conditions:
    - { type: RuntimeResolved, status: "True", reason: RuntimeFound }
    - { type: CapabilityResolved, status: "True", reason: CapabilityProfileFound }
    - { type: MemoryResolved, status: "True", reason: MemoryProfileFound }
    - { type: Hydrated, status: "True", reason: InitContainerSucceeded }
    - { type: WorkloadReady, status: "True", reason: WorkloadReady }
    - { type: ServiceReady, status: "True", reason: ServiceCreated }
    - { type: Ready, status: "True", reason: Ready }
```

Example invalid runtime:

```yaml
status:
  conditions:
    - type: Ready
      status: "False"
      reason: RuntimeNotFound
      message: "AgentRuntime opencode-persistent-small was not found in namespace engineering"
```

Example best-effort-continuity warning:

```yaml
status:
  conditions:
    - type: SessionContinuityWarning
      status: "True"
      reason: BestEffortSessionContinuity
      message: "channel slack uses session.mode=auto continuity=durable but runtime persistence is disabled; sessions will not survive restart"
```

## 37. Recommended conditions

All conditions below are **control-plane observable** — the operator computes them from resolved references and from `pod.status` (init-container result, readiness). None is self-reported by the runtime process.

| Condition | Meaning |
|---|---|
| `RuntimeResolved` | `AgentRuntime` reference resolved |
| `CapabilityResolved` | `CapabilityProfile` reference resolved and its required refs/secrets valid (ach: endpoint+name+ek_; or envFrom present) |
| `MemoryResolved` | `MemoryProfile` reference resolved, if configured |
| `Hydrated` | Init hydration container terminated with exit 0 (derived from `pod.status.initContainerStatuses`) |
| `WorkloadRendered` | Generated workload manifests were rendered |
| `WorkloadReady` | Generated single-replica workload is ready (the pod passed `readyz`, i.e. adapters are listening, §8.5) |
| `ServiceReady` | Generated Service exists, if required |
| `Ready` | Agent is ready to receive/execute events |
| `SessionContinuityWarning` | Non-blocking durability mismatch warning |

Removed in v1.2.5: `PromptResolved`, `ProviderDialectResolved`, `ChannelsListening`. The first two require knowledge only the harness has (→ pod logs, not status); the third is subsumed by `WorkloadReady`/`readyz`.

---

# Part XIV — Security and Operations

## 38. Secret boundaries

- `ek_` secrets are mounted only where required for governed (`ach`) hydration and Forwarder access.
- `CapabilityProfile` source/envFrom secrets are mounted only where required (hydrator for sources; pod for envFrom).
- Channel secrets are mounted only into the main runtime container.
- Memory backend secrets are mounted only where required by the memory adapter.
- The hydrator init container never receives channel secrets.
- The main runtime does not receive local source credentials unless explicitly required at runtime.
- Logs must never emit `ek_`, source tokens, memory tokens, channel tokens or HMAC secrets.
- Runtime-generated configs must not persist plaintext provider credentials in a governed (`ach`) profile.

## 39. RBAC

- Operator watches `AgentRuntime`, `CapabilityProfile`, `MemoryProfile` and `Agent`.
- Operator creates child resources in the same namespace as the `Agent`.
- Cross-namespace secret, `AgentRuntime`, `CapabilityProfile` and `MemoryProfile` references are rejected.
- Operator does not need access to `ach-system` secrets.
- Runtime service accounts should be least-privilege.
- **The runtime process has no write access to `Agent`/`agents/status`.** Only the operator patches status, and only from control-plane-observable facts (resolved refs, `pod.status`). The runtime never self-reports; therefore no `patch agents/status` permission is granted to runtime service accounts.

## 40. Pod security

Generated Kubernetes workloads should support:

- non-root execution;
- dropped Linux capabilities;
- `seccompProfile: RuntimeDefault`;
- read-only root filesystem where possible;
- explicit resource requests/limits;
- NetworkPolicies for egress restriction;
- no unnecessary mounted credentials.

## 41. Shell-exec controls

Engines such as opencode, claude-code or codex may execute shell commands. Runtime profiles for shell-capable engines should use minimal images, isolated workspaces, restricted service accounts, constrained egress, redacted logs, explicit resource limits, and no ambient cluster credentials unless deliberately required.

## 42. Observability

Operator-level metrics: agents by runtime / hydration source / memory profile; generated workload readiness; hydration success/failure; rollout count; channel webhook failures; reconcile errors; Secret/config hash changes.

Runtime-level telemetry: invocations; channel event type; session mode; lane depth and lane wait time; slot saturation (global and per-channel); dedup hits; model selected; memory lookups/writes; action validation failures; repair attempts; action execution success/failure; unsupported side-effect attempts; drain events and drained-vs-killed counts; latency; token/spend metadata where available; trace IDs.

---

# Part XV — Mapping to `ackbot-process`

| Today (`config.yaml` / code) | Under this spec |
|---|---|
| `handlers.*` | `Agent.spec.channels[]` |
| `handlers.<h>.concurrency` | `Agent.spec.channels[].concurrency` (per-channel cap) + `Agent.spec.execution.limits.maxConcurrentInvocations` (global ceiling) |
| `handlers.<h>.expire` | `Agent.spec.channels[].expire` |
| GitLab webhook handler | GitLab channel adapter |
| Cron handler / `cron.tasks` | One cron channel per task, each with its own `channel.prompt` (O13 closed, §12.3/§14.4) |
| per-session race / `step_reserve` / `abort_session` workaround | Per-session FIFO lane (§18.3); missing valid action becomes a repair turn |
| `agent.prompt` | `Agent.spec.prompt` |
| `agent.opencode.*` | `AgentRuntime.spec.engine.opencode.*` |
| `GEMINI_*` env in governed mode | Removed; governed `CapabilityProfile.ach` → ACH Forwarder + `ek_` (harness materializes `ACH_*`, §13.1) |
| local provider env / `base_url` / `api_key` | `CapabilityProfile.envFrom` (non-governed); `Agent.model.provider` emits `PROVIDER_TYPE` |
| `plugins.repos`, `mcps`, `a2a_clients` through ACH | governed `CapabilityProfile.ach` Environment / hydration result |
| directly declared plugin repos | `CapabilityProfile.sources[]` |
| action parsing / `NO_ACTION` | Final JSON action contract / `{ "actions": [] }` |
| tool consent tiers | `consentTier` on `sideEffect` actions |
| GitLab MR comment / issue creation / MR approval | `reply` / `sideEffect` channel actions |
| hindsight endpoint config | `MemoryProfile.spec.hindsight` |
| hindsight mission/scope | `Agent.spec.memory` |
| redaction config | `Agent.spec.redaction` |
| opencode shared process | `AgentRuntime.spec.engine.opencode.shared` |
| durable session files | `AgentRuntime.spec.execution.kubernetes.persistence` |

---

# Part XVI — Implementation Slices

## Slice 1 — CRDs and static reconcile

- `AgentRuntime`, `CapabilityProfile`, `MemoryProfile`, `Agent` CRDs.
- validation rules; status conditions.
- generated single-replica workload from an ephemeral runtime (`persistence.enabled: false`).
- deterministic generated resource names.
- fake/manual trigger.

## Slice 2 — Persistent runtime

- `persistence.enabled: true` → PVC provisioning and mount.
- engine session directory wiring under `mountPath`.
- retain/delete policy handling.
- derived `durableSessions` and `BestEffortSessionContinuity` warning.

## Slice 3 — Hydration

- injected hydrator init container; `ach` and `local` modes.
- local source rendering; source-specific Secret mounting.
- secret-hash rollout; generated workspace; engine config rendering.
- a missing required prompt file (or unknown provider dialect) is a harness-side failure: the workload does not become ready and the cause is logged (no status condition).

## Slice 4 — Invocation, concurrency and action contract

- invocation envelope; prompt stack assembly.
- final JSON parser; action schema validation; repair attempts.
- global ceiling + per-channel caps + per-session FIFO lane + idempotency dedup.
- executable `reply` actions; reject unsupported `sideEffect` with a clear error.
- fake action adapter.

## Slice 5 — API/manual channel

- HTTP/manual channel; stable `/channels/{channelName}/events` endpoint.
- `readyz` (adapters listening; hydration guaranteed by the init gate; accept-and-buffer during engine warmup).
- action adapter log sink; basic session manager; observability IDs.

## Slice 6 — GitLab channel MVP

- generated Service for inbound webhook; webhook signature validation.
- MR note event; `session.mode: auto` deriving `project + mrIid`.
- `channel_message` comment action as `reply`; idempotency key dedup (`X-Gitlab-Event-UUID`, §18.4.0).
- **platform-side setup:** register a GitLab webhook filtered to "Merge request events" pointed at `…/channels/{name}/events` (one webhook per event-type channel; §16 fan-out).

## Slice 7 — Cron channel

- single cron scheduler inside the runtime process.
- `session.mode: none` default; `session.mode: custom` continuity.
- no Job-per-event behavior.

## Slice 8 — Lifecycle

- `preStop` delay; `SIGTERM` lane drain within grace period.
- sized `terminationGracePeriodSeconds`; drained-vs-killed metrics.

## Slice 9 — Memory MVP

- `MemoryProfile` resolution; Hindsight profile support.
- memory config injection; per-agent mission/scope rendering.
- memory lookup before prompt assembly; write/update after invocation; redaction before writes.

---

# Part XVII — Open Items

## O1 — Main runtime to ACH Forwarder credential contract — **CLOSED in v1.2.5**

The contract is the **env-var set the harness receives** (§13.1): `ACH_BASE_URL`, `ACH_API_KEY` (the `ek_`, presented as a bearer credential), and `ACH_PROVIDER_TYPE` (the dialect, from `Agent.model.provider`, default openai). The security invariants are normative: `ek_` as bearer, rotation by secret-hash restart (§11.6), no leak to logs or downstream tool backends, and envFrom precedence (§11.4). What the harness *does* with those env vars — building the engine client, routing model vs MCP vs A2A — is the harness's implementation, not specified by the spec. (In v1.3.0 the access values moved to `CapabilityProfile.ach`; the dialect choice is `Agent.model.provider`.)

## O2 — External runtimes

Future `AgentRuntime.execution.type` may support remote/managed environments (AWS AgentCore, Bedrock Agents, Claude Managed Agents, external HTTP). Not in v1.

## O3 — Capability declaration

Capabilities are **derived** in v1 (§8.3). A declared capability matrix remains intentionally rejected because it drifts from actual behavior. Revisit only if derivation proves insufficient for a future capability that is not structurally observable.

## O4 — Shared Channel CRD

Channels remain inline in v1. A `Channel` CRD may be introduced if a channel/account is shared by multiple agents or managed by a different team. Not in v1.

## O5 — Cluster-scoped runtime profiles

`AgentRuntime` is namespace-local in v1. Future: `ClusterAgentRuntime`, explicit `runtimeRef.namespace` with authorization, platform catalogs. Not in v1.

## O6 — Cluster-scoped memory profiles

`MemoryProfile` is namespace-local in v1; intra-namespace reuse only. Future: `ClusterMemoryProfile`, explicit `memory.profileRef.namespace` with authorization, platform catalogs. Not in v1.

## O7 — External durable substrate (consolidated)

v1 is single-replica with an in-process session lane and on-disk engine sessions. The following all depend on a single future capability — an **external durable substrate** (durable session store + durable lane/queue + coordination) — and are tracked together:

- external session store (replace on-disk opencode JSONL);
- durable lane (survive crash/`SIGKILL`, closing the §8.6 limitation);
- multi-replica execution and ingress HA (requires sticky/consistent routing or session-key sharding, plus cron leader election via `coordination.k8s.io`);
- on-demand / `jobPerInvocation` workers pulling from the durable lane.

Not in v1.

## O8 — On-demand execution

`onDemand` is removed from v1. A future `jobPerInvocation` builds on O7 (durable lane as the work queue) and requires async delivery semantics, idempotency, retry behavior, session continuity and observability. Not in v1.

## O9 — Side-effect execution policy

`sideEffect` actions are part of the API model; first bake may reject them at runtime. Future execution requires consent semantics, idempotency, audit trail, policy controls, rollback/compensation stance, and per-channel/action permission model. Not required for first bake.

## O10 — Local source pinning and supply-chain controls

Local hydration supports direct GitHub/Git sources in v1. Future: pinned refs, digests, lock files, source verification, signed bundles, content cache status, refresh policies. Not required for first bake.

## O11 — Provider/access wiring — **CLOSED (v1.2.5, restructured v1.3.0)**

Access now lives in `CapabilityProfile` (§11): governed via `ach` (endpoint+name+`ek_`) or non-governed via `envFrom`. `Agent.model.provider` supplies the dialect (`PROVIDER_TYPE`). The harness materializes the engine config (§13.1). The former `directExotic` escape hatch was **removed entirely** in v1.3.0 — raw env is just `CapabilityProfile.envFrom` in a non-governed profile, no special concept needed.

## O12 — Content/harness failure visibility — **closed for startup failures in v1.4.0**

Three failure planes, all control-plane observable, none requiring a runtime self-report:

- **Invalid config** → admission/reconcile.
- **Hydration failure** → init container exits non-zero → `Hydrated: False` (§8.5/§37).
- **Harness/engine startup failure** (unknown dialect, unreachable Forwarder, missing required prompt, bad credential) → the engine does not reach ready within `startupTimeoutSeconds` → **the process exits → the substrate marks the pod not-ready → `WorkloadReady: False`** (§8.5). The specific cause is in the pod logs (the harness must emit a clear message naming the offending value — never a cryptic stack trace). This closes the v1.2.5/1.2.6 promise that previously had a hole: `readyz` is adapters-listening, so a broken engine behind listening adapters used to stay `WorkloadReady: True`; the startup deadline now turns a terminal engine failure into an observable pod failure.

Still **not** modeled as status: failures that appear only *after* a healthy start (a model that stops answering mid-run, a Forwarder that drops mid-run, a memory backend that drops — the last is fail-open, §31). **This is closed by design, not deferred:** post-start failures are **per-invocation telemetry** (metrics, audit, trace), never `Agent.status`. *Status is the pod's, not the agent's* — the pod stays live and ready; an individual invocation failed. A failed invocation is `invocation_failed{reason=…}` + an audit entry, and SREs alert on the *rate*, not on a boolean CRD condition. There is **no runtime→`Agent.status` channel in v1** and none is planned: it would violate "the runtime never self-reports" (§39) and conflate "this agent is broken" with "this invocation failed". If an aggregate health-of-agent signal is ever wanted, the *operator* would derive it from metrics — the runtime would still not write status.

## O13 — Cron per-task prompt — **CLOSED in v1.4.1**

Resolved as a single unified field: **`channel.prompt`** (§12.3 layer 5b, §14.4). Cron per-task prompts and webhook per-event prompts are the same field — optional, append-only, ephemeral — injected at prompt-stack layer 5b. A cron multi-task agent is multiple cron channels, each with its own `prompt`/`schedule`/`session.key`; a webhook `channel.prompt` may template the payload. No engine/channel code change: the field maps to Hermes's native `channel_prompt` and cron job `prompt`.

## O14 — `/v1/responses` facade

An OpenAI-style `/v1/responses` facade (HTTP conversational front door translating to `Event → Invocation → Harness → Actions`) is **not in v1**. It only serves `response.mode: automatic`, is not the governed default path, and appears in no implementation slice — pure surface area for first bake. Revisit when a conversational/streaming HTTP product need is concrete.

---

# Part XVIII — Final v1.4.3 Decisions

1. **Four CRDs:** `AgentRuntime`, `CapabilityProfile`, `MemoryProfile`, `Agent`. All namespace-local; cross-namespace refs rejected.
2. **Three RBAC scopes / owners:** `AgentRuntime` → SRE/platform; `CapabilityProfile` + `MemoryProfile` → AI engineers; `Agent` → consumer/business.
3. **Objects that cross an RBAC boundary are referenced, never inlined.** `CapabilityProfile` and `MemoryProfile` carry credentials; an `Agent` references them by name and cannot inline access or memory. The reference is the security control.
4. **`CapabilityProfile`** composes optional blocks: `ach?` (governed access + governed Environment hydration), `sources?` (extra/own context), `envFrom?` (raw env). No `mode` discriminator. At least one of `ach`/`envFrom` required. "Governed" is **derived from the presence of `ach`** — no `governed` status field.
5. **`ach` is an external coordinate** (`endpoint` + `name` + `secretRef`), not an in-cluster CR reference, because the Hub may live off-cluster.
6. **`sources[]` role is derived:** additive over `ach`; sole context without `ach`. Additive-with-`ach` is API-modeled / not-yet-executable in v1 (§11.3, §22.1).
7. **`envFrom` precedence:** with `ach`, reserved `ACH_*` env are materialized after `envFrom` and win on collision (overwrite, not error); a non-blocking reconcile warning names the colliding key (§35). Keeps governance intact without admission collision checks.
8. **`AgentRuntime` holds nothing about access/credentials.** The `directExotic` escape hatch is **removed entirely** (its job is now just `CapabilityProfile.envFrom`). It does hold `health` (substrate-agnostic endpoint) and `startupTimeoutSeconds`.
9. **`Agent` holds behavior + choice:** prompt, channels, response, memory intent, and `model.default` + `model.provider` (the dialect choice; emits `PROVIDER_TYPE`, applies governed or not). No `access`/`hydration` on the Agent.
10. **Single replica per Agent in v1.** `replicas` and `workload.type` removed; operator selects the primitive (recommended `Deployment` `replicas:1` + `Recreate`).
11. `persistence` (`enabled`, `size`, `storageClassName`, `mountPath`, `retainPolicy`) is the only storage knob on `AgentRuntime`.
12. **Capabilities are derived, not declared.** `durableSessions ← persistence.enabled`; `singleWriterPerSession ← always true`; "governed" ← presence of `ach`.
13. `session.mode = none | auto | custom`; default `auto` for non-cron, `none` for cron; `custom` requires `key`; `auto` invalid on cron. `session.continuity = durable | bestEffort`; mismatch with a non-durable runtime is a warning.
14. **Three finite resource bounds, all always-enforced with defaults:** `maxConcurrentInvocations` (default `1`, concurrency); `maxInvocationSeconds` (default `1800`, run time — deadlock prevention); `maxQueuedTotal` (default `100`, queue depth — OOM prevention). Plus per-channel `concurrency` (default `1`)/`expire` (default `120`; `0`=never, interactive) and `idempotencyWindowSeconds` (default `3600`). FIFO serialization per session key; overcommit (no sum rule); `expire` exhaustion (and full-queue admission) never silent (§18.4.1). **Defaults are a safe floor, not tuned for minutes-long runs:** long-running channels (code review) must raise `concurrency`; published runtime profiles ship workload-appropriate caps. Per-channel `rateLimit` is **not** in v1 (OOM bounded by `maxQueuedTotal`).
15. **Idempotency is router-level and independent of `session.mode`:** one key-set per agent (`{channel}:{event.id}`), durable ⟵ `persistence.enabled`, covering `mode: none`. **`event.id` derived per channel type** (§18.4.0, modeled on Hermes `webhook.py`): webhook/http header chain → ms-timestamp fallback; slack `ts`; telegram `update_id`; cron scheduled-tick-time. Invariant: **unique-per-distinct-event, degrading to unique-per-arrival (process), never shared/empty (drop)**. **Pre-lane order pinned: dedup → backpressure → lane** (duplicates never consume queue). Queue-starvation is an accepted trade like slot-starvation (per-lane depth cap is the future hatch).
16. **Cron single-scheduler** by construction; **no ingress HA in v1** (declared); accepted loss modes in §30.1.
17. **Hydration init container; structural gate.** `Hydrated` derived from `pod.status.initContainerStatuses`. Startup order: channels listen → `readyz` Ready (accept+buffer, option A) → engine ready → lane drains. **Engine startup deadline** (`startupTimeoutSeconds`): exceeding it terminates the process → substrate marks not-ready → `WorkloadReady: False` (closes O12 without self-report). Graceful drain on `SIGTERM`; crash/`SIGKILL` loses queued/buffered work (O7).
18. `prompt.source = inline | file`; `prompt.compose = replace | append | prepend`. No `model.small`.
19. **Harness access translation contract (§13.1) closes O1 and O11.** The spec defines the env-var contract (`ACH_BASE_URL`, `ACH_API_KEY` as bearer `ek_`, `ACH_PROVIDER_TYPE`) and the security invariants (bearer, secret-hash rotation, no leak, envFrom precedence); the harness owns translation and routing.
20. Channels inline (secrets inline, 1:1 with the agent; O4 to promote). **Channel layer modeled on Hermes (MIT):** five types (`webhook`/`slack`/`telegram`/`a2a`/`cron`), `type` encapsulates transport+normalization+write-back+trait; `gitlab`/etc. are `webhook` channels with `deliver.type: gitlab_comment` (**no `gitlab`/`http` aliases — one form**); **multiple event types = multiple channels, not `routes[]`**; `a2a` is LiteLLM inbound (receiver-only, header auth, out-of-band registration); `enabled` optional default true. A single **`channel.prompt`** (optional, append-only, templated for webhook / static for cron; prompt-stack layer 5b) is the unified per-event prompt — closes O13. Model never talks to channels; harness validates/repairs; adapters execute only accepted actions; **dual delivery** (sync reply + out-of-band). `reply` executable in first bake; `sideEffect` API-modeled (§22.1).
21. Service-only inbound exposure (only for inbound-HTTP channels; Slack Socket Mode / Telegram polling / cron need no Service); stable endpoints `/channels/{channelName}/events`, `/healthz`, `/readyz`, `/metrics`.
22. **Status is control-plane observable only; the runtime never self-reports.** Conditions: `RuntimeResolved`, `CapabilityResolved`, `MemoryResolved`, `Hydrated`, `WorkloadReady`, `ServiceReady`, `Ready`, `SessionContinuityWarning`. No runtime `agents/status` RBAC. Operator watches all four CRDs.
23. **Memory failure is fail-open:** invocation proceeds without memory context (read and write); memory is enrichment, not a gate. No fail-closed in v1.
24. `onDemand`, a declared `capabilities` matrix, `directExotic`, and the `access.mode` axis are all removed from v1.
25. External durable substrate (session store + durable lane + durable dedup + multi-replica + on-demand) consolidated as O7.

---

**End of v1.4.3 draft.**
