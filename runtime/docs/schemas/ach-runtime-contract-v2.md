# ach-runtime host contract v2 (draft)

Status: **draft proposal, revision 5, 2026-10-27 (external review rounds 1–4 applied). Not implemented.** Decisions are listed in section 9. There is no backward compatibility with v1: runtime and host are upgraded together.

This document defines the boundary between **ach-runtime** and whatever runs it, called the **host**. It replaces contract v1 §1 (model and responsibilities) and §11 (fixed infrastructure interface). It amends §5 and §7 as listed in section 8.

The runtime behaviour in v1 §2–4, §6 and §8–9 stays authoritative and unchanged: identities, admission and ACK, execution, handoff and hooks, artifacts, results.

How v1 responsibilities move to the host is in [the migration note](ach-runtime-contract-v2-migration.md).

## 1. Principle

ach-runtime is a runtime. It runs one agent and nothing else.

It never creates, starts, stops or inspects the containers, sandboxes or machines it runs in, and it has no client for any infrastructure API. Everything below the runtime belongs to the host:

- processes, containers, sandboxes;
- networks, names, permissions;
- isolation;
- cleanup.

Any host that fulfils section 3 can run the runtime: a shell, a container stack, a cluster operator, a sandbox platform. The runtime does not know which one it is.

The contract has two sides:

| Side | Who depends on it | Stability |
|---|---|---|
| **External**: sections 2–5 and 7 | Hosts | Versioned. A breaking change needs a new contract version. |
| **Internal**: section 6 | Only the runtime itself | Free to change between runtime releases. Hosts must not call or rely on it. |

Vocabulary:

| Term | Meaning |
|---|---|
| **Runtime unit** | One process started by the host from a runtime image or install, running one runtime role. |
| **Workspace** | The runtime's unit of shared files and sessions (v1 §1–2). Workspace identity, keys and names are runtime-owned. |
| **Execution unit** | A runtime unit running the `execution` role for exactly one Workspace. |
| **Instance** | One start of one execution unit. A restart is a new instance, even if the host reuses the container, sandbox or name. |

## 2. Roles and topologies

The runtime has internal components: Channels, Harness, facades, mini-harness and the OpenCode engine. A host does not launch components; it launches one of these roles:

| Role | Command | Contains | Listens on |
|---|---|---|---|
| `agent` | `python -m ach_runtime agent --config <config>` | Everything. Workspaces are local directories run in-process. | `8080` ingress |
| `control` | `python -m ach_runtime control --config <config>` | Channels, Harness, facades. Workspaces are hosted by the host (section 5). | `8080` ingress; `8081` facades and host API |
| `execution` | `launch.command` from section 5 | Mini-harness + engine for one Workspace. | `launch.port` (`8080`): execution API, internal (section 6) |

A host picks one topology:

- **Single unit:** one `agent` unit. The host provides only section 3. No Workspace hosting.
- **Split:** one `control` unit, plus one `execution` unit per Workspace the runtime asks for. The host also implements section 5.

**Isolation is a host property, not a runtime one.**

- In the single-unit topology, the engine and the agent's tools run in the same unit as the Harness. The unit holds the ACH identity, the runtime key and the storage credentials. The engine subprocess gets an allowlisted environment, but it shares the unit's user and filesystem. So the runtime provides no credential isolation between the agent's tools and those secrets.
- **The single-unit topology is therefore for running outside Kubernetes: a developer's laptop, a test or pre-production machine, CI. Production always uses split, on Kubernetes** (D5).
- The split topology keeps secrets in the `control` unit. Execution units receive none.
- How strongly units are isolated from each other and from the network is decided entirely by the host.

## 3. What the host provides

### 3.1 Configuration document

- The host renders and delivers one JSON configuration document per agent. The format is section 7: the v1 `workspace-v1` schema minus its infrastructure fields.
- The runtime reads it from the path given by `--config` at start. It does not watch the file.
- A configuration change takes effect when the host restarts the unit. The host decides when; v1 §5 explains what an in-flight restart loses.
- The host computes `configVersion` exactly as v1 §5 defines.
- `agent.uid` is a host-assigned canonical UUID that identifies this agent instance for its whole life.
  - The host must never reuse it for a different agent, and must keep it stable across restarts.
  - Every durable runtime reference (Workspace refs, storage keys, sessions) derives from it, so changing it orphans all stored state.
