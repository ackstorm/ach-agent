# ACH operator handoff: deploy the three-role agent

## Agreed boundary

ACH should remain a simple Kubernetes operator. It creates the workload, mounts
configuration and storage, supplies environment/Secret references, and exposes the
agent. **ach-agent owns its internal startup, hydration, configuration generation,
communication defaults and process supervision.**

This document is self-contained for an engineer on another host. It describes the
agreed integration target, not a claim that the currently available image already
implements every detail.

## 1. Immediate topology

Keep one logical ACHAgent and its existing AgentProfile.

Render **one Deployment with one replica, containing one pod with three ordinary
containers**:

```text
Deployment: agent
  replicas: 1
  Pod
    channels
    harness
    engine
```

This means three containers, not three pods. They share the pod network namespace.
There is no hydration initContainer, application Kubernetes RBAC, internal queue,
KEDA configuration or new per-component CR in this increment.

The same compatible ach-agent image can serve all three roles. Keep existing image
selection unless a concrete packaging requirement makes a change necessary.
Do not assume that new image tags have already been published.

## 2. Image and role selection

The target image contract is:

```dockerfile
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ach_agent.main"]
```

The operator selects each role with Kubernetes `args`:

```yaml
containers:
  - name: channels
    image: <compatible-ach-agent-image>
    args: ["--role", "channels"]
  - name: harness
    image: <compatible-ach-agent-image>
    args: ["--role", "harness"]
  - name: engine
    image: <compatible-ach-agent-image>
    args: ["--role", "engine"]
```

Do not override `command` when using this entrypoint contract: Kubernetes `command`
replaces the image entrypoint and would bypass tini.

Tini is a small PID 1 process included in the image. It forwards signals and reaps
orphaned child processes. It is not another container or a Kubernetes initContainer.
Packaging it and supervising native engines are ach-agent responsibilities.

## 3. Configuration and environment

### Existing agent configuration

Continue rendering the existing agent runtime configuration. The operator supplies
its location using **ACH_CONFIG_PATH** to the harness, which owns the full config.
The full configuration must not be mounted into the engine container.

The operator does not generate channels/engine projection schemas, perform
hydration, resolve native session mappings or configure model/MCP proxy URLs.
The harness derives internal bootstrap artifacts from the full configuration;
other roles wait for and consume their permitted artifacts.

The ach-agent team owns the exact internal format and startup protocol. The
operator only mounts the directories defined in the storage contract below.

### Default internal settings

The operator must not be required to inject these implementation settings:

- ACH_CHANNELS_CONFIG_PATH or ACH_ENGINE_CONFIG_PATH;
- ACH_HARNESS_URL or ACH_ENGINE_URL;
- ACH_CHANNELS_HOST/PORT, ACH_HARNESS_HOST/PORT or ACH_ENGINE_HOST/PORT;
- an independently configured internal agent name;
- an operator-generated channels/harness HMAC key.

ach-agent provides defaults and derives identity from its configuration. Where
supported, environment overrides remain available for deliberate customization.
They are not mandatory boilerplate for the ordinary deployment.

Continue supplying existing integration environment and Secret references, such as
ACH_BASE_URL and ACH_TOKEN, to the harness. Preserve existing required metadata,
such as POD_NAMESPACE when used for memory identity.

### Preserve env and engine.forwardEnv

Do not introduce channelsEnv, engineEnv or a new public environment structure.
Keep the current profile/agent env merging and the existing forwarding fields:

```yaml
spec:
  env:
    - name: DEBUG
      value: "1"
    - name: CUSTOM_TOOL_TOKEN
      valueFrom:
        secretKeyRef:
          name: custom-tool
          key: token
  engine:
    forwardEnv:
      - DEBUG
      - CUSTOM_TOOL_TOKEN
```

Responsibilities:

- ACH resolves the existing EnvVar definitions and preserves their current merge rules.
- The engine container receives definitions selected by engine.forwardEnv.
- A secretKeyRef stays a Kubernetes reference, not an inline secret in a ConfigMap.
- The mini-harness reads selected values from its own environment and forwards them
  to Pi/OpenCode. The harness does not serialize those values over its execution API.
- prepare.forwardEnv remains independent: preparation-only selection does not expose
  a variable to the engine.
- Explicitly selecting an operator-provided secret for engine forwarding is supported
  and means deliberate exposure to the engine.
- Managed ACH credentials and internal authentication material are never forwarded.

Do not clone the complete combined environment into all three containers. Source
credentials belong to channels; preparation and capability credentials belong to
harness. Preserve existing supported passthrough MCP environment behavior when
mapping engine variables. Surface an unresolved routing ambiguity rather than
silently dropping a variable or widening its exposure.

If an operator supplies an optional role-specific runtime override through existing
env, route it to the relevant role. The ach-agent integration contract will identify
such reserved runtime settings; no topology-specific env blocks are required now.

## 4. Storage and access boundaries

Kubernetes mounts are the operator's responsibility; the contents and bootstrap
protocol inside them are ach-agent's responsibility.

| Storage purpose | Access |
| --- | --- |
| Existing full runtime config | Harness only, read-only |
| Harness persistent state and preparation scratch | Harness only |
| Channels bootstrap and internal channels/harness authentication material | Harness writes; channels reads; engine has no mount |
| Engine public bootstrap and hydrated public context | Harness writes; engine reads |
| Workspace | Harness and engine, read-write |
| Engine home and native session state | Engine only, read-write |

The channels bootstrap contains only the data channels needs. The engine bootstrap
contains no managed secret values. Internal authentication material, if retained,
is generated and managed by ach-agent in the channels/harness-only directory.
It must never be placed in the general workspace or public bootstrap directory.

