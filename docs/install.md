# Install

ach-agent needs, in order: an Istio ambient mesh, the agent-sandbox controller, an OpenShell gateway
behind a mesh policy, then the operator chart. All versions below are the tested ones.

The cluster's CNI must enforce NetworkPolicy (on EKS, the VPC CNI addon with `enableNetworkPolicy`).
A sandbox's egress lockdown is the OpenShell driver's deny-all NetworkPolicy; without enforcement it
reaches any IP.

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

## 3. OpenShell 0.1.8

```bash
kubectl create namespace openshell
kubectl label namespace openshell istio.io/dataplane-mode=ambient
helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.8 \
  -n openshell -f test/e2e/openshell-values.yaml --wait --timeout 6m
```

The values (`test/e2e/openshell-values.yaml`):

- `server.disableTls` and `server.auth.allowUnauthenticatedUsers`: the gateway speaks plaintext and
  trusts every caller. This is safe only because step 4 restricts who can reach it. To have the
  gateway itself refuse every caller but the operator, see [Gateway mTLS](#gateway-mtls-optional).
- `server.sandboxUid` / `sandboxGid` `10001`: the execution image runs as 10001, and OpenShell needs
  the agent and the sandbox to share one UID.
- `pkiInitJob.serverDnsNames`: a wildcard SAN `*.<agent namespace>.svc.cluster.local` per Agent
  namespace, so the gateway routes the per-sandbox ExternalName Services the operator creates.

## 4. Mesh policy

Replace `<AGENT_NAMESPACE>` in `docs/mesh/openshell-mesh.yaml` with each Agent namespace and apply it.
It enforces STRICT mTLS, admits only the operator ServiceAccount, the sandbox ServiceAccount and the
Agent namespaces to the gateway, and allows ambient HBONE (TCP 15008) and kubelet probes (Istio's fixed
link-local SNAT address) through the NetworkPolicy.
Label every Agent namespace `istio.io/dataplane-mode=ambient`.

## 5. Operator

```bash
helm install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version 0.7.3 \
  -n ach-agent-system --create-namespace \
  --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080
```

The chart refuses to render unless `openshell.gateway` is set and `openshell.mesh` is `istio-ambient`.
It also installs a ValidatingAdmissionPolicy that limits the operator to Opaque Secrets named
`ach-agent-key-*` (`admissionPolicy.enabled=false` disables it).

### Gateway mTLS (optional)

With mTLS the gateway refuses every caller of its API except the operator, which presents a client
certificate with `OU=operator`. The mesh policy (step 4) stays: the sandbox service path on the same
port has no gateway authentication. Every certificate comes from an OpenShell CA in cert-manager and
is valid 30 years (`duration: 262800h`). The whole PKI, the operator's certificate included, lives in
namespace `openshell` behind namespaced Issuers. Who can mint an `OU=operator` certificate:

- anyone who can read Secrets in `openshell`: the CA key is in Secret `openshell-ca`. This includes
  the ach-agent operator, whose ClusterRole reads Secrets in every namespace;
- anyone who can create Pods, Certificates or CertificateRequests in `openshell` (a Pod can mount
  `openshell-ca`; cert-manager approves every CertificateRequest unless approver-policy is installed).

Keep those rights to the gateway's administrators. Nothing a sandbox runs as may hold them: the
sandbox workloads run in `openshell` as ServiceAccount `openshell-sandbox`, next to the PKI Secrets,
and must not read Secrets or create Pods, Certificates or CertificateRequests there. The same holds
for any sandbox volume configuration: it must never be able to mount a Secret volume.

1. cert-manager v1.19.1:

   ```bash
   helm repo add jetstack https://charts.jetstack.io --force-update
   helm upgrade --install cert-manager jetstack/cert-manager --version v1.19.1 -n cert-manager \
     --create-namespace --set crds.enabled=true --wait
   ```

2. The PKI, before the OpenShell chart. Replace `<AGENT_NAMESPACE>` in `docs/mesh/openshell-pki.yaml`
   (one `dnsNames` entry per Agent namespace in `openshell-server`, the same names as
   `pkiInitJob.serverDnsNames`):

   ```bash
   sed "s/<AGENT_NAMESPACE>/ach-agent/" docs/mesh/openshell-pki.yaml | kubectl apply -f -
   kubectl -n openshell wait --for=condition=Ready certificate/openshell-server certificate/openshell-client \
     certificate/ach-agent-operator --timeout=180s
   ```

3. OpenShell with `test/e2e/openshell-tls-values.yaml` layered after `test/e2e/openshell-values.yaml`
   (the values of [OpenShell](#3-openshell-017)):

   ```bash
   helm upgrade --install openshell oci://ghcr.io/nvidia/openshell/helm-chart --version 0.1.8 \
     -n openshell -f test/e2e/openshell-values.yaml -f test/e2e/openshell-tls-values.yaml --wait --timeout 6m
   ```

4. The operator with an `https://` gateway and its certificate Secret:

   ```bash
   helm upgrade --install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version <version> \
     -n ach-agent-system \
     --set openshell.gateway=https://openshell.openshell.svc.cluster.local:8080 \
     --set openshell.tls.secret.namespace=openshell --set openshell.tls.secret.name=ach-agent-operator-tls
   ```

   The chart lets the operator read that one Secret (a Role in `openshell`); the operator reads it at
   start and fails to start without it. The sandbox URLs become `https://`. Control pods trust the
   OpenShell CA for those URLs only (`ACH_AGENT_EXECUTION_CA_PEM`); every other connection keeps the
   system trust store.

Switching an existing install (cert-manager installed, as in item 1):

1. Delete the Secrets the OpenShell chart's certgen hook created, then apply the PKI (item 2):

   ```bash
   kubectl -n openshell delete secret openshell-server-tls openshell-client-tls
   ```

   cert-manager keeps an existing Secret whose certificate matches its Certificate's spec, whichever
   CA signed it. It re-issues the certgen `openshell-server-tls` (its names differ), but keeps the
   certgen `openshell-client-tls`, signed by the certgen CA: sandbox supervisors then fail the TLS
   handshake with the gateway (`UnknownCA`) and no sandbox becomes ready. Keep `openshell-jwt-keys`.
2. Change the OpenShell values and the operator values (items 3 and 4) in the same rollout. The
   gateway restarts with TLS and refuses the plaintext supervisors of the sandboxes running at the
   time; it drops those sandboxes, as on any gateway restart, and control creates new ones on the
   Agent's next event. A sandbox created before the switch that still runs answers 412 until control
   recreates it; deleting its pods in `openshell` forces that.

A new Agent namespace adds a `dnsNames` entry to `openshell-server` and to `pkiInitJob.serverDnsNames`.

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
  --set openshell.insecurePlaintext=true \
  --set runtime.storage.s3.bucket=<bucket> --set runtime.storage.s3.region=<region> \
  --set 'storage.namespaces={ach-agent}'
```

`openshell.insecurePlaintext=true` is required to run the gateway plaintext behind the mesh: it
acknowledges that the gateway is unauthenticated. Prefer [Gateway mTLS](#gateway-mtls-optional),
which sets `openshell.tls.secret` instead and needs no such flag.

| Value | Default | Notes |
|-------|---------|-------|
| `storage.enabled` | `true` | `false` keeps control pods on S3 directly, with their own credentials. Drain the storage server first (below). |
| `storage.namespaces` | `[]` | The Agent namespaces; required when a bucket is set. An Agent elsewhere with `persistence.type: s3` or artifacts enabled waits for the Secret `ach-agent-storage-s3`. |
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

Each run also logs one `run step` line per model call (duration, time to first token, tokens, the
engine's finish reason) and one `run tool` line per tool call (tool, outcome, duration), keyed by
`invocation_id`. They come from the sandbox, not the Agent pod: in Loki, container `agent` in the
OpenShell namespace, for example `{container="agent"} |= "<invocation_id>"`.

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
      delivery:
        url: https://hooks.example.com/result
        headers:
          Authorization: {value: "Bearer ..."}
  expose: {service: true}
```

A `webhook`, `cron` or `queue` channel answers 202 and the agent's final answer would be lost, so it can set
`delivery`: when a run finishes, the control pod POSTs
`{"invocation_id": "...", "channel": "hook", "state": "completed", "text": "...", "error": null}` to the URL
(`text` is the agent's final text, `null` when there is none; `state` is `failed` and `error` set when the run
raised). One attempt per run, 10 s timeout, redirects not followed. A failed POST is logged and counted in
`ach_agent_channel_delivery_failures_total{channel}`. A run that expires in the admission queue is not
delivered. The control pod needs egress to the URL under the mesh policy (`docs/mesh/`).

The AgentProfile holds the images, the sandbox resources and the defaults every Agent of the namespace
inherits:

```yaml
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: AgentProfile
metadata: {name: default, namespace: demo}
spec:
  control:
    image: ghcr.io/ackstorm/ach-agent-control:0.7.3
    terminationGracePeriodSeconds: 60
  sandbox:
    openshell:
      image: ghcr.io/ackstorm/ach-agent-execution:0.7.3
      resources: {cpu: 500m, memory: 1Gi}
      readyTimeoutSeconds: 180
  defaults:
    limits:
      maxActiveWorkspaces: 4
      maxConcurrentInvocations: 4
      maxConcurrentScripts: 2
      maxInvocationSeconds: 300
      maxQueuedTotal: 100
      idempotencyWindowSeconds: 3600
      maxSteps: 50
    engine:
      compaction: {auto: true, keep: {tokens: 8000}, buffer: 20000}
    workspace:
      idleTimeoutSeconds: 600
      shutdownTimeoutSeconds: 30
      maxConcurrentSessions: 1
      persistence: {type: none}
      session:
        idleTimeoutSeconds: 300
    artifacts: {enabled: false, maxArtifactBytes: 1048576, retentionDays: 1}
```

`test/e2e/fixtures/` holds tested examples of a standalone Agent and an ACH Agent.

The operator image checks an Agent against its AgentProfile without a cluster: the rendered runtime
config and the rules the controller reports as conditions (for example the grace period against
`workspace.shutdownTimeoutSeconds`). CRD defaults and CEL rules are not applied; those need
`kubectl apply --dry-run=server`.

```bash
cat profile.yaml agent.yaml | docker run --rm -i ghcr.io/ackstorm/ach-agent-operator:0.7.3 validate -f -
```

### Engine

`defaults.engine.type` (AgentProfile) or `spec.engine.type` (Agent) selects `opencode` (default) or `pi`. The sandbox image
must contain the engine: for `pi`, set `sandbox.openshell.image` to
`ghcr.io/ackstorm/ach-agent-execution-pi:<version>`, which ships PiG as `/opt/pi/pig` (181 MB against
368 MB for `ghcr.io/ackstorm/ach-agent-execution`). `engine.type: pi` on an image without the binary
fails at start with a message naming the engine and the image. pi takes `model.api: openai` only.
`test/e2e/fixtures/pi-profile.yaml` and `pi-agent.yaml` are a tested example.

The known differences between the engines and the security boundaries are in
[local-agent.md](local-agent.md#engines); they apply to Agents in the cluster as well. In the cluster the
boundary is the OpenShell policy, and `denyTools` is not one.

To run upstream pi instead of PiG, use an image with node and the pi package and add `node` to
`runtimeBinaries` (in `operator/internal/openshell/policy.go`); this adds about 215 MB on Alpine. Agents, config and sessions stay as they are.

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
    model:
      name: ackstorm.smart
      api: openai
      limits: {context: 524288, output: 65536}   # optional; overrides the limits ACH reports
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
| sandbox up, opencode stopped | ~8s at most: S3 restore of the session HOME (skipped when the sandbox kept it), opencode boot, MCP connect |
| no sandbox | ~22s: Sandbox CR, scheduling, OpenShell's `/sandbox` PVC (~12s on EBS), containers, opencode boot |

Two timers decide the state, in order. `workspace.session.idleTimeoutSeconds` after its last turn a
session's opencode is stopped and its HOME saved. With `persistence: s3` and a session timeout
above `0` the HOME stays in the sandbox, so the next message of that conversation resumes it in place
instead of restoring it from S3; with a `0` timeout it is deleted. Only once no session of the
Workspace is running does `workspace.idleTimeoutSeconds` start; when it runs out the Workspace
is committed and the sandbox deleted. `0` acts on the next lifecycle tick (about a second); a
`sessionStart` gate that skips the run (exit 78) counts as the end of its work.
`workspace.idleTimeoutSeconds: -1` never deletes the sandbox for idleness.

`workspace.idleTimeoutSeconds` is per Agent. `workspace.session.idleTimeoutSeconds`, when set, also
applies to every channel; unset, a session takes the default of the channel it last ran on: `0` for
`cron` and `webhook-script`, whose sessions never get a follow-up, and `60` for `a2a`, `webhook` and
`queue`. An Agent with a cron and a chat channel and `workspace.idleTimeoutSeconds: 0` then deletes
the sandbox right after a tick, and a minute after the last chat message.

`routing.workspaceKey` decides how often a message lands on a new sandbox. `session` gives every
conversation its own sandbox, so every new conversation pays the full cold start. For a chat
channel, a per-user key (`"{{ event.headers.x-litellm-user-id }}"`) sends a user's new conversations
to their running sandbox; set `workspace.maxConcurrentSessions` to the channel concurrency. A user's
conversations then share `/sandbox/ach/workspace`, and a channel with a `handoff` needs `workspaceKey: session`.
`maxConcurrentSessions` counts live sessions, idle ones whose engine is still up included, so the
profile's memory must fit that many engines (opencode takes about 200 MiB each). At the limit a new
conversation closes the longest-idle one as its idle timeout would: with `persistence: s3` or `volume`
it resumes on its next message, with `none` it starts over.

The trade-off is pods held against restore cost: each active Workspace keeps one sandbox (the
profile's resources) for the session timeout plus `idleTimeoutSeconds` after its last message.
A long session timeout with a short Workspace timeout keeps opencode alive for as long as the
sandbox exists.

`workspace.persistence` decides what survives the sandbox, for the Workspace files and the session
HOMEs together. With `type: none` (the default) both are lost when the sandbox is deleted. With
`type: s3` both are saved to the install-wide S3 store (`runtime.storage`) when they go idle and
restored on the next sandbox; `retentionDays` is required, and `session.retentionDays` (optional, at
most `retentionDays`) keeps saved sessions for less. An Agent's `persistence` block replaces the
profile's as a whole.

```yaml
workspace:
  persistence: {type: s3, retentionDays: 90}
  session: {idleTimeoutSeconds: 300, retentionDays: 7}
```

The runtime keeps its data under `/sandbox/ach`: the Workspace in `/sandbox/ach/workspace`, the
session HOMEs in `/sandbox/ach/sessions`. With `none` and `s3` that is OpenShell's per-sandbox
`/sandbox` PVC, deleted with the sandbox, so it is start-up cost only. OpenShell 0.1.8 cannot skip it per sandbox; the
gateway-level `workspace_volume = "empty_dir"` setting proposed in NVIDIA/OpenShell#4473 replaces it
with an emptyDir.

#### Workspace volumes (`type: volume`)

With `type: volume` each Workspace gets its own PVC, created by the operator in the sandbox namespace
(`openshell`) as `ach-ws-<hash>` and mounted at `/sandbox/ach` by every sandbox of that Workspace.
The Workspace files and the session HOMEs stay on it: an idle session stops opencode and keeps its
HOME, an idle Workspace deletes the sandbox and keeps the PVC, and the next event mounts the PVC in a
new sandbox and resumes the conversation. Nothing is transferred and no S3 store is needed.

```yaml
workspace:
  persistence:
    type: volume
    retentionDays: 30               # a PVC with no sandbox for this long is deleted
    volume:
      size: 10Gi                    # applies to new Workspaces
      storageClassName: gp3-delete  # optional, the cluster default class; applies to new Workspaces
      accessMode: ReadWriteOnce     # default; or ReadWriteMany
  session: {idleTimeoutSeconds: 300, retentionDays: 7}  # HOMEs unused this long go at Workspace start
```

Requirements:

- Gateway mTLS (`openshell.tls.secret`, above). Without it the operator refuses the Agent
  (`WorkloadApplied=False`, reason `VolumeRequiresGatewayTLS`).
- `server.drivers.kubernetes.allowDriverConfig: true` in the OpenShell values, only with gateway mTLS
  on. The switch lets every caller allowed to create sandboxes set driver config (node selectors,
  tolerations, resources, labelled PVC mounts); with mTLS the operator is the only such caller, and it
  sends nothing but the Workspace PVC mount. Turning the switch off again fails every sandbox created
  with driver config until it is recreated.
- Only the operator may create PVCs in `openshell`: the gateway mounts any PVC there that carries
  `openshell.ai/sandbox-attachable: "true"` and `openshell.ai/sandbox-attachable-workspace: <workspace>`.
- A StorageClass with `reclaimPolicy: Delete`. Deleting the Agent deletes its PVCs, and then the class
  deletes the volumes; with `Retain` the PVs stay behind.
- Write access for uid/gid 10001, the sandbox user. Block volumes (EBS gp3) get it through the pod's
  `fsGroup`; NFS (EFS) ignores `fsGroup`, so its access points need `uid`/`gid` 10001. `local-path`
  pins the PV to one node and suits single-node clusters only.

A Workspace's new sandbox starts once the old pod is gone, with either access mode. `ReadWriteOnce`
(gp3) stays in one AZ and takes about 10 s to attach; `ReadWriteMany` (EFS) spans AZs and survives
node loss. Example classes on EKS:

```yaml
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: gp3-delete}
provisioner: ebs.csi.aws.com
parameters: {type: gp3}
reclaimPolicy: Delete
allowVolumeExpansion: true
volumeBindingMode: WaitForFirstConsumer
---
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata: {name: efs-delete}
provisioner: efs.csi.aws.com
parameters:
  provisioningMode: efs-ap
  fileSystemId: <file system id>
  directoryPerms: "700"
  uid: "10001"
  gid: "10001"
reclaimPolicy: Delete
```

Changing `size`, `storageClassName` or `accessMode` applies to new Workspaces; existing PVCs keep
theirs. Changing `type` on a live Agent starts every Workspace fresh: after a change away from
`volume` each PVC is deleted once its sandbox is gone.