- `agent.namespace` is an opaque, host-assigned scope string with the same stability rule. `agent.name` is a human label.

### 3.2 Environment

The host sets these environment variables. Values are single-line strings.

| Variable | Roles | Required | Meaning |
|---|---|---|---|
| `ACH_SANDBOX_KEY` | `agent`, `control` | yes | Runtime master key. 32 random bytes, hex. The host generates it once per agent, keeps it stable and never gives it to execution units. Everything the runtime signs or verifies derives from it, including the host API credential (5.2). |
| The variable named by `ach.identity.env` | `agent`, `control` | yes | The agent's ACH `ek_` credential. |
| Variables named by config aliases | `agent`, `control` | when configured | Channel auth (`channels[].auth.env`, A2A `auth.env`), handoff and script `secretEnv.*.env`, memory `auth.env`. Each name the config references must exist. |
| Literal `engine.env` values | — | — | These are in the configuration document, not in the environment (v1 §5). |
| `ACH_STORAGE_S3_BUCKET`, `ACH_STORAGE_S3_REGION`, `ACH_STORAGE_S3_ENDPOINT_URL`, `ACH_STORAGE_S3_PREFIX` | `agent`, `control` | bucket required when Workspace persistence, session persistence or artifacts is enabled | S3-compatible storage. Credentials come from the standard AWS SDK chain (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, optional `AWS_SESSION_TOKEN`, or ambient). A missing bucket when storage is required is a configuration error; there is no volatile fallback. |
| `REDIS_URL` | `agent`, `control` | when a `queue` channel is configured | Redis for queue channels. |
| Launch environment (5.5) | `execution` | yes | Copied verbatim from the runtime's launch spec. |

Rules:

- The runtime reserves the `ACH_`, `XDG_` and `OPENCODE_` prefixes plus `HOME` (v1 §5). The host must not set other names in those spaces.
- Anything else the host sets is visible to Channels and Harness. Engine processes receive only the allowlisted base environment plus `engine.env`.

### 3.3 Services the runtime must reach

| Service | Needed by | When |
|---|---|---|
| An ACH-compatible platform API at `ach.baseUrl` (3.3.1) | `agent`, `control` | always |
| S3-compatible storage | `agent`, `control` | persistence or artifacts enabled |
| Redis | `agent`, `control` | queue channels |
| MCP servers listed in the hydrated catalog | `agent`, `control` | when the catalog lists any |
| The control unit's facade endpoint (`infrastructure.facadeEndpoint`) | `execution` | split topology |

These dependencies are interfaces, not products. The runtime depends on an API shape, and the host chooses what implements it:

- the real ACH platform and AWS S3;
- or local stand-ins, such as an S3 emulator and a small ACH-compatible stub in front of any model server.

The runtime's own end-to-end suite already runs this way, with SeaweedFS for S3 and a stub for hydration.

Execution units need no other egress for the runtime itself. Egress the agent's own tools need (git hosts, package registries) is the host's policy decision.

#### 3.3.1 ACH-compatible platform API (minimum)

The runtime calls only these endpoints. Every call carries the agent credential as `x-ach-key`, plus the runtime's identity headers.

| Call | Purpose | A stand-in must |
|---|---|---|
| `POST {baseUrl}/platform/hydrate` | Resolve the agent's environment into a catalog: `environment`, `runtime.models[]` (`id`, `endpoint`), `runtime.mcpServers[]`, `runtime.a2aAgents[]`, `context.{skills,prompts,artifacts}` | return a static catalog. `runtime.models` must contain the configured model with a non-empty `endpoint`; the other lists may be empty. |
| `GET {baseUrl}/v2/model/info?model=` | Model limits and costs (LiteLLM shape) | answer `{"data": []}`. Limits and costs are then unknown and are reported as such. |
| `{baseUrl}/v1/…`, `{baseUrl}/gemini/v1beta/…`, `{baseUrl}/anthropic/…` | Model traffic proxied by the model facade (OpenAI-, Gemini- or Anthropic-compatible) | forward to any compatible model server. |
| `GET` of catalog `downloadUrl`s | Skill, prompt and artifact archives | serve them, or list none. |

Rules:

- `ach.identity` still names an environment variable, but its value is opaque to the runtime. A stand-in may accept any value.
- Memory (`memory.*`) and A2A egress are optional features that need their own endpoints when configured.
- Anything the stand-in does not provide must be absent from the catalog and the configuration; it must not fail at call time.

