# ach-agent: design (spec 1 of 2)

Date: 2026-10-29. Status: approved in brainstorming, revised after a Codex review, pending
written review.

Spec 2 (separate) covers migration of the production agents and removal of ACHAgent,
AgentProfile and the OpenShell backend from ach.

## 1. Goal

One project, **ach-agent**, owns agents end to end: CRDs, operator and runtime. It runs on its
own by default, with models and MCP servers configured directly (e.g. OpenRouter, LiteLLM,
Serper). ACH is an optional capability server. OpenShell is the only execution sandbox, and a
service mesh (Istio ambient) is required.

Non-goals for v1: a2a peers and environment content (skills, plugins, prompts) without ACH;
extra non-ACH MCP servers in ACH mode; an in-process (`none`) sandbox; gateway auth without a
mesh (`mtls`); OpenShell rule approval, binary SHA-256 pinning, policy prover, OCSF export;
gateway high availability; per-endpoint facade bypass.

## 2. Repository and artifacts

- New public repo `github.com/ackstorm/ach-agent`, local path `../ach-agent`, MIT licence.
- One repo, two languages, one version (starts at `0.1.0`), one chart:

```
ach-agent/
  operator/                    Go module
    api/v1alpha1/              Agent, AgentProfile (group ach-agent.ackstorm.ai)
    internal/controller/       reconcile Agent/AgentProfile
    internal/render/           Agent + AgentProfile -> runtime config (ConfigMap)
    internal/backend/openshell sandbox policy merge, gateway client, per-sandbox Services
  runtime/                     Python, package ach_runtime
  chart/                       operator + CRDs + RBAC
  test/e2e/                    kind + real OpenShell gateway
  docs/
```

- Images: `ghcr.io/ackstorm/ach-agent-operator`, `ach-agent-control` (harness and facades),
  `ach-agent-execution` (adds node and OpenCode). Chart: `oci://ghcr.io/ackstorm/charts/ach-agent`.
  All public.
- Bootstrap: plain copy, one import commit per source naming the source SHA, no history.
  - From ach-runtime (current main): the whole runtime.
  - From ach (v0.14.2): ACHAgent/AgentProfile controller, `internal/agentrender`,
    `internal/workspacehost` (OpenShell backend), the workspace key Secret.
  - Not imported: StatefulSet execution backend, the gateway Postgres projection
    (`expose.gateway`, `ach_achagents_changed`), legacy cleanup code.
- The operator-runtime contract (config document + `/host/v1`) is internal to the repo and
  ships with one version. The config schema becomes `workspace-v3`. The rendered config stays
  in a ConfigMap: it holds env names, never credential values.

## 3. Runtime topology per Agent

- **Control unit:** a one-replica StatefulSet running the harness, channels and facades
  (model, MCP, memory, artifacts). It holds every credential, from Secrets wired as env vars.
  A StatefulSet, not a Deployment: the runtime needs a single coordinator, and its
  replacement never overlaps the old pod. `terminationGracePeriodSeconds` is at least the
  runtime drain timeout plus 15 seconds; the operator rejects a smaller value.
- **Execution units:** OpenShell sandboxes, one per execution unit.
- **Host API:** the runtime serves `/host/v1` on port 8081; the operator polls it for desired
  units and reports their state back. A private headless Service on 8081 with
  `publishNotReadyAddresses: true` exists for every Agent, so the operator reaches the
  inventory before the pod is ready. The operator finds the pod as `<statefulset>-0`.
- **Flow:** trigger on a channel -> the runtime publishes a desired unit on `/host/v1` -> the
  operator creates the sandbox with the merged policy -> the sandbox reaches only the facades
  -> the facades call the model and MCP servers (standalone endpoints or ACH).
- **Exposure:** `expose.service` creates a ClusterIP Service on 8080 (channels, a2a);
  `expose.route` additionally creates a Gateway API `HTTPRoute`. Inbound security is each
  channel's own auth (webhook `hmac` / `gitlab_token` / `header_token`, a2a API-key header).

