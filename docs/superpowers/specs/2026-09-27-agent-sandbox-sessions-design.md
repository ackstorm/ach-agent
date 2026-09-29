# Agent Sandbox Sessions — Design

**Status:** Draft for review. Not an implementation plan.

**Date:** 2026-09-27

**Builds on:** [Portable Agent Execution and Isolation](2026-09-10-agent-execution-isolation-design.md)
(concept spec, vendor-neutral) and the
[three-role split contract](../../references/2026-09-14-three-role-split.md). This document
selects the sandbox vendor, fixes the session persistence model and splits the work across
repositories. It does not rewrite either source document.

**Repositories:** `ach-agent` (this repo), `../ach` (operator), `../aws-nglz-genai/terraform-genai-blueprint-module` (infra).

---

## 1. Goals

1. **Task agents** (sessionless, non-interactive): each event runs in a fresh isolated
   sandbox that is destroyed afterwards.
2. **Bot agents** (sessioned): one isolated sandbox per `session_key`. When the session goes
   idle its state is stored; when an event for that session returns, the state is restored
   and the conversation continues — including the next day.
3. **No credential in the sandbox.** The engine reaches models, ACH MCP servers, memory, a2a
   and repo checkout only through harness facades. The sandbox's network is restricted to the
   harness.
4. **Reuse upstream** rather than build: Kubernetes SIG projects where they fit.

## 2. Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Sandbox lifecycle uses **`kubernetes-sigs/agent-sandbox`** (v1.0.x, `agents.x-k8s.io/v1beta1` + `extensions.agents.x-k8s.io/v1beta1`): `SandboxTemplate`, `SandboxWarmPool`, `SandboxClaim` | SIG Apps project, stable API, one controller. Isolation itself is `runtimeClassName` (gVisor/Kata) in the template. |
| D2 | **kagent** (kagent.dev v1) and **agent-substrate** are references only; no code reused | kagent v1 is alpha on a forked pre-1.0 Substrate, lacks our channels and LiteLLM governance. Substrate's value (RAM checkpoint, actor multiplexing) needs its own control plane + Postgres + privileged DaemonSet; our engines keep state on disk, so a disk snapshot suffices. The *idea* is borrowed: idle → snapshot to object storage → sandbox returns to pool. |
| D3 | The **harness** creates and deletes `SandboxClaim`s, per event (task) or per session (bot) | The harness owns lanes and session lifecycle. |
| D4 | The in-sandbox process is the existing **mini-harness** (the three-role `engine` role, `ach_agent.execution`). `sandboxd` is not used | sandboxd is a generic process/file daemon (exec + files, no auth, no agent semantics). The mini-harness already speaks acquire/turn/session-op/session-import/release/cancel, and hosts the engine drivers (opencode HTTP/SSE, pi JSONL stdio). |
| D5 | Engine control moves from Unix socket to **TCP**; trust = **NetworkPolicy + per-session token** | mTLS not required yet. |
| D6 | Bot session state = **the whole per-session HOME** as one tarball, repo clone included | Engine-agnostic: opencode SQLite, pi storage, future engines. A resumed session finds the repo as it left it. |
| D7 | Export **on idle only**; a sandbox crash loses state since the last export | Accepted trade-off; no per-turn snapshots. |
| D8 | Resume tiers: **hot** (sandbox kept) → **PVC cache** (1 day) → **S3** (60 days, bucket lifecycle) | Fast common case, cheap long tail. |
| D9 | Restore is **not** a new session: channel `prepare` does not run | The workspace is already there. |
| D10 | Harness-owned config files (`opencode.json`, pi config, …) are **always rewritten** by the harness, including after restore | An agent cannot plant config for its next life. |
| D11 | `mcpServers` passthrough (`local`/`remote`) stays as-is: its credentials **do** reach the sandbox | Documented exception (see §9). Only ACH harness surfaces are credential-free. |
| D12 | No credential-injecting egress gateway | Harness facades cover it. |
| D13 | S3 access via **EKS Pod Identity** on the harness, provisioned in Terraform | No static keys. The sandbox never holds S3 access. |
| D14 | Phase 1 = **one harness replica**. Multi-replica + dedicated cache pod = phase 2 | See §10. |

## 3. Architecture

