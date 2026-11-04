# Install

ach-agent needs, in order: an Istio ambient mesh, the agent-sandbox controller, an OpenShell gateway
behind a mesh policy, then the operator chart. All versions below are the tested ones.

## 1. Istio ambient 1.28.3

```bash
helm repo add istio https://istio-release.storage.googleapis.com/charts --force-update
helm upgrade --install istio-base istio/base --version 1.28.3 -n istio-system --create-namespace --wait
helm upgrade --install istiod istio/istiod --version 1.28.3 -n istio-system --set profile=ambient --wait
helm upgrade --install istio-cni istio/cni --version 1.28.3 -n istio-system \
  --set profile=ambient --set ambient.dnsCapture=false --wait
helm upgrade --install ztunnel istio/ztunnel --version 1.28.3 -n istio-system --wait
```

`ambient.dnsCapture=false` is required: ztunnel's DNS proxy answers the sandbox supervisor's TCP DNS
probe with a malformed reply, so sandboxes never become ready.

## 2. agent-sandbox v1.0.8

```bash
kubectl apply --server-side -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.8/sandbox-with-extensions.yaml
kubectl -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s
```

## 3. OpenShell 0.1.5

```bash
kubectl create namespace openshell
kubectl label namespace openshell istio.io/dataplane-mode=ambient
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.5 \
  -n openshell -f test/e2e/openshell-values.yaml --wait --timeout 6m
```

The values (`test/e2e/openshell-values.yaml`):

- `server.disableTls` and `server.auth.allowUnauthenticatedUsers`: the gateway speaks plaintext and
  trusts every caller. This is safe only because step 4 restricts who can reach it.
- `server.sandboxUid` / `sandboxGid` `10001`: the execution image runs as 10001, and OpenShell needs
  the agent and the sandbox to share one UID.
- `pkiInitJob.serverDnsNames`: a wildcard SAN `*.<agent namespace>.svc.cluster.local` per Agent
  namespace, so the gateway routes the per-sandbox ExternalName Services the operator creates.

## 4. Mesh policy

Replace `<AGENT_NAMESPACE>` in `docs/mesh/openshell-mesh.yaml` with each Agent namespace and apply it.
It enforces STRICT mTLS, admits only the operator ServiceAccount, the sandbox ServiceAccount and the
Agent namespaces to the gateway, and allows ambient HBONE (TCP 15008) through the NetworkPolicy.
Label every Agent namespace `istio.io/dataplane-mode=ambient`.

## 5. Operator

```bash
helm install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version 0.2.1 \
  -n ach-agent-system --create-namespace \
  --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080
```

The chart refuses to render unless `openshell.gateway` is set and `openshell.mesh` is `istio-ambient`.
It also installs a ValidatingAdmissionPolicy that limits the operator to Opaque Secrets named
`ach-agent-key-*` (`admissionPolicy.enabled=false` disables it).

### Storage server

With a bucket configured and `storage.enabled` (the default), the chart runs one storage server,
`ach-agent-storage` (SeaweedFS), in every namespace listed in `storage.namespaces`. Control pods in that
namespace save and restore workspaces and sessions through it; it keeps them on its volume and
replicates them to a real bucket in the background: AWS S3 (`storage.remote.type: s3`, the default) or
Google Cloud Storage (`gcs`). A save is done once the storage server has it.

```bash
helm upgrade --install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version <version> \
  -n ach-agent-system \
  --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080 \
  --set runtime.storage.s3.bucket=<bucket> --set runtime.storage.s3.region=<region> \
  --set 'storage.namespaces={ach-agent}'
```