### Runtime does not touch Kubernetes

The runtime never calls the Kubernetes API or the OpenShell gateway API. The operator is the
only client of both: it polls `/host/v1`, creates and deletes sandboxes, wires Secrets into
env and rolls the pod when they change. The runtime learns cluster state only from `/host/v1`
(e.g. gateway availability). Control -> sandbox requests cross the gateway as network routing
(ExternalName Service), not as API calls. This keeps the runtime runnable without Kubernetes
(compose, tests) and leaves a compromised control unit no cluster access.

### Trust boundary

- Agent authors are trusted control-code authors. Handoff and webhook scripts, hooks and
  channel code run in the control unit with the Secrets they select, outside OpenShell.
  Creating or editing an Agent is therefore as privileged as reading the Secrets it can
  reference in its namespace; the chart's RBAC docs say so.
- OpenShell confines only what runs in the sandbox: the engine, its tools and the model's
  actions.

### Workspace key

- The operator creates one Secret per Agent, `ach-agent-key-<uid>`, holding 32 random bytes
  (hex), controlled by the Agent (ownerReference) and never rewritten. It authenticates
  `/host/v1` and facade calls (HMAC), and the runtime derives the Ed25519 key pair that
  signs execution tokens from it.
- Only the control unit mounts it. A sandbox receives only the public verification key and
  its own scoped workspace token.
- A Secret with the name but another controller fails reconciliation (`SecretConflict`).
  Rotation: delete the Secret; the operator creates a new one and rolls the control unit.

### Hardening

- Control and sandbox pods run as non-root, no privilege escalation, all capabilities
  dropped, read-only root filesystem, writable `emptyDir` mounts only where the runtime
  writes (`/tmp`, the workspace root).
- Runtime ServiceAccounts have no Kubernetes permissions and
  `automountServiceAccountToken: false`.
- Operator RBAC: Agents, AgentProfiles and their status; StatefulSets, Services, ConfigMaps,
  Secrets (create the key Secret, read referenced Secrets) in agent namespaces; Pods
  read-only in agent namespaces and the sandbox namespace; HTTPRoutes when `expose.route`
  is used; Events.

## 4. Resources (`ach-agent.ackstorm.ai/v1alpha1`)

### AgentProfile (shared infrastructure)

```yaml
kind: AgentProfile
metadata: {name: default}
spec:
  control:
    image: ghcr.io/ackstorm/ach-agent-control:0.1.0
    serviceAccountName: ach-agent-control
    resources: {...}
    nodeSelector: {...}
    tolerations: [...]
    imagePullSecrets: [...]
    env: [...]
    terminationGracePeriodSeconds: 60
    persistence: {enabled, size, storageClassName, mountPath, retainPolicy}   # control PVC
  sandbox:
    openshell:                      # the only sandbox; a future one is a sibling block
      image: ghcr.io/ackstorm/ach-agent-execution:0.1.0
      resources: {cpu: "2", memory: 4Gi, ephemeralStorage: 2Gi}
      readyTimeoutSeconds: 90
      policy: {...}                 # OpenShell sandbox policy, passthrough
  defaults: {...}                   # Agent defaults: limits, workspace, engine, artifacts
```

- `sandbox.openshell` is required. There is no `type` field: the block name selects the
  sandbox, and another sandbox later becomes a sibling block with a CEL rule that exactly
  one is set.
- `sandbox.openshell.resources` maps to the OpenShell SDK's sandbox resource fields; a
  resource the SDK cannot express is rejected (`SandboxResourcesUnsupported`), never silently dropped.
  `readyTimeoutSeconds` bounds the operator's wait and is rendered into the runtime's unit
  startup budget.