```
 harness pod (trusted, 1 replica in phase 1)                 Sandbox pod (untrusted, gVisor/Kata)
 ┌────────────────────────────────────────────┐              ┌──────────────────────────────────┐
 │ channels → router → lane(session_key)      │              │ mini-harness (execution role)    │
 │ SandboxBackend: claim / push / pull / drop │── control ──▶│   └─ engine: opencode | pi       │
 │ facades: model · McpProxy · memory · a2a · │   TCP+token  │ HOME=/home/agent (tar unit)      │
 │          repo checkout  (hold every key)   │◀── /t/<tok>/ │ no secrets · no SA token         │
 │ session store: PVC cache (RWO) ⇄ S3        │   facades    │ NetworkPolicy: egress → harness  │
 └────────────────────────────────────────────┘              │   + DNS only; ingress ← harness  │
         │ Pod Identity                                       └──────────────────────────────────┘
         ▼
   S3 bucket  <ns>/<agent>/<hash(session_key)>.tar.zst   (lifecycle: expire 60d)
```

The three-role split is kept: channels + harness stay in the trusted pod; the `engine` role
becomes the sandbox. Placement `standalone`/`distributed` remain available; sandboxed execution
is a new placement (§8).

**Deviation from the isolation spec §5:** that spec keeps the engine adapter on the trusted side
and calls the receiver "transport only". Here the adapter (engine driver) runs inside the
sandbox, as it already does in the `engine` role. This is acceptable because the adapter holds
no secrets; the harness treats every event it returns as untrusted input (existing terminal
validation stays harness-side).

## 4. Flows

### 4.1 Task agent (sessionless)

```
event → lane → claim (warm) → wait Ready → hello + token + engine config
      → workspace/prepare (channel prepare) → turn → deliver → delete claim
```

No restore, no export. `SandboxClaim.lifecycle.shutdownPolicy: Delete` with a
`shutdownTime` as a backstop if the harness dies mid-run.

### 4.2 Bot agent (sessioned)

```
event(session_key) → lane
  hot?   sandbox for session_key alive → turn                              (subsecond)
  else   ┌ claim (warm) ─────────────┐   in parallel
         └ fetch tarball: PVC → S3 ──┘
         → hello + token → stream tarball into sandbox, extract into HOME
         → rewrite harness-owned config (D10) → turn                       (seconds)
         none found → new session: channel prepare → turn
after turn: keep sandbox hot for idle window (release TTL)
idle window expires:
  stop engine → mini-harness streams tar.zst of HOME → harness writes PVC cache
  → delete claim → upload to S3 in background
```

- **Hot tier** reuses the existing `pool.release(key, ttl_seconds)` idea, moved from "keep the
  opencode process" to "keep the sandbox".
- **Claim ‖ fetch** in parallel so their latencies overlap.
- **PVC before S3**: sandbox deletion never waits on S3; a harness restart mid-upload finds the
  file in the PVC and retries.
- **Tar only with the engine stopped** — SQLite consistency.

## 5. The session tarball

| Aspect | Rule |
|---|---|
| Content | Entire session HOME, including workspace/repo. Token and harness config are outside HOME or rewritten (D10). |
| Format | `tar` + zstd, streamed end to end. Never fully buffered in memory. |
| Trust | Written by the agent → **untrusted**. The harness never extracts it; it only moves bytes between S3/PVC and the sandbox. Extraction happens in the sandbox, so a hostile archive can only damage its own sandbox. |
| Size cap | Configurable maximum; over the cap → export fails loud, session is lost (logged + metric). |
| Key | `<namespace>/<agent>/<hash(session_key)>.tar.zst`. No raw payload-derived text in keys; tenants separated by prefix. |
| Retention | PVC: evict after 1 day (sweep on startup + periodic) plus a size cap. S3: bucket lifecycle expiry, 60 days default. |
| Credentials in archive | None from ACH surfaces. **Exception:** `mcpServers` passthrough credentials that land in engine config — mitigated because harness-owned config is rewritten on restore, but a copy may still sit in the archived HOME. See §9. |

## 6. Transport and trust between harness and sandbox

- **Control:** the existing `/execution/v1/*` API of the mini-harness, over TCP on the Sandbox
  headless Service FQDN / pod IP. `boot/execution_client.py` already accepts a `base_url`.
