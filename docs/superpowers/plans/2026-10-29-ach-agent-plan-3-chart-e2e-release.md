# ach-agent Plan 3: chart, cluster e2e and release

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship ach-agent: a Helm chart (operator, CRDs, RBAC, admission guard), an end-to-end suite on its own kind cluster with Istio ambient and a real OpenShell gateway, and a release workflow that publishes the three images and the OCI chart at `v0.19.0`.

**Architecture:** `chart/` is a small Helm chart for the operator only; OpenShell and Istio are prerequisites documented in `docs/install.md` and installed by the e2e script. `test/e2e/` holds the cluster script (`cluster.sh`), stub upstreams (model, MCP, ACH hydrate) in one stdlib-Python image, and a Go e2e suite (build tag `e2e`) driving real Agents. Release follows the ach-runtime convention: a `chore(release): vX.Y.Z` commit on `main` triggers CI, image and chart publication, then the tag and GitHub release.

**Tech Stack:** Helm 3, kind, Istio 1.28.3 ambient (`ambient.dnsCapture=false`), OpenShell chart `oci://ghcr.io/nvidia/openshell/helm-chart` 0.1.5, kubernetes-sigs/agent-sandbox v1.0.8, Go 1.26 + client-go for e2e, GitHub Actions.

**Depends on:** Plans 1 and 2 merged on `feat/bootstrap` in `/home/coder/workspace/local/ach-agent`, plus the `/code-review` fixes. Open issues carried from Plan 2's report: ValidatingAdmissionPolicy for operator-created Secrets; leases Role; cluster-wide `pods: delete`; unverified gateway handling of `{cpu, memory, ephemeral-storage}` template resources; finalizer vs a record already gone while its pod terminates.

## Global Constraints