- `control.persistence` is the control unit's PVC. Workspace snapshots, session snapshots
  and artifacts live in S3, configured once in the chart (`runtime.storage.s3`: bucket,
  region, endpoint, prefix, optional credentials Secret), as today. An Agent that enables
  any of them while the chart has no bucket gets `Ready=False`, reason `StorageUnavailable`.

### Agent (behaviour)

```yaml
kind: Agent
metadata: {name: researcher}
spec:
  profileRef: {name: default}
  capabilityServer: standalone      # standalone (default) | ach
  standalone:                       # required with standalone, forbidden with ach
    model:
      name: anthropic/claude-sonnet-5.5
      api: openai                   # openai | anthropic | gemini: wire format only
      baseUrl: https://litellm.example.com/v1
      headers:
        x-litellm-api-key: {secretKeyRef: {name: litellm, key: apiKey}}
      params: {...}
      thinking: {...}
      limits: {context: 200000, output: 32000}   # optional
    mcpServers:
      - name: serper
        url: https://mcp.serper.dev/mcp
        headers:
          X-API-KEY: {secretKeyRef: {name: serper, key: apiKey}}
  ach:                              # required with ach, forbidden with standalone
    baseUrl: https://api.ackstorm.ai
    environment: prod
    keySecretRef: {name: researcher-ek, key: token}
    model: {name: ackstorm.smart, api: openai, params: {...}, thinking: {...}}
  filter: {exclude: {mcpServers: [...], tools: [...], skills: [...]}}   # both modes
  memory: {...}                     # see "Memory" below
  prompt: {...}
  engine: {...}
  limits: {...}
  workspace: {...}
  hooks: {...}
  artifacts: {...}
  channels: [...]
  env: [...]
  sandbox:
    openshell:
      policy: {...}                 # merged over the profile's policy
  expose:
    service: true
    route:                          # optional; requires service
      hostname: researcher.example.com
      path: /
      parentRef: {...}              # default from chart values
status:
  conditions: [Ready, GatewayAvailable]
```

- Header values are `secretKeyRef` or a literal `value`. There are no default headers per
  `api`: the user states every header (e.g. LiteLLM needs `x-litellm-api-key`). Header names
  are case-insensitive and unique per endpoint. Reserved names are rejected: `host`,
  `content-length`, `transfer-encoding`, `connection`, `x-ach-*`, and the runtime's
  correlation headers.
- `ach.model.api` defaults to `openai`; AgentProfile `defaults` never set `api` or `name`.
- `filter.exclude.skills` is accepted only with `ach` (standalone has no skills).
- CEL rules: `capabilityServer` selects `standalone` or `ach` (named block required, the other
  forbidden); `expose.route` requires `expose.service`.
- Status reasons include `SandboxPolicyRejected`, `SandboxResourcesUnsupported`,
  `GatewayUnavailable`, `SecretMissing`, `SecretConflict`, `StorageUnavailable`.

### Field mapping from ACHAgent v0.14.2

| ACHAgent | Agent |
|---|---|
| `ach.baseUrl`, `ach.environment` | `ach.baseUrl`, `ach.environment` |
| `ach.identity.secretRef` | `ach.keySecretRef` |
| `ach.capability.environment` | removed (one environment per Agent) |
| `ach.capability.filter` | `filter` (both modes) |
| `model{name, type, params, thinking}` | `ach.model{name, api, params, thinking}` or `standalone.model` |
| `mcpServers` (never rendered) | removed; `standalone.mcpServers` |
| `expose.gateway` | removed |
| `expose.service` | `expose.service`; `expose.route` added |
| `image` | removed (profile `control.image`) |
| `health{host, port}` | removed (fixed 8080 / 8081) |
| `memory`, `prompt`, `engine`, `limits`, `workspace`, `hooks`, `artifacts`, `channels`, `env`, `profileRef` | unchanged, same validation |