- **Token on first contact:** warm-pool pods are generic, so no per-session secret exists at
  pod creation (claim `env` forces a cold start). The harness generates a per-session token and
  pushes it in the first call; the mini-harness accepts exactly one `hello`, then requires the
  token on every call. The window before `hello` is closed by NetworkPolicy (ingress from the
  harness only).
- **Facades:** served on the harness pod IP instead of `127.0.0.1`, still only under
  `/t/<token>/…` (`engine/trace.py` `tokenize_url`). Tokens are per session and revoked on
  claim deletion.
- **NetworkPolicy** (`networkPolicyManagement: Unmanaged` on the template, ACH renders its own):
  sandbox egress → harness facade port(s) + DNS only; ingress ← harness pods only.
  `automountServiceAccountToken: false`.
- **New export route:** `session-export` streams the HOME tarball out; `session-import` is
  extended to accept a tarball stream. Exact wire shapes are left to the plan.

## 7. Harness changes (`ach-agent`)

1. **Sandbox backend module** — one module owning: claim create/delete (deterministic name =
   `hash(session_key)`), Ready wait (bounded, explicit failure), hello/token, tarball
   push/pull. Kept in one place so a future backend swap or the phase-2 cache pod touches one
   module. No speculative interface.
2. **Session store** — PVC cache read/write, S3 get/put (streamed), 1-day sweep. One module.
3. **Pool** — hot-tier idle TTL keeps the sandbox, and on expiry triggers export → PVC →
   delete claim → background S3 upload.
4. **Facades bind** to the pod network with per-session token routing.
5. **Mini-harness**: TCP listener, token check after `hello`, `session-export`, tarball
   `session-import`.
6. **Config**: sandbox mode enabled, idle window, tarball size cap, bucket/prefix, PVC path.
   `make schema` after editing.
7. **Docs**: mark the `mcpServers` exception in CLAUDE.md invariant section as reaching the
   sandbox; update `docs/references/README.md` with this doc when accepted.

## 8. Operator changes (`../ach`)

1. New placement (e.g. `sandboxed`) on `AgentProfile.spec.achagent.placement`.
2. Render from `AgentProfile`: `SandboxTemplate` (mini-harness image, `runtimeClassName`,
   resources, HOME volume, `automountServiceAccountToken: false`, `networkPolicyManagement:
   Unmanaged`) and `SandboxWarmPool` (replicas).
3. Render the sandbox NetworkPolicy (§6) and the harness egress rule to sandbox pods.
4. Harness Deployment: Role/RoleBinding for `sandboxclaims` (create/get/patch/delete) in
   its namespace only; RWO PVC for the tarball cache; session bucket/prefix/retention in
   `config.json`.
5. Dependency: agent-sandbox CRDs + controller (+ extensions) installed in the cluster; the
   chart documents the minimum version.
6. `docs/api-reference/` + `examples/` in the same commit (ach doc-hygiene rule).

## 9. Known exception: `mcpServers` passthrough

`LocalMcpServer`/`RemoteMcpServer` (`engine/mcp_passthrough.py`) resolve `${env:NAME}` and
write the result into engine config / child env. With sandboxes these credentials therefore
reach the sandbox and may be captured in the archived HOME. **Accepted for now (D11)**, the
operator opts in by naming the server. Future options, not scheduled: route `remote` servers
through a harness facade (same `_forward` path as `McpProxy`); run credential-bearing `local`
servers outside the sandbox.

## 10. Phase 2 (not implemented now)

- Multiple harness replicas: Redis/Valkey lock per `session_key` (lease + renew), dedup in
  Redis (`SET NX` + TTL). The deterministic claim name from phase 1 lets any replica find a
  hot sandbox.
- **Dedicated cache pod** replacing the per-harness RWO PVC cache (small HTTP service with its
  own PVC and auth).

## 11. Infrastructure (Terraform)

In `../aws-nglz-genai/terraform-genai-blueprint-module`:
- S3 bucket for session tarballs, SSE, public access blocked, lifecycle expiry (60d default).
- Pod Identity association: harness ServiceAccount → IAM role scoped to the bucket/prefix
  (`GetObject`, `PutObject`, `DeleteObject`).
- RuntimeClass for gVisor (or Kata) on a node group that supports it.
- agent-sandbox controller installation (if not owned by the ach chart).

## 12. Acceptance evidence