- Repo `/home/coder/workspace/local/ach-agent`, branch `feat/release` from `feat/bootstrap`. Version `0.19.0` everywhere (runtime `pyproject.toml`, `chart/Chart.yaml` `version` and `appVersion`, image tags).
- Images: `ghcr.io/ackstorm/ach-agent-operator`, `ghcr.io/ackstorm/ach-agent-control` (runtime Dockerfile target `control`), `ghcr.io/ackstorm/ach-agent-execution` (target `execution`). Chart: `oci://ghcr.io/ackstorm/charts/ach-agent`.
- Chart install namespace default `ach-agent-system`; operator ServiceAccount `ach-agent-operator`; the chart refuses to render unless `openshell.gateway` is set and `openshell.mesh` is `istio-ambient` (`fail`).
- Operator pod label `istio.io/dataplane-mode: ambient`.
- e2e cluster name `ach-agent-e2e` ONLY. Never touch `ach-e2e` or any other cluster. All kubectl/helm calls pass `--context kind-ach-agent-e2e`.
- Versions pinned: Istio `1.28.3`, OpenShell chart `0.1.5`, agent-sandbox `v1.0.8`, kind node image pinned in `test/e2e/kind-config.yaml`.
- Bounded waits only (`kubectl wait --timeout`, `helm --wait --timeout`, bounded loops with failure exit). Never pipe a gate into `tail`/`grep`.
- Git: `/usr/bin/git`, never push, never tag, never trigger a release. Commit trailers:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
  ```
- No secrets in files; e2e stub credentials are fixed test strings (`e2e-model-key`, `e2e-serper-key`, `e2e-ek`).

---

### Task 1: Helm chart

**Files:**
- Create: `chart/Chart.yaml`, `chart/values.yaml`, `chart/values.schema.json`, `chart/crds/` (copied from `operator/config/crd/`), `chart/templates/{_helpers.tpl,serviceaccount.yaml,clusterrole.yaml,clusterrolebinding.yaml,leases-role.yaml,deployment.yaml,admission-policy.yaml,NOTES.txt}`, `chart/tests/render_test.sh`, root `Makefile` (targets `chart-sync`, `chart-test`)
- Create: `docs/install.md`, `docs/mesh/openshell-mesh.yaml`

**Interfaces:**
- Values (exact keys):
  ```yaml
  image: {operator: ghcr.io/ackstorm/ach-agent-operator, tag: "", pullPolicy: IfNotPresent}
  imagePullSecrets: []
  openshell:
    gateway: ""                 # required, e.g. http://openshell.openshell.svc.cluster.local:8080
    mesh: istio-ambient         # required value; anything else fails rendering
    workspace: default
    podNamespace: openshell
    serviceURL: ""              # default from the operator
  runtime:
    storage:
      s3: {bucket: "", region: "", endpointUrl: "", prefix: ach, credentialsSecret: ""}
  route:
    parent: {name: "", namespace: "", sectionName: ""}
  operator:
    replicas: 1
    leaderElect: true
    resources: {requests: {cpu: 50m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}
    podLabels: {}
    nodeSelector: {}
    tolerations: []
  admissionPolicy:
    enabled: true
  ```
- Env wiring (deployment.yaml): `ACH_AGENT_OPENSHELL_GATEWAY`, `_WORKSPACE`, `_POD_NAMESPACE`, `_SERVICE_URL` (only if set), `ACH_AGENT_NAMESPACE` (fieldRef `metadata.namespace`), `ACH_AGENT_STORAGE_S3_{BUCKET,REGION,ENDPOINT_URL,PREFIX,CREDENTIALS_SECRET}`, `ACH_AGENT_ROUTE_PARENT_{NAME,NAMESPACE,SECTION}`. Args: `--leader-elect={{ .Values.operator.leaderElect }}`, `--metrics-bind-address=:8080`, `--health-probe-bind-address=:8081`. Image tag defaults to `.Chart.AppVersion`.

- [ ] **Step 1: Write the failing render test**

`chart/tests/render_test.sh`:

```bash
#!/usr/bin/env bash
# Renders the chart and asserts the security-relevant output. Needs helm and python3.
set -euo pipefail
cd "$(dirname "$0")/.."
fail() { echo "FAIL: $*" >&2; exit 1; }
base=(--namespace ach-agent-system --set openshell.gateway=http://openshell.openshell.svc.cluster.local:8080)

helm template t . --namespace ach-agent-system >/dev/null 2>&1 && fail "renders without openshell.gateway"
helm template t . "${base[@]}" --set openshell.mesh=none >/dev/null 2>&1 && fail "renders with mesh=none"

out=$(helm template t . "${base[@]}")
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
kinds = {(d["kind"], d["metadata"]["name"]) for d in docs}
def one(kind):
    found = [d for d in docs if d["kind"] == kind]
    assert len(found) == 1, (kind, len(found))
    return found[0]
dep = one("Deployment")
pod = dep["spec"]["template"]
assert pod["metadata"]["labels"]["istio.io/dataplane-mode"] == "ambient"
c = pod["spec"]["containers"][0]
env = {e["name"]: e for e in c.get("env", [])}
assert env["ACH_AGENT_OPENSHELL_GATEWAY"]["value"] == "http://openshell.openshell.svc.cluster.local:8080"
assert env["ACH_AGENT_NAMESPACE"]["valueFrom"]["fieldRef"]["fieldPath"] == "metadata.namespace"
assert c["image"].endswith(":0.19.0"), c["image"]
sc = c["securityContext"]
assert sc["readOnlyRootFilesystem"] and not sc["allowPrivilegeEscalation"] and sc["capabilities"]["drop"] == ["ALL"]
role = one("Role")
assert any("leases" in r["resources"] for r in role["rules"]), role
vap = one("ValidatingAdmissionPolicy")
exprs = " ".join(v["expression"] for v in vap["spec"]["validations"])
assert "ach-agent-key-" in exprs and "Opaque" in exprs, exprs
one("ValidatingAdmissionPolicyBinding")
cr = one("ClusterRole")
for r in cr["rules"]:
    assert "*" not in r.get("verbs", []) and "*" not in r.get("resources", []), r
print("render ok")
PY
echo PASS
```

- [ ] **Step 2: Run to verify failure**

Run: `bash chart/tests/render_test.sh`
Expected: FAIL (chart missing).

- [ ] **Step 3: Write the chart**

- `Chart.yaml`: `apiVersion: v2`, `name: ach-agent`, `version: 0.19.0`, `appVersion: "0.19.0"`, `type: application`.
- `_helpers.tpl`: `ach-agent.fullname`, labels (`app.kubernetes.io/name: ach-agent-operator`, `app.kubernetes.io/instance`, `app.kubernetes.io/version`), and a `ach-agent.validate` helper called from `deployment.yaml`:

```yaml
{{- define "ach-agent.validate" -}}
{{- if not .Values.openshell.gateway -}}
{{- fail "openshell.gateway is required (the OpenShell gateway URL, e.g. http://openshell.openshell.svc.cluster.local:8080)" -}}
{{- end -}}
{{- if ne .Values.openshell.mesh "istio-ambient" -}}
{{- fail "openshell.mesh must be istio-ambient: ach-agent requires an Istio ambient mesh to protect the OpenShell gateway" -}}
{{- end -}}
{{- end -}}
```

- `clusterrole.yaml`: generated from `operator/config/rbac/role.yaml` by `make chart-sync` (rename to `{{ include "ach-agent.fullname" . }}`), minus the `leases` rule (moved to the namespaced Role).
- `leases-role.yaml`: `Role` + `RoleBinding` in the release namespace: `coordination.k8s.io` `leases` get;list;watch;create;update;patch;delete, and core `events` create;patch.
- `admission-policy.yaml` (when `admissionPolicy.enabled`): `admissionregistration.k8s.io/v1` `ValidatingAdmissionPolicy` + `ValidatingAdmissionPolicyBinding`:

```yaml
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {{ include "ach-agent.fullname" . }}-secrets
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["secrets"]
  matchConditions:
    - name: operator-only
      expression: >-
        request.userInfo.username == "system:serviceaccount:{{ .Release.Namespace }}:{{ include "ach-agent.serviceAccountName" . }}"
  validations:
    - expression: >-
        object.metadata.name.startsWith("ach-agent-key-") &&
        (!has(object.type) || object.type == "Opaque")
      message: "the ach-agent operator may only create or update Opaque Secrets named ach-agent-key-*"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {{ include "ach-agent.fullname" . }}-secrets
spec:
  policyName: {{ include "ach-agent.fullname" . }}-secrets
  validationActions: [Deny]
```

- `deployment.yaml`: per Interfaces; pod `securityContext` runAsNonRoot, seccomp RuntimeDefault; container `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, drop ALL; probes `/healthz` and `/readyz` on 8081; label `istio.io/dataplane-mode: ambient` merged with `operator.podLabels`.
- `crds/`: `make chart-sync` copies `operator/config/crd/*.yaml` and `operator/config/rbac/role.yaml`-derived rules; `make chart-test` runs `chart-sync`, fails on git drift in `chart/`, runs `helm lint chart --set openshell.gateway=http://x:8080` and `bash chart/tests/render_test.sh`.
- `values.schema.json`: types for the values above; `openshell.mesh` enum `["istio-ambient"]`.

`docs/install.md`: prerequisites in order — Istio ambient 1.28.3 (`istio-cni` with `--set ambient.dnsCapture=false`, with the reason: ztunnel's DNS proxy breaks the sandbox supervisor's TCP DNS probe), agent-sandbox v1.0.8, OpenShell chart 0.1.5 with the values from `test/e2e/openshell-values.yaml` (explain each: `disableTls` + `allowUnauthenticatedUsers` are safe only behind the mesh policy; `sandboxUid/Gid 10001`; `pkiInitJob.serverDnsNames` wildcard per agent namespace), `docs/mesh/openshell-mesh.yaml`, then `helm install ach-agent oci://ghcr.io/ackstorm/charts/ach-agent`. Then a minimal standalone `AgentProfile` + `Agent` example (OpenRouter-style model with a `secretKeyRef` header, one MCP server).

`docs/mesh/openshell-mesh.yaml`: the v0.14.2 `test/e2e/cluster/07-openshell/mesh.yaml` with the gateway `AuthorizationPolicy` sources changed to `principals: [cluster.local/ns/ach-agent-system/sa/ach-agent-operator, cluster.local/ns/openshell/sa/openshell-sandbox]` plus a second source `namespaces: [<AGENT_NAMESPACE>]` (control pods use per-Agent ServiceAccounts, so they are admitted by namespace); keep the `from-gateway` policy and the HBONE `NetworkPolicy` (port 15008) with their comments.

- [ ] **Step 4: Run tests**

Run: `make chart-test`
Expected: `PASS`.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add chart docs/install.md docs/mesh Makefile
/usr/bin/git commit -q -m "feat(chart): operator chart with leases Role and Secret admission guard" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 2: e2e cluster and stub upstreams

**Files:**
- Create: `test/e2e/kind-config.yaml`, `test/e2e/cluster.sh`, `test/e2e/openshell-values.yaml`, `test/e2e/mesh.yaml` (rendered from `docs/mesh/openshell-mesh.yaml` with `AGENT_NAMESPACE=ach-agent-e2e`), `test/e2e/stubs/{Dockerfile,stub.py}`, root Makefile targets `e2e-up`, `e2e-down`, `e2e-run`, `e2e`

**Interfaces:**
- `test/e2e/cluster.sh up|down|load` — `up`: create kind `ach-agent-e2e`, install agent-sandbox, Istio ambient, OpenShell (values: `serverDnsNames: ["*.ach-agent-e2e.svc.cluster.local"]`), apply `mesh.yaml`, label namespace `ach-agent-e2e` ambient, build + `kind load` four images (`ach-agent-operator:e2e`, `ach-agent-control:e2e`, `ach-agent-execution:e2e`, `ach-agent-e2e-stub:e2e`), deploy the stub, `helm upgrade --install` the chart with `openshell.gateway=http://openshell.openshell.svc.cluster.local:8080`, image tags `e2e`. `down`: `kind delete cluster --name ach-agent-e2e`. Every step idempotent.
- Stub service `e2e-stub.ach-agent-e2e.svc.cluster.local` (one Deployment, ports): `8090` model (OpenAI-compatible `POST /v1/chat/completions`, streaming and non-streaming, replies with a fixed assistant text), `8091` MCP (streamable HTTP with one tool `echo`), `8092` ACH (`POST /platform/hydrate`, `GET /v2/model/info`, reusing `runtime/examples/hosts/compose/stub/ach_stub.py` logic). Every request is recorded with its headers; `GET /_requests?port=<p>` returns the recorded list (JSON) so tests assert the received headers; `DELETE /_requests` clears it. Required credentials: model `x-litellm-api-key: e2e-model-key`, MCP `X-API-KEY: e2e-serper-key`, ACH `x-ach-key: e2e-ek` (wrong or missing -> 401).

- [ ] **Step 1: Stub and its test**

Write `test/e2e/stubs/stub.py` (stdlib `http.server` + `threading`, one server per port; non-root user 10001 in the Dockerfile, `python:3.13-alpine`, `COPY stub.py /app/stub.py` only). Add `test/e2e/stubs/test_stub.py` (pytest, starts the three servers on ephemeral ports in-process) covering: 401 without the credential, 200 with it, header recording, `DELETE /_requests`. Run: `python3 -m pytest test/e2e/stubs -q` inside `runtime/.venv` (`runtime/.venv/bin/python -m pytest ...`). Expected: PASS.

- [ ] **Step 2: Cluster script**

`cluster.sh` uses the v0.14.2 `reconcile_openshell` sequence (map: istio base, istiod `--set profile=ambient`, istio-cni `--set profile=ambient --set ambient.dnsCapture=false`, ztunnel; namespace `openshell` labelled ambient; OpenShell chart with values; `mesh.yaml`), plus agent-sandbox `kubectl apply --server-side -f https://github.com/kubernetes-sigs/agent-sandbox/releases/download/v1.0.8/sandbox-with-extensions.yaml` and `kubectl -n agent-sandbox-system rollout status deploy/agent-sandbox-controller --timeout=300s` BEFORE the OpenShell chart. Every helm call `--wait --timeout 6m`; every kubectl call `--context kind-ach-agent-e2e`. Fail fast if the current kind clusters include `ach-agent-e2e` but its context is missing.

- [ ] **Step 3: Bring it up**

```bash
cd /home/coder/workspace/local/ach-agent
timeout 2400 make e2e-up > /tmp/e2e-up.log 2>&1; echo "EXIT=$?"
kubectl --context kind-ach-agent-e2e -n ach-agent-system rollout status deploy/ach-agent-operator --timeout=300s
kubectl --context kind-ach-agent-e2e -n openshell get pods
```

Expected: `EXIT=0`, operator rolled out, OpenShell gateway Ready.

- [ ] **Step 4: Commit**

```bash
/usr/bin/git add test/e2e Makefile
/usr/bin/git commit -q -m "test(e2e): kind cluster with Istio ambient, OpenShell and stub upstreams" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 3: e2e scenarios

**Files:**
- Create: `test/e2e/go.mod` (module `github.com/ackstorm/ach-agent/test/e2e`, requires the operator module via `replace ../../operator`), `test/e2e/e2e_test.go` (build tag `e2e`), `test/e2e/helpers_test.go`, `test/e2e/fixtures/{profile.yaml,standalone-agent.yaml,ach-agent.yaml,rejected-policy-agent.yaml}`

**Interfaces:**
- Helpers: `apply(t, path string, vars map[string]string)`, `waitCond(t, kind, name, cond, status string, timeout)`, `postWebhook(t, agent string, body []byte) (status int)` (port-forward to the agent's `-http` Service or a curl pod in the namespace), `stubRequests(t, port int) []recorded`, `sandboxes(t, agentUID string) []string` (OpenShell sandbox pods by label `runtime.ach-agent.ackstorm.ai/agent-uid`), `restartGateway(t)` (`kubectl rollout restart statefulset/openshell -n openshell` + wait), `operatorLog(t) string`.
- Fixtures: profile with `sandbox.openshell.image: ach-agent-execution:e2e`, `control.image: ach-agent-control:e2e`, `sandbox.openshell.resources: {cpu: "1", memory: 1Gi}`, `defaults` from the v0.14.2 e2e profile; standalone Agent with `standalone.model` pointing at `http://e2e-stub.ach-agent-e2e.svc.cluster.local:8090/v1` (header `x-litellm-api-key` from Secret `e2e-model`), one MCP server at `:8091/mcp` (header `X-API-KEY` from Secret `e2e-serper`), a `webhook` channel with `header_token` auth, `expose.service: true`; ACH Agent pointing at `:8092` with key Secret `e2e-ek`.

Scenarios (one `t.Run` each, in this order, sharing the cluster):

1. `standalone`: apply profile + Secrets + standalone Agent; `Ready=True` within 5m; post a webhook; the stub model saw exactly one `x-litellm-api-key: e2e-model-key` per call and NO `x-ach-*` credential; the MCP stub saw `X-API-KEY: e2e-serper-key`; a sandbox pod exists with the agent-uid label; the operator log has no `SandboxPolicyRejected`.
2. `sandbox-egress`: `kubectl exec` into the sandbox workload container: `curl -sS -m 5 http://e2e-stub.ach-agent-e2e.svc.cluster.local:8090/` must fail (denied by policy; not a facade binary path), while the agent run in scenario 1 succeeded through the facade. Also assert the effective sandbox spec (from the gateway: `openshell` CLI in the gateway pod, or the sandbox CR the driver creates) carries the `{cpu, memory}` resources; if the gateway ignores them, record it in the report as a known gap (do not fail).
3. `ach-mode`: apply the ACH Agent; `Ready=True`; post a webhook; the ACH stub saw `/platform/hydrate` with `x-ach-key: e2e-ek`.
4. `rejected-policy`: Agent whose policy has a rule named `ach-agent-x`; `Ready=False`, reason `SandboxPolicyRejected`, within 3m after a webhook triggers a sandbox; then patch the Agent to remove the rule; `Ready=True` returns.
5. `gateway-restart`: with the standalone Agent serving, `restartGateway(t)`; within 3m `GatewayAvailable=True` again WITHOUT restarting the operator; the original sandbox name is unchanged (not replaced); a new webhook succeeds.
6. `gateway-down-control-restart`: scale the OpenShell gateway StatefulSet to 0; delete the control pod; wait for the new control pod Running; `GatewayAvailable=False`; scale the gateway back to 1; within 5m the existing sandbox is adopted (same name) and a webhook succeeds.
7. `secret-rotation`: change Secret `e2e-model` to `e2e-model-key-2` and the stub's accepted key; the control StatefulSet's `ach-agent.ackstorm.ai/config-hash` changes; a webhook after rollout reaches the stub with the new key.
8. `deletion`: delete the standalone Agent; within 3m no sandbox pod with its UID, no ExternalName Service with its UID label, and the Agent is gone (finalizer released).

- [ ] **Step 1: Write the suite** (fixtures + helpers + scenarios above; each assertion with a bounded wait and an explicit failure message that includes the relevant `kubectl get -o yaml` or operator log tail).

- [ ] **Step 2: Run**

```bash
cd /home/coder/workspace/local/ach-agent
timeout 3600 make e2e-run > /tmp/e2e-run.log 2>&1; echo "EXIT=$?"
grep -E "^(=== RUN|--- (PASS|FAIL)|PASS|FAIL|ok)" /tmp/e2e-run.log
```

Expected: `EXIT=0`, all 8 PASS. A failure that reveals a product bug: fix it in `runtime/` or `operator/` with a unit test first (TDD), rebuild images (`test/e2e/cluster.sh load`), rerun the failing scenario (`go test -tags e2e -run 'TestE2E/<name>'`), then the whole suite once.

- [ ] **Step 3: Commit**

```bash
/usr/bin/git add test/e2e
/usr/bin/git commit -q -m "test(e2e): standalone, ach, policy, gateway outage, rotation and deletion scenarios" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 4: Release workflow

**Files:**
- Create: `.github/workflows/release.yml`, `.github/workflows/e2e.yml` (manual `workflow_dispatch` + nightly), root `CHANGELOG.md` (`## [0.19.0]` entry summarising the unification), root Makefile targets `release-bump VERSION=X.Y.Z`, `release-check`
- Modify: `.github/workflows/runtime.yml`, `.github/workflows/operator.yml` (add `workflow_call` so release reuses them)

**Interfaces:**
- Release trigger: push to `main` whose head commit subject is `chore(release): vX.Y.Z` (same parser as `../ach-runtime/.github/workflows/release.yml` job `parse-release`).
- `release-check` (also run by the workflow): `runtime/pyproject.toml` version == `chart/Chart.yaml` `version` == `appVersion` == `X.Y.Z`; `CHANGELOG.md` has `## [X.Y.Z]`.
- `release-bump VERSION=X.Y.Z`: sets those three fields and adds a CHANGELOG heading; never commits.

- [ ] **Step 1: Workflow**

Jobs, in order: `parse-release` (marker + `make release-check`), `runtime-ci` and `operator-ci` (`uses: ./.github/workflows/runtime.yml` / `operator.yml`), `publish-images` (login `ghcr.io` with `GITHUB_TOKEN`, `packages: write`; build+push `ach-agent-operator` from `operator/`, `ach-agent-control` and `ach-agent-execution` from `runtime/` targets; tags `X.Y.Z` plus `latest` for non-prerelease; then `docker buildx imagetools inspect` each), `publish-chart` (`helm package chart --version X.Y.Z --app-version X.Y.Z`, `helm push ach-agent-X.Y.Z.tgz oci://ghcr.io/ackstorm/charts`), `create-release` (annotated tag `vX.Y.Z` on the release commit, verify-not-overwrite logic as in ach-runtime, `gh release create` with the CHANGELOG section). Pin actions as the existing workflows do.

- [ ] **Step 2: Validate locally**

```bash
cd /home/coder/workspace/local/ach-agent
make release-check VERSION=0.19.0; echo "EXIT=$?"
docker run --rm -v "$PWD:/repo" -w /repo rhysd/actionlint:1.7.7 -color; echo "EXIT=$?"
```

Expected: both `EXIT=0`.

- [ ] **Step 3: Commit**

```bash
/usr/bin/git add .github CHANGELOG.md Makefile
/usr/bin/git commit -q -m "build: release workflow publishing images and the OCI chart" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Final gate

```bash
cd /home/coder/workspace/local/ach-agent
make chart-test; echo "EXIT=$?"
make release-check VERSION=0.19.0; echo "EXIT=$?"
timeout 3600 make e2e-run > /tmp/e2e-final.log 2>&1; echo "EXIT=$?"
```

Expected: all `EXIT=0`. Leave the `ach-agent-e2e` cluster up for the user; report how to delete it (`make e2e-down`).