### 3.4 Filesystem

| Role | Writable paths | Notes |
|---|---|---|
| `agent` | Workspace root, sessions root, temp dir (flags, with defaults) | Ephemeral. Durable state goes to storage (v1 §7). |
| `control` | temp dir | No durable local state. |
| `execution` | `--workspace-dir` (default `/workspace`), `--sessions-root` (default `/var/lib/ach-runtime/sessions`), temp dir | Ephemeral, and owned by one instance. A new instance must start with empty directories or directories the runtime may overwrite (see 5.6). |

The runtime runs as an unprivileged user and needs no root, extra capabilities or privileged mounts.

### 3.5 Network exposure

| Listener | Expose to |
|---|---|
| `8080` on `agent` / `control` | Whoever sends events: webhooks, A2A callers, gateways. Also the host's health checks. |
| `8081` on `control` | Execution units (facades) and the host (host API). Never to event senders or the public. Must be reachable before the unit is ready: the host delivers the inventory that readiness waits for (5.4). |
| `8080` on `execution` | The `control` unit only. |

The host owns names, routing and TLS for these listeners. `infrastructure.facadeEndpoint` must be the URL by which execution units reach `8081/facades` on the control unit.

### 3.6 Process lifecycle

- **One agent, one control.** At most one `agent` or `control` unit per `agent.uid` runs at a time. There is no high availability for the coordinator. Storage fencing (v1 §7) protects data against an overlap, but does not make one correct.
- **One Workspace, one live instance.** The host runs at most one execution unit per Workspace name. Storage fencing turns a stale instance into a failed writer, not a second writer.
- **Same release.** `control` and its `execution` units must run the same runtime release. The internal protocol (section 6) is only compatible within one release. The control unit checks the release on every probe (5.6) and never drives a unit of another release.
- **Shutdown.** SIGTERM starts a drain bounded by `workspace.shutdownTimeoutSeconds`. The host must allow at least that plus 15 seconds before killing the unit. A kill before the drain ends can lose uncommitted work (v1 §7).
- **Restart.** The host restarts failed units. A restarted `control` unit rebuilds its state from storage and from the host's inventory (5.4).
- **Health.** `GET /healthz` is liveness. `GET /readyz` is readiness. Both are on `8080`, on every role. Section 4 has their meaning.

## 4. What the runtime provides

| Surface | Where | Contract |
|---|---|---|
| Channel ingress | `agent`/`control` `8080`: `POST /channels/{channel}/events`, `/a2a/{channel}/…` | v1 §2–3: authentication per channel config, HTTP 202 on admission, 503 when the queue is full or closed. |
| Liveness | `GET /healthz`, every role | 200 while the process serves. A failure means restart the unit. |
| Readiness | `GET /readyz`, every role | `agent`/`control`: configuration and hydration valid and adapters listening; does not need any Workspace. `control` also waits for the host's inventory (5.4). `execution`: configured by its control unit and restore finished. |
| Metrics | `GET /metrics` on `agent`/`control` `8080` | Prometheus, prefix `ach_agent_`. No user keys as labels. |
| Logs | stderr | JSON lines for run records and anomalies (`kind` from v1 §9); secrets redacted. |
| Workspace demand | `control` `8081`, section 5 | Split topology only. |

The runtime guarantees nothing about isolation, network policy or resource limits. Those are the host's.

## 5. Workspace hosting (split topology)

### 5.1 Model

The runtime decides which Workspaces it needs: first session, idle close, limits, recovery (v1 §3 and §7). The host decides how and where they run.

**The host always calls the runtime; the runtime never calls the host.** The runtime publishes desired state on its host API. The host reconciles it and registers what it started. That keeps the runtime free of infrastructure clients, and lets any host implement this with a polling loop.

**Host messages are hints; the unit is the truth.** A registration makes the runtime probe a unit, and the identity it uses is the one the unit reports (5.6), never one the host supplies. A message that no longer matches the runtime's current view is a no-op (5.3).

```text
runtime (control)                         host
  wants Workspace W  ── desired: running ──▶ start one execution unit from W's launch spec
                     ◀── PUT instance ────── registers its base URL
  probe, restore, configure, serve
  W idle, committed  ── desired: released ─▶ stop and remove the unit
  W unhealthy        ── desired: released ─▶ (same; W is listed running again for a fresh unit)
                     ◀── DELETE instance ─── confirms it has stopped
  unit lost          ◀── DELETE instance ─── optional: host reports it gone
```