Prefer the existing persistence fields and narrow mounts; do not add new CR fields
just to expose these internal directories. Ephemeral bootstrap/scratch can use
emptyDir. Persistent state can use separate existing-PVC subdirectories with
container-specific mounts where supported.

The ach-agent team will supply the final fixed paths, directory ownership and
mount example before integration. ACH should consume that small filesystem contract,
not implement the internal artifact formats. Do not invent paths or silently
broaden mounts in the meantime.

Preserve existing workspace, session and memory data during rollout. Native session
mapping import and bootstrap migration logic belong to ach-agent. Kubernetes-level
storage relocation or provisioning belongs to ACH/deployment tooling. Coordinate
any physical data movement required by existing PVC layouts; do not replace existing
data with empty directories or give engine access to harness state to avoid migration.

## 5. Network, Service, probes and lifecycle

- The three containers share pod networking; harness/engine communication uses
  localhost by default.
- The public Service and gateway address retain their identity and point to channels.
- No Service is needed for the engine or the harness's private proxies in this topology.
- The normal channels ingress port is 8080. Internal role ports and discovery are
  ach-agent defaults; the operator need not set corresponding environment variables.
- ach-agent supplies a fixed, documented health/readiness probe contract for each
  role. ACH installs those probes and derives status from the workload.
- The operator supplies resources, scheduling, termination grace and security settings.
  ach-agent handles startup waiting, readiness transitions and orderly process cleanup.
- Keep a single active execution instance during updates. Inspect update strategy:
  one desired replica does not by itself prevent rolling-update overlap.
- Hash relevant configuration/image inputs so changes trigger the expected rollout.

Use restricted container security and narrowly scoped mounts. Disable automatic
service-account tokens and avoid host networking, host PID/IPC, shared process
namespaces, privileged containers and runtime sockets. No application role needs
Kubernetes API access.

Existing pod-level network policy remains applicable. This split does not promise
per-container egress isolation.

## 6. Public schema delta

The split should not require changes to the functional agent configuration.
The implementation already contains one optional public schema addition for bounded
result recovery:

```yaml
limits:
  resultRetentionSeconds: 300
```

Exact property to add under the existing LimitsBlock.properties:

```json
{
  "resultRetentionSeconds": {
    "default": 300,
    "exclusiveMinimum": 0,
    "maximum": 86400,
    "title": "Resultretentionseconds",
    "type": "integer"
  }
}
```

Do not add it to the required list. schemaVersion stays "1". Synchronize ACH's
schema fixture, but exposing a new CR setting is unnecessary for this increment:
omission uses the default. engine.forwardEnv already exists and remains supported.

## 7. Future direction: two Deployments, execution can scale to zero

Document this direction without implementing it now:

```text
One ACHAgent
  |- channels Deployment: 1 replica
  `- execution Deployment: harness + engine, 0 <-> 1 replica

Public Service -> channels -> durable internal transport -> harness + engine
```

This is not a channels-only agent: the full agent remains defined, while its
execution unit can be inactive. Harness and engine stay colocated with shared
workspace and localhost. Channels does not need the execution PVC.

Deployment policy should live in AgentProfile when an alternative is implemented.
A future deployment.mode: onDemand is only a candidate interface, not a field to
add or accept today.

The ach-agent side owns the future ability to accept durable work while execution
is absent, consume that work and return correlated results. The scaling integration
must account for queued and running work. KEDA or another selected autoscaler would
own execution replica changes; ACH must not overwrite them on each reconcile.
Start with maximum one execution replica to preserve ordering/session assumptions.
Expected inactivity must be distinguishable from failure in observed status.

This future is not achieved by setting replicas to zero on today's implementation.
Do not implement the queue, autoscaler or S3 persistence as part of this handoff.

## 8. Pending work in ach-agent — not delegated to ACH

The existing split snapshot was validated on an unpublished local branch, but its
operator contract was too demanding. The following adjustments are now agreed and
must land on the ach-agent side before the simplified integration is complete:

1. Restore engine.forwardEnv in split mode, selecting names and resolving values
   in the engine container rather than transferring harness environment values.
2. Make the image own tini and the role entrypoint.
3. Provide internal connection/path defaults so role-specific environment boilerplate
   is unnecessary for ordinary deployments.
4. Let harness generate the channels/engine bootstrap artifacts from the full config;
   make each role wait for its appropriate bootstrap without operator projections.
5. Own internal channels/harness authentication bootstrap without requiring ACH to
   generate a dedicated shared key.
6. Deliver exact volume paths/permissions and probe instructions as a small deployment
   contract, with upgrade behavior and compatible images tested.

Do not treat old snapshot behavior rejecting forwardEnv or requiring hand-generated
role configurations as the desired contract. Do not work around it in the operator.

## 9. Requested ACH work and integration acceptance

Inspect your renderer and propose the minimal Kubernetes delta for this contract.
You can prepare the Deployment/Service/env/mount changes while the ach-agent side
finishes its defaults/bootstrap work. Before final integration obtain the compatible
image and its small mount/probe contract; no local repository access is assumed.

Acceptance:

- Existing ACHAgent/AgentProfile configuration still works without new role-env sections.
- One pod runs three ordinary containers using the image entrypoint and role args.
- The operator supplies the existing full config, not handcrafted internal projections.
- Explicit forwardEnv values reach the native engine; managed and preparation-only
  credentials do not.
- Public ingress remains stable and reaches channels.
- Private/shared mounts enforce the intended boundaries.
- Sessions, workspace and memory survive the documented rollout path.
- Probes, status and rollout behavior reflect the three components correctly.
- A real invocation succeeds against each supported engine using the delivered image.

Keep ACH concerned with Kubernetes resources. Internal agent protocols, native engine
configuration, hydration, bootstrap generation and process supervision belong to
ach-agent.