| Value | Default | Notes |
|-------|---------|-------|
| `storage.enabled` | `true` | `false` keeps control pods on S3 directly, with their own credentials. Drain the storage server first (below). |
| `storage.namespaces` | `[]` | The Agent namespaces; required when a bucket is set. An Agent elsewhere with persistence or artifacts enabled waits for the Secret `ach-agent-storage-s3`. |
| `storage.remote.type` | `s3` | `s3` replicates to `runtime.storage.s3.bucket`; `gcs` to `storage.remote.gcs.bucket`. |
| `storage.remote.gcs.{bucket,project,prefix,credentialsSecret}` | `prefix: ach` | GCS only; `project` is required. `credentialsSecret` names an existing Secret with the key `credentials.json` (a service account key); leave it empty for Workload Identity. |
| `storage.serviceAccount.annotations` | `{}` | Annotations on the ServiceAccount `ach-agent-storage`, for example `iam.gke.io/gcp-service-account` for GKE Workload Identity. |
| `storage.size`, `storage.storageClass` | `50Gi`, cluster default | A cache of the bucket plus what S3 does not have yet: size it for the backlog of an S3 outage. A StatefulSet cannot resize its claim; grow the PVC by hand. |
| `storage.evictThreshold` | `0.8` | Above this disk usage the server drops data already in S3, oldest first; a later read fetches it again. Data not yet in S3 is never dropped. |
| `storage.image` | SeaweedFS 4.51, pinned by digest | 4.51 or newer: 4.48 never marks objects written through the server as replicated, so it never evicts them. Run `make storage-smoke` before changing it. |

- **S3.** The storage server replicates to `runtime.storage.s3.bucket` in `region`, at `endpointUrl`
  (default `https://s3.<region>.amazonaws.com`). It is the only component with AWS credentials, from the
  default AWS chain: on EKS, one Pod Identity association per namespace for the ServiceAccount
  `ach-agent-storage`, with the bucket policy the control ServiceAccount had; remove the association on the
  control ServiceAccount after the switch. `runtime.storage.s3.credentialsSecret` instead names a Secret
  with `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` (optionally `AWS_SESSION_TOKEN`) in each listed
  namespace.
- **GCS.** With `storage.remote.type=gcs` the storage server replicates to `storage.remote.gcs.bucket`
  with Application Default Credentials: on GKE, Workload Identity, binding a Google service account with
  access to the bucket to `<namespace>/ach-agent-storage` and setting
  `storage.serviceAccount.annotations."iam.gke.io/gcp-service-account"`; elsewhere, the key Secret in
  `storage.remote.gcs.credentialsSecret`. Control pods still speak S3 to the storage server, which names
  its bucket after the GCS bucket and keeps the same keys under `prefix`, so GCS needs the storage server
  (`storage.enabled=false` with `gcs` does not render).
- **Control pods.** The operator points them at `http://ach-agent-storage:8333` with the S3 key the chart
  generates into the Secret `ach-agent-storage-s3`. Helm `lookup` keeps the key across upgrades, so install
  with `helm` or Flux, not through a `helm template` pipeline. An Istio AuthorizationPolicy admits only
  ServiceAccounts named `ach-agent-control*` to port 8333 (the default per-Agent name and the usual profile
  name); an Agent whose profile sets another `control.serviceAccountName` cannot reach it.
- **Existing data.** Objects already in the bucket stay readable. The first start reads the bucket's
  object list, which takes a while for a large bucket.
- **Switching on.** Enable it while no workspace of the namespace is active (no sandbox pods, every
  Workspace asleep). The upgrade rolls the control pods, and a control pod still on the old version saves
  its active workspaces and sessions to the bucket directly on its way out; the storage server may already
  have read the bucket by then and would keep serving the older registry. Writing to the bucket other than
  through the storage server has the same effect at any time.
- **Switching off.** `storage.enabled=false` or removing a namespace from `storage.namespaces` deletes the
  storage server and leaves its volume; nothing uploads what is still on it. First let the Agents of that
  namespace go idle, wait until `ach_agent_storage_unsynced_files` is 0 and the sync container has
  nothing left to send, then change the value.
- **Failures.** While the storage pod restarts, saves retry within `workspace.shutdownTimeoutSeconds` and
  a failed restore is retried by the next message. While S3 is unreachable, saves still succeed but each
  new object waits on S3 (about 30 s when the connection is refused, about 90 s when the endpoint does not
  resolve) and the backlog grows on the volume. A killed pod's last minute or two of changes reach S3
  at its next start, before it takes traffic; every 10 minutes it measures what S3 lacks
  (`ACHAgentStorageSyncLagging`). A file overwritten in place (a `registry.json`) whose change a killed pod
  lost is not uploaded again: S3 keeps its previous version until that state is saved again. Losing the
  volume loses what S3 does not have yet.

### Observability (optional)

Each block under `metrics` is off by default so the chart renders without the Prometheus Operator.