### 5.2 Authentication

- Host API requests carry `Authorization: Bearer <host token>`.
- The host token is `HMAC-SHA256(key, "host:" + agent.uid)`, as lowercase hex.
  - `key` is the UTF-8 bytes of the `ACH_SANDBOX_KEY` value exactly as set. The hex text is used as given; it is not decoded to raw bytes.
  - The host computes the token from the key it already provides, so no new secret is introduced.
- The runtime rejects host API calls on any other credential. Facade tokens and engine bearers are not accepted on host routes.

### 5.3 Messages

All routes are on `control` `8081`, under `/host/v1`, and are served from process start, before `/readyz` is 200. JSON bodies are at most 1 MiB; a larger one is 422. Every route answers 401 on a bad credential and 503 while the control unit is stopping.

**`GET /host/v1/workspaces?after=<cursor>&waitSeconds=<0..60>`** lists desired state.

```json
{
  "cursor": "8f0c….42",
  "runtimeEpoch": "8f0c…",
  "adopting": false,
  "workspaces": [
    {
      "name": "agent-poc-openshell-1a2b3c4d5e6f7a8b9c0d",
      "workspaceRef": "1a2b…",
      "desired": "running",
      "launch": {
        "runtimeRelease": "0.1.22",
        "port": 8080,
        "command": ["python", "-m", "ach_runtime", "execution"],
        "env": {
          "ACH_SANDBOX_VERIFY_KEY": "…",
          "ACH_WORKSPACE_NAME": "…",
          "ACH_WORKSPACE_REF": "…",
          "ACH_BOOTSTRAP_JSON": "{\"bootstrapVersion\":\"workspace-v2\",\"agent\":{…},\"facadeEndpoint\":\"…\"}"
        }
      },
      "instance": {"instanceId": "…", "baseUrl": "http://…:8080", "state": "serving"}
    }
  ]
}
```

- **`cursor`** is opaque. Pass the last one as `after`. If `after` equals the current cursor, the request is held until something changes or `waitSeconds` passes (default 0, capped at 60). Otherwise, and during shutdown, it answers at once.
- **`runtimeEpoch`** changes each time the control unit starts.
- **`adopting`** is `true` from start until the inventory (below) is accepted. While it is `true`, `workspaces` is empty and **is not desired state**: the host must not stop or remove anything because it is unlisted. From the moment the inventory is accepted, every reported unit is listed as `running` with state `probing`, so the host keeps it.
- **`desired`** is one of:
  - `running`: keep exactly one unit;
  - `released`: stop and remove the unit, then confirm with `DELETE`.
- **`instance`** is `null` until the host registers a unit. After that, `state` is `probing` or `serving`, and `instanceId` is `null` until the unit's first successful probe. A unit that fails its probe is listed `released`.
- When `adopting` is `false`, the list is complete: a Workspace that is not listed must have no unit.

**`PUT /host/v1/workspaces/{name}/instance`** registers a started unit. Body: `{"baseUrl": "http://…:8080"}`.

| Status | When |
|---|---|
| 202 | Registered. The runtime probes the unit, learns its instance ID (5.6), then restores, configures and serves (v1 §7). |
| 200 | The same `baseUrl` is already registered, whether `probing` or `serving`. Nothing changes. |
| 409 | `{"error": "not_desired"}` when the name is not listed as `running`; the host should remove its unit. `{"error": "registered"}` when a different `baseUrl` is registered; nothing changes, and the registered unit keeps serving. A host that moved a unit to a new address first reports the old one with `DELETE`. |
| 422 | Malformed body or URL. |

**`DELETE /host/v1/workspaces/{name}/instance[?instanceId=<id>]`** reports that a unit has stopped. It always answers 204.