| AgentProfile v0.14.2 | AgentProfile |
|---|---|
| `controlServiceAccountName`, `resources`, `nodeSelector`, `tolerations`, `imagePullSecrets`, `env`, `terminationGracePeriodSeconds`, `persistence` | under `control` |
| `execution{image, resources, ephemeralStorage, readyTimeoutSeconds}` | `sandbox.openshell{image, resources, readyTimeoutSeconds}` |
| `execution.backend` | removed (always OpenShell) |
| `execution{nodeSelector, tolerations, imagePullSecrets, terminationGracePeriodSeconds}` | removed (OpenShell places sandboxes) |
| `networkPolicy.egress` | removed (sandbox egress is the OpenShell policy) |
| `podTemplate` | removed |
| `achagent{ach, artifacts, engine, health, image, limits, model, workspace}` | `defaults{artifacts, engine, limits, workspace}`; `ach`, `model`, `image`, `health` removed |

Unchanged fields keep today's validation, including handoff/session-concurrency and timeout
constraints. Parity tests render one representative ACHAgent and its Agent equivalent and
compare the runtime config, excluding the `capability` block.

## 5. Runtime

### Config `workspace-v3`

The private config replaces the `ach` block and root `model` of `workspace-v2` with a
`capability` block. Secrets are always env names; the operator wires values into the control
pod with `secretKeyRef`. No credential reaches a sandbox.

```json
"capability": {
  "server": "standalone",
  "standalone": {
    "model": {"name": "...", "type": "openai", "baseUrl": "...",
              "headers": {"x-litellm-api-key": {"env": "ACH_AGENT_MODEL_H0"}},
              "params": {}, "limits": {"context": 200000, "output": 32000}},
    "mcpServers": [{"name": "serper", "url": "...",
                    "headers": {"X-API-KEY": {"env": "ACH_AGENT_MCP_SERPER_H0"}}}]
  },
  "filter": {"exclude": {"mcpServers": [], "tools": [], "skills": []}}
}
```

With `"server": "ach"`: `"ach": {"baseUrl", "environment", "key": {"env"}, "model"}`. The
CRD's `api` renders into the runtime's existing `model.type`.

- The private schema (control unit only) and the public projection (what a sandbox can read)
  are separate types. The public projection keeps the existing `public_config` shape: model
  name and `api`, metadata, filters. It never carries `baseUrl`, MCP URLs or header entries.
- `Hydrator` and the standalone source take one normalized input (model name, `api`, filter,
  and for ACH the base URL, environment and key env) instead of reading the config tree.
- `identity` (`agent`, `environment`) for metrics, run records and alarms comes from
  `agent.name` plus `ach.environment`, or the Agent's namespace in standalone. `/metrics`
  is always mounted.

### Catalog sources

One catalog interface, two implementations:

- `ach`: the existing `Hydrator` (`POST /platform/hydrate`, `GET /v2/model/info`) on the
  normalized input.
- `standalone`: built from the config. Model metadata comes from `limits` when given. Without
  `limits`, the managed-config plugin's fallback applies: context 128000, output 8192, text
  modalities, cost reported as unknown (not zero). Skills, prompts, artifacts and a2a peers
  are empty.

### Facades

- Upstream credentials are per upstream: ACH injects `x-ach-key: <ek>`; standalone injects
  each endpoint's configured headers. `_proxy.forward` removes every inbound header whose
  name matches an injected one case-insensitively, then sets exactly one value. Credentials
  are kept outside the catalog, which the sandbox can read.
- The model facade enforces the configured model and an allow-list per `api`. Anything else
  gets 4xx with zero upstream egress:

  | `api` | Allowed (relative to `baseUrl`) | Model taken from |
  |---|---|---|
  | `openai` | `POST chat/completions`, `POST responses` | body `model` |
  | `anthropic` | `POST messages`, `POST messages/count_tokens` | body `model` |
  | `gemini` | `POST models/{m}:generateContent`, `:streamGenerateContent`, `:countTokens` | path `{m}` |

  Rejected with 400: a non-JSON or compressed (`Content-Encoding` other than identity) body,
  a missing model, a model other than the configured name, percent-encoded or `.`/`..` path
  segments, a query string other than gemini's `alt=sse`. The e2e records every path OpenCode
  sends, so a missing entry fails a test rather than a run.