1. Task agent: event → sandbox from warm pool → result → claim gone.
2. Bot agent: turn 1 → idle expiry → tarball in PVC and S3 → claim gone → turn 2 restores and
   the engine remembers turn 1; the repo's uncommitted change is present.
3. PVC miss (entry older than 1 day) restores from S3.
4. From inside the sandbox: no ACH credential in env, files or `/proc`; egress to anything but
   the harness facades and DNS fails; cloud metadata unreachable.
5. A tampered `opencode.json` inside a restored HOME is overwritten before the turn.
6. Calls to the mini-harness without the session token are rejected after `hello`.
7. Resume latency measured per tier (hot / PVC / S3).

## 13. Open for the plan

- Default idle window and warm-pool size.
- Exact wire shapes for `hello`, `session-export`, tarball `session-import`.
- Where the agent-sandbox controller is installed (ach chart vs Terraform).
- Whether `standalone`/`distributed` placements remain long-term once sandboxed is proven.

---

## Amendments 2026-09-28

These supersede the body above where they differ. Implementation detail lives in
`docs/superpowers/plans/2026-09-28-agent-sandbox-sessions.md`.

1. **Archive format tar.gz** (stdlib), not zstd. Key: `<ns>/<agent>/<digest>.tar.gz`,
   `digest = sha256(session_key)[:32]`.
2. **Session end is sandbox-initiated.** The mini-harness owns the idle timer: no invocation for
   `idleSeconds` → close admission → stop engine → pack HOME → `PUT` to the harness gateway
   (`/s/<facade-token>/session/archive`). The harness writes PVC then S3 and deletes the claim.
   Replaces §4.2's harness-driven export.
3. **Kubernetes is the registry.** No SQLite, no ACH Postgres (that would put DB credentials in
   every agent namespace). Live sandboxes are the agent's labelled `SandboxClaim`s; claim name and
   `ach.ackstorm.ai/session` label carry the digest; the harness→sandbox bearer and the
   sandbox→harness facade token are `HMAC(K, purpose:claim)` with a per-agent Secret `K` held only
   by the harness; `lifecycle.shutdownTime` (refreshed per lease) is the idle backstop. A restarted
   harness lists claims, re-derives tokens, keeps serving facade calls and accepts archive pushes.
   Multi-replica (phase 2) inherits this for free.
4. **FacadeGateway** (§6): facades stay on loopback; one pod-network port verifies the facade
   token on every request and relays to registered facade ports only. Needed because `/t/<token>/`
   is correlation (minted by the engine pool), not authentication.
5. **Harness restart mid-turn:** the in-flight turn fails; the session survives (the mini-harness
   releases the lost controller, stops the engine, accepts the new harness).
6. **D10 widened:** every harness-owned agent config is rewritten — opencode.json and the system
   prompt file, pi models/settings/mcp.json, hydrated skills/prompts/artifacts.
7. **`handoff` + in-sandbox session hooks** (supersedes §3's harness-side prepare and D9).
   `channel.prepare` → `channel.handoff`: credentialed harness script run only for a new session in
   an empty staging dir; its output is shipped into the sandbox at `<workDir>/handoff`
   (content-agnostic). `channel.cleanup` is removed. Agent-level `hooks.sessionStart` (once per
   session, after the handoff, before the first turn, fail-closed) and `hooks.sessionSuspend`
   (every sandbox stop, before the HOME archive, best-effort, may run many times) run inside the
   sandbox with only `engine.forwardEnv`. The harness never extracts or runs scripts over an
   agent-written archive. The job/steps workflow model is parked as a future idea.
8. **Same options in every placement; `distributed` removed.** Placements differ only in where
   the engine runs: `standalone` (local child over a Unix socket; local testing and quick k8s
   tests) and `sandboxed` (claimed sandbox over TCP). `handoff`, `hooks` and new-session detection
   (the session workspace did not exist yet) behave identically in both. `distributed` is removed
   (follow-up plan). No babysitting: without `cleanup`, workspace disk growth in `standalone` is the
   operator's concern — no retention sweep.
9. **Handoff cadence is configurable:** `handoff.scope: event` (default, every invocation — today's
   prepare cadence) or `session` (new sessions only). Every run starts from an empty staging dir
   and replaces `<workspace>/handoff` wholesale; the agent should not keep edits inside it.