- For a `released` entry, it confirms the unit has stopped, and the entry disappears.
- For a `running` entry with a registered unit, it reports an unexpected loss. The entry disappears, and the runtime lists the Workspace `running` again for a fresh unit (5.6). Runs whose outcome cannot be established become `unknown` and are never replayed (v1 §7).
- **Precondition.** Once the runtime has observed an instance ID for the entry, a `DELETE` must carry that same `instanceId`; any other `DELETE`, including one without `instanceId`, is a no-op. Hosts always pass the `instanceId` from the last listing.
- An unknown name, or an entry with no registered unit, is a no-op.
- Before the first successful probe there is no ID to check, so a late `DELETE` can remove a registration that has not served yet. That unit holds no work; the cost is one more start.
- A repeated `DELETE` never releases anything twice: the first one removes the entry.

**`PUT /host/v1/inventory`** reports every execution unit the host runs for this agent. Body: `{"runtimeEpoch": "…", "units": [{"name": "…", "workspaceRef": "…", "baseUrl": "…"}]}`.

| Status | When |
|---|---|
| 202 | Accepted. `adopting` turns `false` and the reported units are listed as `probing`. Probing, reconnecting and any retirement follow through the normal listing. |
| 200 | Adoption already ran for this epoch. The request is ignored. |
| 409 | `runtimeEpoch` is not the current epoch. |
| 422 | A unit's `name` does not belong to its `workspaceRef`. The whole request is rejected. |

- Every listed unit is adopted, regardless of `limits.maxActiveWorkspaces`: probed, checked and reconnected, and runs that are still executing are recovered (v1 §3). Adopted units count against the limit, so new Workspaces wait until enough of them close.
- A unit that cannot be adopted is listed as `released`. That covers a unit unreachable until its probe deadline, one of another release, and one whose reconnect fails. Its uncommitted work is lost, as for any lost unit (v1 §7). Its slot stays held until the host's `DELETE`.

### 5.4 Host obligations

1. **Talk to the host API before readiness.** The control unit's `/readyz` waits for the inventory, so the host must not wait for that readiness before calling `/host/v1`.
2. **Send the inventory first.** When `runtimeEpoch` changes, send `PUT /host/v1/inventory` with every live unit for this agent, or an empty list if none are live. The control unit stays not-ready until it has the inventory; there is no timeout.
3. **Reconcile, only while `adopting` is `false`.** Poll or long-poll `GET /host/v1/workspaces`.
   - For each `running` entry, make sure one unit exists, started from `launch`. If `instance` is `null`, or its `baseUrl` is not this unit's, `PUT` the unit.
   - For each `released` entry, stop and remove the unit. Once its process has stopped, `DELETE` with the listed `instanceId`. Never confirm a stop that is not confirmed, for example after a forced removal whose process may survive.
   - Remove the units of Workspaces that are not listed.
4. **Report loss, optionally.** `DELETE` with the listed `instanceId` when a unit stops without being asked. The runtime also detects restarts and loss by itself (5.6), so this only makes recovery faster.
5. **Clean up.** When the agent is removed, stop and delete all its execution units. The runtime may not be running at that point. Durable state in storage stays until the host or an operator deletes it.
6. **Respect capacity.** Apart from adopted units, the runtime never lists more `running` Workspaces than `limits.maxActiveWorkspaces`. The host may delay a start, for example while waiting for capacity. A Workspace that is not registered before the invocation deadline fails that invocation (v1 §3).

### 5.5 Launch spec

`launch` is everything the host needs to start an execution unit for that Workspace, and nothing more.

- It contains no secrets. The verify key is public, and Workspace names and refs are opaque identifiers. So the host may store `launch` in plain workload definitions, logs or status.
- It is stable for the Workspace's lifetime. Every instance of a Workspace, including replacements and in-place restarts, uses the same `launch`, because the instance identity is minted by the process (5.6), not by the host.
- It changes only when the control unit's configuration or release changes. A host should start new instances from the current `launch`. It may keep running instances until they are released, but a unit of another release is never driven by the control unit (5.6).

An execution unit must be started with:

- A runtime image or install of release `launch.runtimeRelease`, the same release as the control unit (3.6). The host picks the image itself; the runtime names only the release and checks it on every probe.
- `launch.command`, listening on `launch.port`.
- Exactly the `launch.env` variables. They are single-line, each at most 8 KiB. The execution bootstrap travels inside them as `ACH_BOOTSTRAP_JSON`, so the host has no file to render.
- Writable paths per 3.4.
- Egress to `infrastructure.facadeEndpoint`.
- Reachability from the control unit before the unit is ready. Its `/readyz` turns 200 only after the control unit has configured it, so the registered `baseUrl` must route to a not-ready unit.