- MCP per-tool exclusion, artifacts and correlation are unchanged and read `filter` in both
  modes.

### Memory

- Memory keeps today's `memory.achMemory` shape: `endpoint` or `mcpServerId`, and `auth`.
- With `ach`: unchanged (`auth.type: ach` sends the ek; `mcpServerId` names a hydrated
  server).
- With `standalone`: `auth.type: ach` is rejected; `auth.type: bearer` reads its own
  `secretKeyRef`; `mcpServerId` names a `standalone.mcpServers` entry. Memory auth never
  inherits that entry's headers.
- In both modes every MCP server whose endpoint equals the memory endpoint is excluded from
  the MCP facade, so the scope the memory facade injects cannot be bypassed.

### Engine

`engine/opencode.py` writes the OpenCode provider from `api`; its URL is always the facade.
The engine never sees the real `baseUrl` or any header value.

### Errors

- `ach`: missing env or unreachable ACH -> `HydrationError`, fail closed (as today).
- `standalone`: no startup round-trip; the first upstream failure is the run's error.
- Run-record redaction covers every configured header value (read from env at startup), not
  only the `ek_` / `sk-` patterns.

## 6. OpenShell integration

### Prerequisite

The OpenShell gateway is installed separately (NVIDIA chart, own namespace) inside an Istio
ambient mesh. The ach-agent chart takes `openshell.gateway` (URL) and documents a working
install: operator and control pods labelled `istio.io/dataplane-mode: ambient`, the gateway
and sandbox namespaces in the mesh, a STRICT PeerAuthentication and an AuthorizationPolicy
admitting only the ach-agent operator and control ServiceAccounts. The same mesh protects
`/host/v1` and sandbox -> facade traffic. Without a mesh the chart refuses to install
(`openshell.mesh: istio-ambient` is required).

### Policy per sandbox

1. Start from the profile's `sandbox.openshell.policy`.
2. Merge the Agent's: `network_policies` by name (Agent wins), `filesystem_policy` lists
   concatenated, scalars (e.g. `landlock`) Agent over profile. Extra egress and Landlock
   settings are the Agent author's choice (see "Trust boundary").
3. Apply the reserved `ach-agent-*` rules last; they cannot be overridden:
   - egress to the facades, bound to the runtime binaries;
   - the runtime binaries and their directories read-only: an Agent `filesystem_policy`
     entry that makes any of them writable is rejected (`SandboxPolicyRejected`), so a
     sandboxed process cannot replace a binary that other rules trust;
   - the runtime's base read-write paths (workspace root, `/tmp`).
4. `landlock.compatibility` defaults to `hard_requirement` when nobody sets it.
5. Decode strictly into the OpenShell Go SDK type (unknown fields rejected). A gateway
   rejection sets `Ready=False`, reason `SandboxPolicyRejected`, with the gateway message.

New OpenShell policy fields need an SDK bump, not a CRD change.

### Lifecycle

- One sandbox per execution unit, `RestartPolicy: Never`, a per-sandbox ExternalName Service
  to the gateway, `unitLost` -> `DELETE .../instance`.
- Deletion: the finalizer first stops new creates for the Agent and waits for in-flight
  creates to finish, then lists sandboxes and Services and deletes them. The finalizer stays
  while the gateway is unconfigured or does not answer, or a delete is unconfirmed; it is
  removed only after an inventory shows nothing left.

### Gateway unavailable

Control -> sandbox traffic goes through the gateway (Host routing via the ExternalName
Service), so a gateway outage stops invocations even though sandboxes keep running. The host
API carries the operator's view: `PUT /host/v1/gateway {"available": bool}`, sent on every
change and after each adoption inventory.