| Value | Renders | Notes |
|-------|---------|-------|
| `metrics.podMonitor.enabled` | PodMonitors `ach-agent-control` and `ach-agent-operator`, and `ach-agent-storage` when the storage server runs | `ach-agent-control` scrapes `:http/metrics` of every Agent control pod in any namespace and sets the `agent` label from the pod label `ach-agent.ackstorm.ai/agent`. `ach-agent-operator` scrapes the operator (`:metrics`), which exports `ach_agent_sandbox_start_seconds` (no `agent` label). `ach-agent-storage` scrapes SeaweedFS (`:metrics`) and the reconcile gauges (`:reconcile`) in `storage.namespaces`. `labels` adds labels to all of them. |
| `metrics.prometheusRule.enabled` | PrometheusRule `ach-agent` | Nine alerts: six on `ach_agent_anomalies_total`, `ach_agent_runs_total` and the exchanged ACH credential (`ACHAgentKeyRefreshFailing`), and three on the storage server (`ACHAgentStorageDown`, which needs kube-state-metrics, `ACHAgentStorageDiskHigh`, `ACHAgentStorageSyncLagging`). `labels` must match the Prometheus `ruleSelector`. |
| `metrics.dashboards.enabled` | One ConfigMap per dashboard (Agents, Agent Detail, FinOps, Start-up) | `labels` must match the Grafana sidecar label (default `grafana_dashboard: "1"`). `annotations` choose the folder, for example `--set-string 'metrics.dashboards.annotations.grafana_folder=100 - Application - ACH Agents'`; the sidecar needs `folderAnnotation=grafana_folder` and `provider.foldersFromFilesStructure=true` to honour it. |

The sidecar must watch the release namespace. The `agent` selector lists the agents with an
`ach_agent_runs_total` series in the time range, which each Agent pod exports from its first scrape. Some panels read other sources and stay empty without them:

- LLM spend and gateway errors: LiteLLM's Prometheus metrics (`litellm_spend_metric_total`,
  `litellm_proxy_*_requests_metric_total`), attributed to an agent through its LiteLLM team
  `ach-env-<environment>`, where `<environment>` is the Agent's `ach.environment`. The engine's own
  estimate (`ach_agent_turn_cost_usd_total`) prices only the models it knows and is shown for comparison.
- Pod CPU, memory and restarts: cAdvisor and kube-state-metrics.
- The Logs row of "ACH — Agent Detail": a Loki datasource with the `namespace`, `pod` and `container` labels.

## 6. A standalone Agent

An Agent namespace needs the ambient label, the Secrets the Agent references, one AgentProfile and one
Agent:

```bash
kubectl create namespace demo
kubectl label namespace demo istio.io/dataplane-mode=ambient
kubectl -n demo create secret generic openrouter --from-literal=apiKey=...
```

```yaml
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: Agent
metadata: {name: demo, namespace: demo}
spec:
  profileRef: {name: default}
  standalone:
    model:
      name: anthropic/claude-sonnet-4.5
      api: openai
      baseUrl: https://openrouter.ai/api/v1
      headers:
        Authorization: {secretKeyRef: {name: openrouter, key: apiKey}}   # the full "Bearer ..." value
    mcpServers:
      - name: serper
        url: https://mcp.serper.dev/mcp
        headers:
          X-API-KEY: {secretKeyRef: {name: serper, key: apiKey}}
  channels:
    - name: hook
      type: webhook
      source: generic
      routing: {sessionKey: "{{ event.id }}"}
      prompt: "{{ payload | json }}"
      webhook:
        auth: {type: header_token, header: X-Token, secretRef: {name: hook, key: token}}
  expose: {service: true}
```

The AgentProfile (image, resources, defaults) is in `test/e2e/fixtures/profile.yaml`; copy it and
change the images to `ghcr.io/ackstorm/ach-agent-control:0.2.1` and
`ghcr.io/ackstorm/ach-agent-execution:0.2.1`. `test/e2e/fixtures/` holds tested examples of a
standalone Agent and an ACH Agent.

## 7. An ACH Agent

With `capabilityServer: ach` the Agent takes its model, MCP servers and context from an ACH Environment,
with a credential for one identity in that Environment from exactly one of:

- `keyFrom.tokenExchange` (ACH 1.1.0 or later). The control pod exchanges a projected token of its control
  ServiceAccount (the profile's `control.serviceAccountName`, else `ach-agent-control-<uid>`) at ACH for a
  short-lived credential, which it renews at 80% of its lifetime. No key is stored in the cluster.
  `environment` is required and `baseUrl` must be `https://` and equal ACH's `ACH_BASE_URL` (a trailing
  slash is ignored): it is the token's audience.
- `keyFrom.secretKeyRef`: a static `ek_` in a Secret, for an ACH outside this cluster or a manual key.

```yaml
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: Agent
metadata: {name: k8s-doctor, namespace: ach-agent}
spec:
  profileRef: {name: default}          # control.serviceAccountName: ach-agent-control
  capabilityServer: ach
  ach:
    baseUrl: https://api.example.com
    environment: agent-k8s-doctor
    keyFrom:
      tokenExchange: {identity: ai.run@example.com}
    model: {name: ackstorm.fast, api: openai}
  channels:
    - name: tick
      type: cron
      cron: {schedule: "*/15 * * * *"}
```

`ach.mcpServers` (same shape as `standalone.mcpServers`) adds MCP servers the Environment does not have.
They authenticate with their own headers and are never sent the ACH credential; a name the Environment
already serves fails the control pod's start.

ACH issues the credential only when a platform admin's `KeyGrant` names the identity, the Environment and
the requester `<namespace>/<control ServiceAccount>`, for example `ach-agent/ach-agent-control` (see the ACH
documentation). Without one the control pod fails to start and logs ACH's code, for example
`ACH token exchange refused: not_granted`.

Trust boundary: any pod that runs as a namespace's control ServiceAccount can exchange every grant that
names `<namespace>/<serviceaccount>`, so the granularity of a grant is the namespace and ServiceAccount, not
the Agent. Agents sharing a profile ServiceAccount can use each other's grants. Keep Agents in a namespace
where only Flux and the ach-agent operator create pods, StatefulSets and Agents, and give Agents that must
not share grants different namespaces or profile ServiceAccounts. ACH reviews the token with a TokenReview,
whose request body contains it: keep `tokenreviews` at audit level `Metadata`.

The control pod exports `ach_agent_key_expiry_timestamp_seconds`, `ach_agent_key_lifetime_seconds` and
`ach_agent_key_refresh_failures_total`; `ACHAgentKeyRefreshFailing` fires when less than 10% of the
credential's lifetime is left.

## 8. Reply latency

A message pays one of three costs, depending on what is still running for its Workspace:

| State | Cost (EKS, measured) |
|---|---|
| opencode running | ~1.3s, the model call |
| sandbox up, opencode retired | ~8s: S3 restore of the session HOME, opencode boot, MCP connect |
| no sandbox | ~22s: Sandbox CR, scheduling, OpenShell's `/sandbox` PVC (~12s on EBS), containers, opencode boot |

Two timers decide the state, in order. `workspace.session.idleTimeoutSeconds` after its last turn a
session's opencode is stopped, its HOME saved and the session retired. Only once every session of
the Workspace is retired does `workspace.idleTimeoutSeconds` start; when it runs out the Workspace
is committed and the sandbox deleted. Both are per Agent, not per channel.

`routing.workspaceKey` decides how often a message lands on a new sandbox. `session` gives every
conversation its own sandbox, so every new conversation pays the full cold start. For a chat
channel, a per-user key (`"{{ event.headers.x-litellm-user-id }}"`) sends a user's new conversations
to their running sandbox; set `workspace.maxConcurrentSessions` to the channel concurrency. A user's
conversations then share `/workspace`, and a channel with a `handoff` needs `workspaceKey: session`.

The trade-off is pods held against restore cost: each active Workspace keeps one sandbox (the
profile's resources) for `session.idleTimeoutSeconds + idleTimeoutSeconds` after its last message.
A long session timeout with a short Workspace timeout keeps opencode alive for as long as the
sandbox exists.

The runtime never writes to `/sandbox` (its data is in `/workspace` and `/var/lib/ach-runtime`;
durable state goes to S3), so OpenShell's per-sandbox PVC is pure start-up cost. OpenShell 0.1.5
cannot skip it per sandbox; the gateway-level `workspace_volume = "empty_dir"` setting proposed in
NVIDIA/OpenShell#4473 removes it.