The host adds nothing in the `ACH_` space beyond `launch.env`. It may add its own scheduling, resources and policy freely; those are not runtime concerns.

Workspace names are runtime-owned opaque strings: at most 52 characters of `[a-z0-9-]`, unique per agent. The host may map a name to its own, shorter, identifiers. It must keep that mapping stable, and must not parse the name.

### 5.6 Instance identity

- Every execution process mints a fresh random instance ID when it starts. It returns the ID on every response, in the `X-ACH-Instance-Id` header.
- `GET /healthz` on an execution unit answers `{"status": "ok", "release": "<runtime release>"}`. The control unit treats a unit of another release as a failed probe.
- The host does not provide, choose or copy the instance ID.
- The runtime binds a registration to the first instance ID it observes there:
  - Any later different ID on the same Workspace means the unit was restarted, so its local files cannot be trusted. The runtime restores before serving again (v1 §7).
  - The storage writer identity, which v1 calls `writer_pod_uid`, is this instance ID.
- Whatever detects the loss of an instance (a different ID, a failed health check, a host `DELETE`), the runtime handles it the same way: the Workspace is listed `released`, then `running` again for a fresh unit, and restored.
- A run on a lost instance whose outcome was not already confirmed ends `unknown` and is never started again (v1 §3). Loss of access to an instance is not evidence that the run failed. An outcome confirmed before the loss is kept.

This removes the need for the host to expose a platform identity. It also catches restarts that hosts perform silently, such as a process or container restart that keeps the same name, configuration and environment.

### 5.7 Accepted limits

These are deliberate, not guarantees the runtime provides:

- **Facade tokens are bound to the Workspace name, not to an instance.** They stop working when the Workspace has no serving instance, but a former instance still running beside a newer one could keep using them. The host's one-live-instance rule (3.6) is what prevents that.
- **Storage fencing protects storage only.** It turns a stale instance into a failed writer. It does not revoke that instance's access to models or tools.
- **Some recoveries discard uncommitted work:** an upgrade with busy Workspaces (units of the old release are not adopted), and any unit that cannot be adopted. Work since the last commit is lost, as for any lost unit (v1 §7).

## 6. Internal topology (not part of the host contract)

This section is informative. Hosts must not depend on it.

- **Components.**
  - Channels authenticate and normalise events.
  - The Harness does admission, sessions, limits, run tracking, persistence coordination and Workspace demand.
  - The facades are model, MCP, memory, artifacts and A2A; they hold all credentials.
  - The mini-harness manages sessions, processes, hooks and snapshots.
  - OpenCode runs the agent.
- **In-process versus split.** In `agent` the mini-harness and Workspaces run in-process with local directories. In split they run in execution units and are reached over HTTP. Admission, limits, persistence and hook semantics are identical in both.
- **Harness to mini-harness.** HTTP on the execution unit's `8080`:
  - `/configure`, `/start`, `/status/{id}`, `/runs`, `/cancel`;
  - `/sessions…`, `/workspace/archive`, `/workspace/hooks/{phase}`;
  - `/execution/v1/…` handoff and hydration transfers.
  - Authentication is an Ed25519 bearer derived from `ACH_SANDBOX_KEY` and bound to the Workspace name, plus the instance ID header.
- **Execution to Harness.** Only the facades, under `infrastructure.facadeEndpoint`, through HMAC-signed URL tokens bound to the Workspace. A facade request is honoured only while that Workspace has a serving instance in the Harness's own state.
- **No outbound call from execution to control.** `controlEndpoint` is not used.

## 7. Configuration document changes

The schema is the v1 `workspace-v1` schema, with these changes:

| Field | Change |
|---|---|
| `infrastructure.control.serviceAccount` | Removed: a host concern. |
| `infrastructure.execution.image`, `serviceAccount`, `resources`, `ephemeralStorage`, `terminationGracePeriodSeconds`, `imagePullSecrets`, `nodeSelector`, `tolerations` | Removed: host concerns. The host reads them from its own agent definition. |
| `infrastructure.execution.controlEndpoint` | Removed: unused by the runtime. |
| `infrastructure.execution.facadeEndpoint` | Moves to `infrastructure.facadeEndpoint`. Required for `control`; optional for `agent`, where it defaults to loopback. |
| `agent.namespace`, `agent.uid` | Same shape, host-neutral meaning (3.1). The rule that the execution service account equals `ach-execution-<uid>` is dropped. |
| Bootstrap document | Becomes `bootstrapVersion: "workspace-v2"`, and `controlEndpoint` is removed. The runtime produces it inside `launch.env` as `ACH_BOOTSTRAP_JSON` (5.5); the host no longer renders it. |
| `workspace.readyTimeoutSeconds` | New, optional, default 30. How long a new execution unit has to become ready (placement, health probe) before activation fails; also bounds adoption and recovery starts. Hosts with slow starts (sandboxes that pull images) raise it. |