- Operator: gRPC keepalive; the client is rebuilt after a transport error (fixes the observed
  "connection reset by peer" that lasted until the operator restarted); condition
  `GatewayAvailable=False`; `available: false` sent on `/host/v1/gateway`; no
  sandbox is deleted or reported lost (never confirm an unconfirmed stop).
- Runtime, while the gateway is reported unavailable:
  - health-check failures do not trigger recovery, and failed adoption does not schedule
    retirement (adoption is retried when the gateway returns);
  - activation of a new unit keeps its startup budget and fails as today when it runs out;
  - delivery: a request that was never sent retries with backoff until its deadline, then
    fails with anomaly `gateway_unavailable`. Only connect failures count as never sent. A request that may have reached the sandbox is never
    replayed elsewhere: ownership stays with that unit, and the run ends with outcome `unknown`
    unless the same unit answers before the deadline (today's same-instance replay rule).
- No gateway HA in v1 (0.1.5 runs one pod with a PVC). Investigation item: whether the control
  unit can reach sandboxes pod to pod, leaving the gateway only for create and delete.

### Secret changes

The operator watches every Secret an Agent references: model and MCP headers, the ACH key,
memory and channel credentials, S3 credentials. A content hash of all of them is a pod
template annotation, so any change rolls the control unit. A missing Secret or key sets
`SecretMissing`.

## 7. Testing

- Operator (Go unit + envtest): policy merge (reserved rules win, writable runtime binaries
  rejected, lists concatenated, unknown fields rejected); the effective policy sent to the
  gateway for each case; CEL for `capabilityServer` and `expose.route`; reserved and duplicate
  header names; Secret -> env wiring and hash rollout; workspace key creation and conflict;
  HTTPRoute rendering; ACHAgent -> Agent parity; gateway unavailable -> condition set, units
  reported, nothing deleted; deletion racing a sandbox create -> nothing left, finalizer held
  until verified.
- Runtime (pytest): standalone catalog and metadata fallback; injected headers replace
  inbound ones case-insensitively and are absent from everything the engine sees; model
  allow-list per `api` (wrong model, wrong path, encoded path, compressed body -> 4xx, no
  upstream call); memory in standalone and alias exclusion; `/metrics` and redaction in
  standalone; gateway-unavailable handling for health, adoption, activation and delivery
  (unsent retried, ambiguous not replayed).
- e2e on its own kind cluster `ach-agent-e2e` (never the shared `ach-e2e`), Istio ambient,
  real OpenShell gateway, stub model and MCP servers that assert the received headers:
  1. standalone: invocation succeeds, headers arrive, sandbox reaches only the facades, every
     model path OpenCode sends is on the allow-list;
  2. `ach` mode against the ACH hydrate stub;
  3. rejected policy -> condition;
  4. gateway restart -> operator recovers without a restart, no sandbox replaced;
  5. gateway down during a control unit restart -> units adopted after the gateway returns;
  6. Agent deletion -> sandboxes and Services removed;
  7. Secret change -> control unit rolled, new header seen upstream.
- Local dev: `docker compose` with the existing `agent` image target (harness plus in-process
  `LocalSource`) and a stub model. Dev only: no CRD field selects it, and the image is not
  published.

## 8. Release

A `vX.Y.Z` tag on GitHub Actions builds and publishes the three images and the OCI chart.

## 9. Open items for later specs

- Facade optional per endpoint (`via: facade | sandbox`) with OpenShell provider credentials.
- Gateway auth without a mesh (`mtls`): TLS in every control and operator client, SAN and Host
  validation, protection for `/host/v1` and sandbox -> facade traffic.
- OpenShell rule approval flow, binary SHA-256 pinning, policy prover in CI, OCSF export.
- Gateway HA or direct control -> sandbox routing.
- a2a peers declared by URL in standalone mode.
