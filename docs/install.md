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
helm install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent --version 0.1.5 \
  -n ach-agent-system --create-namespace \
  --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080
```

The chart refuses to render unless `openshell.gateway` is set and `openshell.mesh` is `istio-ambient`.
It also installs a ValidatingAdmissionPolicy that limits the operator to Opaque Secrets named
`ach-agent-key-*` (`admissionPolicy.enabled=false` disables it).

### Observability (optional)

Each block under `metrics` is off by default so the chart renders without the Prometheus Operator.

| Value | Renders | Notes |
|-------|---------|-------|
| `metrics.podMonitor.enabled` | PodMonitor `ach-agent-control` | Scrapes `:http/metrics` of every Agent control pod in any namespace and sets the `agent` label from the pod label `ach-agent.ackstorm.ai/agent`. `labels` adds labels to the PodMonitor. |
| `metrics.prometheusRule.enabled` | PrometheusRule `ach-agent` | Six alerts on `ach_agent_anomalies_total`, `ach_agent_runs_total` and the exchanged ACH credential (`ACHAgentKeyRefreshFailing`). `labels` must match the Prometheus `ruleSelector`. |
| `metrics.dashboards.enabled` | One ConfigMap per dashboard (Agents, Agent Detail, FinOps) | `labels` must match the Grafana sidecar label (default `grafana_dashboard: "1"`). `annotations` choose the folder, for example `--set-string 'metrics.dashboards.annotations.grafana_folder=100 - Application - ACH Agents'`; the sidecar needs `folderAnnotation=grafana_folder` and `provider.foldersFromFilesStructure=true` to honour it. |

The sidecar must watch the release namespace. The dashboards list agents from `ach_agent_info`, which
each Agent pod exports from its first scrape.

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
change the images to `ghcr.io/ackstorm/ach-agent-control:0.1.5` and
`ghcr.io/ackstorm/ach-agent-execution:0.1.5`. `test/e2e/fixtures/` holds tested examples of a
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