These changes ship as `schemaVersion: "workspace-v2"` (D1).

## 8. Amendments to v1

| v1 | Amendment |
|---|---|
| §1 | Replaced by sections 1–2. The Harness no longer creates infrastructure objects. The deferred "Workspace CR" becomes one possible host implementation of section 5, not a runtime concern. |
| §3, restart | "The Harness rediscovers its StatefulSets" becomes: the control unit adopts the units the host reports in its inventory after `runtimeEpoch` changes (5.3, 5.4). "Retry of Start only on the same pod UID" becomes the same instance ID. |
| §5, delivery | "The operator mounts the JSON at `/etc/ach-runtime/config.json`" becomes: the host delivers the configuration document at the `--config` path (3.1). The ConfigMap, subPath and hot-reload remarks, and template-hash rollout of execution StatefulSets, become host concerns. The runtime only requires that a unit's release matches its control unit's. |
| Run outcomes | A run on a lost instance whose outcome was not confirmed is `unknown`, including when its Workspace was deleted. Today's code records `failed` in that case; v2 changes it (5.6). |
| §7, recovery | "If health fails, the pod is deleted and another is created" becomes: the runtime lists the Workspace as `released`, waits for the host's `DELETE`, then lists it as `running` again for a fresh unit. Termination is confirmed before a new writer is used. `writer_pod_uid` becomes the writer instance ID (5.6). |
| §5, "there is no mode without ACH" | Replaced. The runtime needs an ACH-compatible API (3.3.1), which may be a stand-in. |
| §7, storage | The `ACH_STORAGE_S3_*` and credential rules stay as they are, as host-provided environment (3.2). |
| §11 | Replaced by sections 2–7. Naming, labels, RBAC, service accounts, ConfigMaps, downward API and service DNS are host concerns. |

## 9. Decisions

| ID | Question | Proposal |
|---|---|---|
| D1 | Should removing the infrastructure fields bump the schema to `workspace-v2`, or should v1 tolerate and ignore them during migration? | **Decided 2026-10-26:** bump to `workspace-v2`. The vendored schema and drift test in the operator move together with it. |
| D2 | Who owns the instance identity? | **Decided 2026-10-26:** the execution process mints it (5.6). |
| D3 | Where does the host API live? | **Decided 2026-10-26:** `control` `8081` under `/host/v1`. |
| D4 | Pull or push for Workspace demand? | **Decided 2026-10-26: pull with long-poll**, returning full desired state each time (level-triggered). The runtime never calls the host. Plain periodic polling is an allowed degraded form. |
| D5 | Is the single-unit topology allowed in production? | **Decided 2026-10-26: no.** It is for running outside Kubernetes (laptop, test or pre-production machine, CI); production always uses split, on Kubernetes. In a single unit the agent's tools can read the unit's secrets: through the shared user they can reach `/proc/<pid>/environ` and the loopback listeners. Isolating the whole unit protects the outside world from the unit, but not the unit's secrets from its own tools. |
| D6 | Is the ACH platform a mandatory dependency? | **Decided 2026-10-26: no.** The runtime depends on the ACH-compatible API in 3.3.1, not on the ACH product, just as it depends on S3-compatible storage rather than AWS. A host may provide the real platform or a stand-in. |
| D7 | How should short host name limits be handled? | **Decided 2026-10-26:** the host maps runtime names to its own identifiers (5.5); the runtime does not shorten them. |
| D8 | Does the runtime include a launcher that starts units (process, Docker, Kubernetes, OpenShell)? | **Decided 2026-10-27: no.** Infrastructure credentials would sit beside internet-facing ingress, and a launcher cannot tell a deleted agent from a restarting one, so its cleanup has no owner. Outside Kubernetes the `agent` role runs everything in one process. A launcher can be added later as another host implementing section 5, without changing this contract. |
