# ach-agent Plan 2: operator (`Agent`, `AgentProfile`, OpenShell)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the ACHAgent operator from ach v0.14.2 into `ach-agent/operator`, reshaped to the spec: CRDs `Agent`/`AgentProfile` in `ach-agent.ackstorm.ai/v1alpha1`, rendering `workspace-v3`, OpenShell as the only backend with a merged per-sandbox policy, and gateway-outage handling.

**Architecture:** One Go module `github.com/ackstorm/ach-agent/operator`. Packages: `api/v1alpha1` (types), `internal/render` (Agent + AgentProfile -> `workspace-v3` config), `internal/controller` (Agent reconciler: control StatefulSet, Services, ConfigMap, key Secret, HTTPRoute, finalizer), `internal/openshell` (sandbox backend: policy merge, create/delete, per-sandbox ExternalName Service, client rebuild), `internal/host` (leader-elected runner: one host loop per Agent against the runtime's `/host/v1`), `third_party/openshellconv` (vendored Apache-2.0 SDK converter), `cmd/operator` (main).

**Tech Stack:** Go 1.26, controller-runtime v0.24.1, k8s.io v0.36, OpenShell Go SDK `github.com/NVIDIA/OpenShell/sdk/go v0.0.0-20261020123705-bdb956f2cd65`, `google.golang.org/protobuf/encoding/protojson`, envtest, controller-gen.

**Depends on:** Plan 1 (repo exists; runtime `workspace-v3` schema at `runtime/docs/schemas/ach-workspace-config-v3.schema.json`; host API `PUT /host/v1/gateway`). Source of the port: ach tag `v0.14.2` (never ach main).

**Code map (read before Task 1):** `/home/coder/.claude/tmp/claude-1000/-home-coder-workspace-local-ach-runtime/0f976f5a-668c-4500-8ddc-cac010f2c821/scratchpad/operator-code-map.md` — file:line references below point into it.

## Global Constraints

- Module `github.com/ackstorm/ach-agent/operator` in `/home/coder/workspace/local/ach-agent/operator`. Go 1.26 (`GOTOOLCHAIN=go1.26.10` in the Makefile; the host has 1.25.6).
- API group `ach-agent.ackstorm.ai`, version `v1alpha1`, kinds `Agent`, `AgentProfile`. No shortNames.
- Label/annotation/finalizer prefix `ach-agent.ackstorm.ai/`: labels `ach-agent.ackstorm.ai/agent`, `ach-agent.ackstorm.ai/component`; annotation `ach-agent.ackstorm.ai/config-hash`; finalizer `ach-agent.ackstorm.ai/sandboxes`; pause annotation `ach-agent.ackstorm.ai/pause-host`. Sandbox labels `runtime.ach-agent.ackstorm.ai/agent-uid`, `runtime.ach-agent.ackstorm.ai/workspace-name`, `runtime.ach-agent.ackstorm.ai/port`. `app.kubernetes.io/name=ach-agent`, `app.kubernetes.io/managed-by=ach-agent-operator`.
- Resource names per Agent `<n>`: control StatefulSet and headless Service `ControlName(n)` (as v0.14.2: `agent-<n>`, hashed when long); ConfigMap `ControlName(n)+"-config"`; PVC `ControlName(n)+"-state"`; ClusterIP Service `ControlName(n)+"-http"`; HTTPRoute `ControlName(n)`; key Secret `ach-agent-key-<uid>` (data key `key`, 32 random bytes hex); control SA `ach-agent-control-<uid>` unless the profile names one.
- Control pod: label `istio.io/dataplane-mode: ambient` always; `automountServiceAccountToken: false`; non-root 10001; read-only root FS; drop ALL; seccomp RuntimeDefault; ports 8080 (http) and 8081 (host + facades). Headless Service on 8081 with `publishNotReadyAddresses: true` always exists.
- `terminationGracePeriodSeconds` must be >= `workspace.shutdownTimeoutSeconds` (runtime drain) + 15; else `Ready=False` reason `InvalidGracePeriod`.
- Config document `workspace-v3` in a ConfigMap (never a Secret). Header env names: model `ACH_AGENT_MODEL_H<i>`, MCP `ACH_AGENT_MCP_<NAME>_H<i>` (`<NAME>` = server name uppercased, `-` -> `_`; `<i>` = index in the header names sorted case-insensitively). ACH key env `ACH_SECRET_IDENTITY`. Literal header values become plain env vars with the same names.
- Status condition types: `Ready`, `ProfileResolved`, `SecretsResolved`, `WorkloadApplied`, `WorkloadReady`, `GatewayAvailable`. Reasons include `SandboxPolicyRejected`, `SandboxResourcesUnsupported`, `GatewayUnavailable`, `SecretMissing`, `SecretConflict`, `StorageUnavailable`, `InvalidGracePeriod`, `RenderFailed`, `ProfileNotFound`.
- Reserved policy rule names start with `ach-agent-`; Agent/profile rule names with that prefix are rejected. Runtime binaries (read-only, never writable): `/usr/local/bin/python3.13`, `/usr/local/bin/node`, `/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe`, and their directories `/usr/local/bin`, `/opt/opencode`. Default `landlock.compatibility`: `hard_requirement`.
- The operator never deletes a sandbox or reports a unit lost while the gateway is unavailable; the finalizer is removed only after an inventory shows no sandbox and no per-sandbox Service left.
- Watches all namespaces. Leader election ID `ach-agent-operator.ackstorm.ai`.
- Operator env: `ACH_AGENT_OPENSHELL_GATEWAY` (required; start fails without it), `ACH_AGENT_OPENSHELL_WORKSPACE` (default `default`), `ACH_AGENT_OPENSHELL_SERVICE_URL` (default `http://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080`), `ACH_AGENT_OPENSHELL_POD_NAMESPACE` (default `openshell`), `ACH_AGENT_NAMESPACE` (leader election namespace), `ACH_AGENT_STORAGE_S3_{BUCKET,REGION,ENDPOINT_URL,PREFIX,CREDENTIALS_SECRET}`, `ACH_AGENT_ROUTE_PARENT_{NAME,NAMESPACE,SECTION}` (default HTTPRoute parentRef).
- The operator is the only Kubernetes and gateway API client; it talks to the runtime only through `/host/v1`.
- Every new `.go` file starts with `// SPDX-License-Identifier: MIT` except `third_party/openshellconv` (keeps the NVIDIA Apache-2.0 header).
- Git: `/usr/bin/git`, never push, commit trailers:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
  ```
- Bounded waits only; never pipe a gate into `tail`/`grep`; never touch kind cluster `ach-e2e` or `/home/coder/workspace/local/ach`.

Test commands (from `operator/`): `make test` (unit, no cluster), `make test-envtest` (controller envtest), `make gen` (deepcopy + CRDs + RBAC), `make verify` (gen drift + vet + test).

---

### Task 1: Import the v0.14.2 agent code into a buildable module

**Files:**
- Create: `operator/go.mod`, `operator/Makefile`, `operator/hack/boilerplate.go.txt`, `operator/cmd/operator/main.go`
- Create (copied from ach `v0.14.2`, then trimmed): `operator/api/v1alpha1/*`, `operator/internal/render/*` (from `internal/agentrender`), `operator/internal/controller/*` (from `internal/controller/ach/achagent_*.go`, `suite_test.go`, `test_helpers_test.go`), `operator/internal/host/*` (from `internal/workspacehost`: `runner.go`, `host.go`, `client.go` + tests), `operator/internal/openshell/*` (from `internal/workspacehost`: `openshell.go`, `units.go` kept parts, `backend.go` + tests)

**Interfaces:**
- Produces: module compiles; `make test` green on `internal/render`, `internal/host`, `internal/openshell`; the code still uses the old kinds `ACHAgent`/`AgentProfile` (group renamed in Task 2).

- [ ] **Step 1: Extract the source**

```bash
cd /home/coder/workspace/local/ach-agent && /usr/bin/git checkout -b feat/operator
SRC=$(mktemp -d)
/usr/bin/git -C ../ach archive v0.14.2 api/ach/v1alpha1 internal/agentrender internal/workspacehost \
  internal/controller/ach hack/boilerplate.go.txt go.mod | tar -x -C "$SRC"
echo "$SRC"
```

- [ ] **Step 2: Copy with deletions**

Copy into `operator/` per the file list. Do NOT copy (gateway projection, legacy, StatefulSet backend, other CRDs):
- `internal/workspacehost/statefulset.go`; from `units.go` keep only `AgentUIDLabel`, `NameLabel`, `imageRelease`, `plainVersion` (rename label values per Global Constraints).
- `internal/controller/ach/*` except `achagent_controller.go`, `achagent_workload.go`, `achagent_workspace_key.go`, and their tests `achagent_envtest_test.go`, `achagent_finalizer_test.go`, `achagent_workload_test.go`, `achagent_workspace_key_test.go`, `achagent_storage_test.go`, `achagent_storage_envtest_test.go`, `achagent_hostloop_test.go`, `suite_test.go`, `test_helpers_test.go`. In `achagent_controller.go` delete: fields `DB`, `PublicBaseURL`; funcs `agentGatewayURL`, `agentProjectionRow`, `writeAgentProjection`, `deleteAgentProjection`; `Status.GatewayURL` writes; calls to `pruneLegacyDeployment`, `pruneLegacySandbox`, `pruneHarnessAccess`; NetworkPolicy apply/prune and `Owns(&networkingv1.NetworkPolicy{})`; podTemplate overlay (`applyPodTemplateOverlay`, `replaceReserved*`, `PodTemplateInvalid`). Keep `replaceStaleControlPod`.
- `api/ach/v1alpha1`: only `achagent_types.go`, `agentprofile_types.go`, `groupversion_info.go`, and the shared types they need (`LocalObjectRef`; move `SecretKeyRef` from `litellmconnection_types.go` into a new `shared_types.go`). Drop fields `ExecutionInfraSpec.Backend`, `AgentProfileSpec.PodTemplate`, `AgentProfileSpec.NetworkPolicy`, `ACHAgentStatus.GatewayURL`, `ExposeSpec.Gateway`.
- In `host.go`/`runner.go`: delete the `Backends` map, `every`/`active` helpers and inactive-backend error paths; the Host holds one `backend` (the OpenShell one). Delete obsolete tests (`stubBackend`, `TestPass_InactiveBackend*`, `TestPass_BackendSwitch*`).
- Replace `config.EnvOr` with a local `envOr(key, def string) string` in `internal/openshell`.
- `cmd/operator/main.go`: port only the agent-relevant parts of `cmd/ach/cmd/operator.go` (map §3): stdlib `flag` (`--metrics-bind-address` default `:8080`, `--health-probe-bind-address` default `:8081`, `--leader-elect`), zap logger, scheme (clientgo + `v1alpha1`), manager with leader election ID `ach-agent-operator.ackstorm.ai` and namespace from `ACH_AGENT_NAMESPACE`, NO cache namespace restriction, health/ready pings, reconciler + `host.Runner` registration.

Rewrite imports from `github.com/ackstorm/ach/...` to `github.com/ackstorm/ach-agent/operator/...` (`internal/agentrender` -> `internal/render`, `internal/workspacehost` -> `internal/host` or `internal/openshell` by file). Package names follow directories. Replace SPDX `Apache-2.0` with `MIT` in copied files.

- [ ] **Step 3: go.mod and Makefile**

`operator/go.mod`: `module github.com/ackstorm/ach-agent/operator`, `go 1.26.0`, `toolchain go1.26.10`; then `GOTOOLCHAIN=go1.26.10 go mod tidy`.

`operator/Makefile`:

```make
export GOTOOLCHAIN := go1.26.10
CONTROLLER_GEN := go run sigs.k8s.io/controller-tools/cmd/controller-gen@v0.20.0
ENVTEST := go run sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.24
ENVTEST_K8S_VERSION := 1.36.x
ENVTEST_BIN := $(CURDIR)/bin/envtest
UNIT_PKGS = $$(go list ./... | grep -v /internal/controller)

.PHONY: gen test test-envtest verify build

gen: ## deepcopy, CRDs and RBAC
	$(CONTROLLER_GEN) object:headerFile=hack/boilerplate.go.txt paths="./api/..."
	$(CONTROLLER_GEN) rbac:roleName=ach-agent-operator crd paths="./api/..." paths="./internal/..." \
	  output:crd:artifacts:config=config/crd output:rbac:artifacts:config=config/rbac

build:
	go build ./...

test: ## unit tests (no cluster)
	go vet ./...
	go test -race -count=1 $(UNIT_PKGS)

test-envtest: gen ## controller envtest
	KUBEBUILDER_ASSETS="$$($(ENVTEST) use $(ENVTEST_K8S_VERSION) --bin-dir $(ENVTEST_BIN) -p path)" \
	  go test -race -count=1 -timeout 15m ./internal/controller/...

verify: gen test
	@/usr/bin/git diff --exit-code -- config api || { echo "FAIL: generated files drifted; run make gen" >&2; exit 1; }
```

If `controller-tools@v0.20.0` is not the version matching controller-runtime v0.24, pick the matching release (check its go.mod) and note it. In `suite_test.go`, set the CRD path to `config/crd` and remove the hardcoded `/opt/envtest` probes (KUBEBUILDER_ASSETS comes from the Makefile).

- [ ] **Step 4: Build and test**

```bash
cd /home/coder/workspace/local/ach-agent/operator
timeout 900 make build > /tmp/op-build.log 2>&1; echo "EXIT=$?"
timeout 900 make test > /tmp/op-test.log 2>&1; echo "EXIT=$?"; grep -E "^(ok|FAIL|---)" /tmp/op-test.log | head -40
```

Expected: both `EXIT=0`. Envtest is not required to pass in this task (Task 2 reshapes the API).

- [ ] **Step 5: Commit**

```bash
cd /home/coder/workspace/local/ach-agent
/usr/bin/git add operator
/usr/bin/git commit -q -m "chore(operator): import the ACHAgent operator from ach v0.14.2

Plain copy of api/ach/v1alpha1 (agent types), internal/agentrender,
internal/workspacehost and internal/controller/ach (agent files) at v0.14.2,
without the gateway projection, legacy cleanup, StatefulSet backend,
NetworkPolicy and podTemplate overlay.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 2: API `ach-agent.ackstorm.ai/v1alpha1` and `workspace-v3` rendering

**Files:**
- Modify/rename: `operator/api/v1alpha1/groupversion_info.go`, `achagent_types.go` -> `agent_types.go`, `agentprofile_types.go`
- Modify: `operator/internal/render/*` (Render2 -> `Render`), `operator/internal/render/outputschema.go` (embed v3 schema), `operator/internal/render/schema_test.go` (drift path)
- Copy: `runtime/docs/schemas/ach-workspace-config-v3.schema.json` -> `operator/internal/render/testdata/` ; regenerate `testdata/config-canonicalization.json` from `runtime/tests/config/fixtures/config-canonicalization.json`
- Create: `operator/internal/render/headers.go`, `operator/internal/render/headers_test.go`, `operator/internal/render/render_v3_test.go`
- Generated: `operator/config/crd/*.yaml`, `zz_generated.deepcopy.go`

**Interfaces:**
- Produces (Go types in `api/v1alpha1`):
  - `type HeaderValue struct { SecretKeyRef *SecretKeyRef `json:"secretKeyRef,omitempty"`; Value *string `json:"value,omitempty"` }` (CEL: exactly one set)
  - `StandaloneModelSpec{Name, API (Enum openai;anthropic;gemini), BaseURL, Headers map[string]HeaderValue, Params *apiextensionsv1.JSON, Thinking *ThinkingSpec, Limits *ModelLimitsSpec{Context, Output int64 Min 1}}`
  - `StandaloneMcpServerSpec{Name (Pattern ^[a-z0-9][a-z0-9_-]{0,62}$), URL, Headers map[string]HeaderValue}`
  - `StandaloneSpec{Model StandaloneModelSpec; McpServers []StandaloneMcpServerSpec (listType=map key=name)}`
  - `AchServerSpec{BaseURL, Environment, KeySecretRef SecretKeyRef, Model AchModelSpec{Name, API (Enum, default openai), Params, Thinking}}`
  - `AgentSpec{ProfileRef, CapabilityServer string (Enum standalone;ach default standalone), Standalone *StandaloneSpec, Ach *AchServerSpec, Filter *FilterSpec, Memory, Prompt, Engine, Limits, Workspace, Hooks, Artifacts, Channels, Env, Sandbox *AgentSandboxSpec{OpenShell *AgentOpenShellSpec{Policy *apiextensionsv1.JSON}}, Expose *ExposeSpec{Service bool, Route *RouteSpec{Hostname, Path (default "/"), ParentRef *ParentRef{Name, Namespace, SectionName}}}}`
  - `AgentStatus{ObservedGeneration int64, Conditions []metav1.Condition}`
  - `AgentProfileSpec{Control ControlSpec{Image, ServiceAccountName, Resources, NodeSelector, Tolerations, ImagePullSecrets, Env, TerminationGracePeriodSeconds *int64, Persistence *PersistenceSpec}, Sandbox ProfileSandboxSpec{OpenShell OpenShellSpec{Image, Resources *SandboxResources{CPU, Memory string}, ReadyTimeoutSeconds *int64, Policy *apiextensionsv1.JSON}}, Defaults AgentDefaults{Engine, Limits, Workspace, Artifacts}}`
  - `render.Render(p AgentProfile, a Agent) (WSConfig, []EnvBinding, error)` where `EnvBinding{Name string; SecretKeyRef *SecretKeyRef; Value *string}` lists every header/key env the control pod needs.
  - `render.HeaderEnv(prefix string, headers map[string]HeaderValue) (map[string]WSSecretAlias, []EnvBinding)`

Field mapping: follow the spec's "Field mapping from ACHAgent v0.14.2" tables exactly; sub-types (`ChannelSpec`, `MemorySpec`, `HooksSpec`, `EngineSpec`, `LimitsSpec`, `WorkspaceSpec`, `ArtifactsSpec`, `PromptSpec`, `PersistenceSpec`) keep their v0.14.2 fields and markers.

CEL rules (on `AgentSpec`):
- `self.capabilityServer == 'standalone' ? (has(self.standalone) && !has(self.ach)) : (has(self.ach) && !has(self.standalone))`, message `capabilityServer selects exactly one of standalone/ach`
- `!has(self.expose) || !has(self.expose.route) || self.expose.service`, message `expose.route requires expose.service`
- `!has(self.filter) || !has(self.filter.exclude) || !has(self.filter.exclude.skills) || size(self.filter.exclude.skills) == 0 || self.capabilityServer == 'ach'`, message `filter.exclude.skills requires capabilityServer ach`
- On `HeaderValue`: `has(self.secretKeyRef) != has(self.value)`.
- Object-level (kept from v0.14.2): name <= 50; name must not match `-[0-9a-f]{20}$`.
- On `AgentProfile`: `spec.sandbox.openshell.image` non-empty; `spec.defaults.limits`, `.engine.compaction`, `.workspace`, `.artifacts` required (v0.14.2 rules, re-pointed).

- [ ] **Step 1: Write the failing render tests**

`operator/internal/render/headers_test.go`:

```go
// SPDX-License-Identifier: MIT
package render

import (
	"reflect"
	"testing"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func ptr[T any](v T) *T { return &v }

func TestHeaderEnv_SortsCaseInsensitivelyAndBindsSecretsAndLiterals(t *testing.T) {
	aliases, bindings := HeaderEnv("ACH_AGENT_MODEL", map[string]v1.HeaderValue{
		"x-litellm-api-key": {SecretKeyRef: &v1.SecretKeyRef{Name: "litellm", Key: "apiKey"}},
		"Accept-Language":   {Value: ptr("en")},
	})
	wantAliases := map[string]WSSecretAlias{
		"Accept-Language":   {Env: "ACH_AGENT_MODEL_H0"},
		"x-litellm-api-key": {Env: "ACH_AGENT_MODEL_H1"},
	}
	if !reflect.DeepEqual(aliases, wantAliases) {
		t.Fatalf("aliases = %#v", aliases)
	}
	if bindings[0].Name != "ACH_AGENT_MODEL_H0" || *bindings[0].Value != "en" {
		t.Fatalf("binding 0 = %#v", bindings[0])
	}
	if bindings[1].Name != "ACH_AGENT_MODEL_H1" || bindings[1].SecretKeyRef.Name != "litellm" {
		t.Fatalf("binding 1 = %#v", bindings[1])
	}
}

func TestMcpEnvPrefix_UppercasesAndReplacesDashes(t *testing.T) {
	if got := McpEnvPrefix("web-search"); got != "ACH_AGENT_MCP_WEB_SEARCH" {
		t.Fatalf("got %q", got)
	}
}
```

`operator/internal/render/render_v3_test.go` (fixture builders adapted from `minimalRender2Fixture` in the v0.14.2 `render2_test.go`):

```go
// SPDX-License-Identifier: MIT
package render

import (
	"encoding/json"
	"strings"
	"testing"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func standaloneFixture() (v1.AgentProfile, v1.Agent) {
	p, a := minimalFixture() // profile with full defaults, agent with one webhook channel
	a.Spec.CapabilityServer = "standalone"
	a.Spec.Standalone = &v1.StandaloneSpec{
		Model: v1.StandaloneModelSpec{
			Name: "anthropic/claude-sonnet-5.5", API: "openai", BaseURL: "https://litellm.example.com/v1",
			Headers: map[string]v1.HeaderValue{"x-litellm-api-key": {SecretKeyRef: &v1.SecretKeyRef{Name: "litellm", Key: "apiKey"}}},
			Limits:  &v1.ModelLimitsSpec{Context: 200000, Output: 32000},
		},
		McpServers: []v1.StandaloneMcpServerSpec{{
			Name: "serper", URL: "https://mcp.serper.dev/mcp",
			Headers: map[string]v1.HeaderValue{"X-API-KEY": {SecretKeyRef: &v1.SecretKeyRef{Name: "serper", Key: "apiKey"}}},
		}},
	}
	return p, a
}

func TestRender_StandaloneProducesV3CapabilityAndEnvBindings(t *testing.T) {
	p, a := standaloneFixture()
	cfg, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	doc := string(raw)
	for _, want := range []string{`"schemaVersion":"workspace-v3"`, `"server":"standalone"`,
		`"x-litellm-api-key":{"env":"ACH_AGENT_MODEL_H0"}`, `"X-API-KEY":{"env":"ACH_AGENT_MCP_SERPER_H0"}`,
		`"limits":{"context":200000,"output":32000}`, `"type":"openai"`} {
		if !strings.Contains(doc, want) {
			t.Errorf("config lacks %s\n%s", want, doc)
		}
	}
	if strings.Contains(doc, `"ach":`) || strings.Contains(doc, `"model":{"name"`) && !strings.Contains(doc, `"capability"`) {
		t.Errorf("v2 blocks leaked: %s", doc)
	}
	names := map[string]bool{}
	for _, b := range env {
		names[b.Name] = true
	}
	if !names["ACH_AGENT_MODEL_H0"] || !names["ACH_AGENT_MCP_SERPER_H0"] || names["ACH_SECRET_IDENTITY"] {
		t.Fatalf("env bindings = %#v", env)
	}
}

func TestRender_AchProducesKeyAliasAndModelApi(t *testing.T) {
	p, a := minimalFixture()
	a.Spec.CapabilityServer = "ach"
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://api.example", Environment: "prod",
		KeySecretRef: v1.SecretKeyRef{Name: "ek", Key: "token"}, Model: v1.AchModelSpec{Name: "ackstorm.smart", API: "anthropic"}}
	cfg, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	for _, want := range []string{`"server":"ach"`, `"key":{"env":"ACH_SECRET_IDENTITY"}`, `"type":"anthropic"`, `"environment":"prod"`} {
		if !strings.Contains(string(raw), want) {
			t.Errorf("config lacks %s: %s", want, raw)
		}
	}
	if env[0].Name != "ACH_SECRET_IDENTITY" || env[0].SecretKeyRef.Name != "ek" {
		t.Fatalf("env = %#v", env)
	}
}

func TestRender_McpEnvPrefixCollisionFails(t *testing.T) {
	p, a := standaloneFixture()
	a.Spec.Standalone.McpServers = append(a.Spec.Standalone.McpServers,
		v1.StandaloneMcpServerSpec{Name: "a-b", URL: "https://x"}, v1.StandaloneMcpServerSpec{Name: "a_b", URL: "https://y"})
	if _, _, err := Render(p, a); err == nil || !strings.Contains(err.Error(), "env prefix") {
		t.Fatalf("err = %v", err)
	}
}
```

The existing JSON-schema conformance test (`TestRender2_FullOutputConformsToSchemaAndJCSHash`) is kept, renamed to `TestRender_FullOutputConformsToSchemaAndJCSHash`, and run for both fixtures.

- [ ] **Step 2: Run to verify failure**

Run: `cd operator && go test ./internal/render/... -count=1`
Expected: FAIL (undefined `HeaderEnv`, `Render`, new types).

- [ ] **Step 3: Implement the API types**

Rewrite `api/v1alpha1` per the Interfaces block: `GroupVersion = schema.GroupVersion{Group: "ach-agent.ackstorm.ai", Version: "v1alpha1"}`; kinds `Agent`/`AgentList`, `AgentProfile`/`AgentProfileList`; markers `+kubebuilder:object:root=true`, `+kubebuilder:subresource:status`, `+kubebuilder:resource:scope=Namespaced`, printcolumns `Ready` (`.status.conditions[?(@.type=="Ready")].status`) and `Age`. `Policy` fields use `+kubebuilder:pruning:PreserveUnknownFields` and `+kubebuilder:validation:Type=object`. Then `make gen`.

- [ ] **Step 4: Implement rendering**

`internal/render/headers.go`:

```go
// SPDX-License-Identifier: MIT
package render

import (
	"fmt"
	"sort"
	"strings"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// EnvBinding is one env var the control pod needs: from a Secret key or a literal.
type EnvBinding struct {
	Name         string
	SecretKeyRef *v1.SecretKeyRef
	Value        *string
}

// HeaderEnv names one env var per header (<prefix>_H<i>, i in case-insensitive name order)
// and returns the config aliases plus the pod bindings.
func HeaderEnv(prefix string, headers map[string]v1.HeaderValue) (map[string]WSSecretAlias, []EnvBinding) {
	names := make([]string, 0, len(headers))
	for name := range headers {
		names = append(names, name)
	}
	sort.Slice(names, func(i, j int) bool { return strings.ToLower(names[i]) < strings.ToLower(names[j]) })
	aliases := make(map[string]WSSecretAlias, len(names))
	bindings := make([]EnvBinding, 0, len(names))
	for i, name := range names {
		env := fmt.Sprintf("%s_H%d", prefix, i)
		aliases[name] = WSSecretAlias{Env: env}
		h := headers[name]
		bindings = append(bindings, EnvBinding{Name: env, SecretKeyRef: h.SecretKeyRef, Value: h.Value})
	}
	return aliases, bindings
}

// McpEnvPrefix maps an MCP server name to its env prefix.
func McpEnvPrefix(server string) string {
	return "ACH_AGENT_MCP_" + strings.ToUpper(strings.ReplaceAll(server, "-", "_"))
}
```

In `render.go`/`render2.go` (rename `Render2` -> `Render`, signature per Interfaces, drop `defaultBaseURL`): replace `WSConfig.Ach` and `WSConfig.Model` with `Capability WSCapabilityBlock` matching the runtime schema exactly:

```go
type WSCapabilityBlock struct {
	Server     string            `json:"server"`
	Standalone *WSStandalone     `json:"standalone,omitempty"`
	Ach        *WSAchServer      `json:"ach,omitempty"`
	Filter     WSFilterBlock     `json:"filter"`
}
type WSStandalone struct {
	Model      WSStandaloneModel `json:"model"`
	McpServers []WSMcpServer     `json:"mcpServers"`
}
type WSStandaloneModel struct {
	Name     string                   `json:"name"`
	Type     string                   `json:"type"`
	BaseURL  string                   `json:"baseUrl"`
	Headers  map[string]WSSecretAlias `json:"headers"`
	Params   map[string]any           `json:"params"`
	Thinking *ThinkingBlock           `json:"thinking,omitempty"`
	Limits   *WSModelLimits           `json:"limits,omitempty"`
}
type WSModelLimits struct {
	Context int64 `json:"context"`
	Output  int64 `json:"output"`
}
type WSMcpServer struct {
	Name    string                   `json:"name"`
	URL     string                   `json:"url"`
	Headers map[string]WSSecretAlias `json:"headers"`
}
type WSAchServer struct {
	BaseURL     string        `json:"baseUrl"`
	Environment string        `json:"environment"`
	Key         WSSecretAlias `json:"key"`
	Model       ModelBlock    `json:"model"`
}
```

`Filter` always renders `{"exclude":{"tools":[],"mcpServers":[],"skills":[]}}` with non-null arrays. `SchemaVersion: "workspace-v3"`. Standalone: MCP env prefixes must be unique (else error `mcp server %q and %q map to the same env prefix %s`). ACH: binding `ACH_SECRET_IDENTITY` from `KeySecretRef` first in the returned slice. Memory bearer secrets, channel secrets keep their v0.14.2 env names and are also returned as bindings (or keep the existing `ChannelSecretEnv`/`MemorySecretEnv` helpers and append them in the controller — pick one, don't do both). `ReferencedSecrets(p, a)` must now include header Secrets and the ACH key Secret. Delete `ResolveAch*`, `ResolveImage`, `ResolveModel` and the `Health`/`Image` handling; `workspace.readyTimeoutSeconds` comes from `p.Spec.Sandbox.OpenShell.ReadyTimeoutSeconds`.

`outputschema.go`: embed `testdata/ach-workspace-config-v3.schema.json`; rename `validateWorkspaceV2Output` -> `validateWorkspaceOutput`. `schema_test.go`: drift test compares with `../../../runtime/docs/schemas/ach-workspace-config-v3.schema.json` and FAILS (not skips) when the file is missing.

- [ ] **Step 5: Run unit tests**

Run: `cd operator && make gen && make test`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A operator
/usr/bin/git commit -q -m "feat(operator): Agent/AgentProfile in ach-agent.ackstorm.ai rendering workspace-v3" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 3: Agent reconciler (workload, Secrets, exposure)

**Files:**
- Modify: `operator/internal/controller/agent_controller.go` (from `achagent_controller.go`), `agent_workload.go`, `agent_workspace_key.go`; tests `agent_envtest_test.go`, `agent_workload_test.go`, `agent_workspace_key_test.go`, `agent_storage_test.go`
- Create: `operator/internal/controller/agent_route.go`, `agent_route_test.go`

**Interfaces:**
- Consumes: `render.Render`, `render.EnvBinding`, `render.ReferencedSecrets` (Task 2).
- Produces: `AgentReconciler{Client, APIReader, Scheme, Storage render.RuntimeStorageOptions, RouteParent v1.ParentRef, HTTPRouteAvailable bool}`; key Secret `ach-agent-key-<uid>`; conditions per Global Constraints.

- [ ] **Step 1: Write the failing tests**

Fake-client unit tests in `agent_workload_test.go` (style of v0.14.2 `buildTestControlSTS`):

```go
func TestBuildStatefulSet_AmbientLabelPortsAndHeaderEnv(t *testing.T) {
	sts := buildTestControlSTS(t) // standalone fixture with one model header from Secret "litellm"
	labels := sts.Spec.Template.Labels
	if labels["istio.io/dataplane-mode"] != "ambient" {
		t.Fatalf("labels = %v", labels)
	}
	c := sts.Spec.Template.Spec.Containers[0]
	ports := map[int32]bool{}
	for _, p := range c.Ports {
		ports[p.ContainerPort] = true
	}
	if !ports[8080] || !ports[8081] {
		t.Fatalf("ports = %v", c.Ports)
	}
	var found bool
	for _, e := range c.Env {
		if e.Name == "ACH_AGENT_MODEL_H0" {
			found = e.ValueFrom != nil && e.ValueFrom.SecretKeyRef.Name == "litellm"
		}
	}
	if !found {
		t.Fatalf("ACH_AGENT_MODEL_H0 not wired from Secret litellm: %v", c.Env)
	}
	if *sts.Spec.Template.Spec.AutomountServiceAccountToken {
		t.Fatal("token automount must be off")
	}
}

func TestGraceBelowDrainPlus15IsRejected(t *testing.T) {
	p, a := testProfileAndAgent()
	p.Spec.Control.TerminationGracePeriodSeconds = ptr(int64(30))
	a.Spec.Workspace = &v1.WorkspaceSpec{ShutdownTimeoutSeconds: ptr(int64(20))}
	if err := checkGracePeriod(&p, &a); err == nil || !strings.Contains(err.Error(), "35") {
		t.Fatalf("err = %v", err)
	}
}
```

`agent_route_test.go`:

```go
func TestBuildHTTPRoute_UsesHostnamePathAndDefaultParent(t *testing.T) {
	a := testAgent("researcher")
	a.Spec.Expose = &v1.ExposeSpec{Service: true, Route: &v1.RouteSpec{Hostname: "r.example.com", Path: "/"}}
	route := buildHTTPRoute(&a, v1.ParentRef{Name: "public", Namespace: "gateway"})
	if string(route.Spec.Hostnames[0]) != "r.example.com" || string(route.Spec.ParentRefs[0].Name) != "public" {
		t.Fatalf("route = %#v", route.Spec)
	}
	backend := route.Spec.Rules[0].BackendRefs[0]
	if string(backend.Name) != ControlName("researcher")+"-http" || *backend.Port != 8080 {
		t.Fatalf("backend = %#v", backend)
	}
}
```

Envtest (`agent_envtest_test.go`, adapting `TestACHAgent_HappyPath_AppliesConfigMapAndDeployment`): 
- `TestAgent_HappyPath_Standalone` — creates Secrets `litellm`, `serper`, an `AgentProfile`, a standalone `Agent`; waits `WorkloadApplied=True`; asserts ConfigMap `config.json` has `"schemaVersion":"workspace-v3"`, headless Service on 8081 with `publishNotReadyAddresses`, key Secret `ach-agent-key-<uid>` (64 hex chars, controller ownerRef), finalizer `ach-agent.ackstorm.ai/sandboxes` present.
- `TestAgent_MissingHeaderSecret_SecretMissing` — no `serper` Secret -> `Ready=False`, reason `SecretMissing`; creating the Secret -> `WorkloadApplied=True`.
- `TestAgent_ForeignKeySecret_SecretConflict` — pre-create `ach-agent-key-<uid>`... (uid unknown before create: create the Agent with the profile missing, read its UID, create a foreign Secret with that name, then create the profile) -> reason `SecretConflict`.
- `TestAgent_SecretChangeRollsPodTemplate` — change the `litellm` Secret data -> the StatefulSet's `ach-agent.ackstorm.ai/config-hash` annotation changes.
- `TestAgent_StorageUnavailable` — workspace persistence enabled, operator without bucket -> `Ready=False`, reason `StorageUnavailable`.
- `TestAgent_CEL_*` — both-servers set, route without service, skills exclusion in standalone, header with both secretKeyRef and value: each rejected by the apiserver with the CEL message.

- [ ] **Step 2: Run to verify failure**

Run: `cd operator && make test && make test-envtest`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

In the reconciler (ported flow, map §4):
1. Fetch Agent; if deleting -> Task 5 finalizer path (leave the existing call for now).
2. Always ensure finalizer `ach-agent.ackstorm.ai/sandboxes`.
3. Profile -> `ProfileResolved`.
4. `render.Render(profile, agent)` -> `WorkloadApplied=False/RenderFailed` on error.
5. `checkGracePeriod` -> `Ready=False/InvalidGracePeriod`:

```go
func checkGracePeriod(p *v1.AgentProfile, a *v1.Agent) error {
	grace := int64(defaultGraceSeconds)
	if g := p.Spec.Control.TerminationGracePeriodSeconds; g != nil {
		grace = *g
	}
	drain := effectiveShutdownTimeout(p, a) // agent.workspace over profile defaults, runtime default 60
	if min := drain + 15; grace < min {
		return fmt.Errorf("control terminationGracePeriodSeconds %d is below workspace.shutdownTimeoutSeconds + 15 = %d", grace, min)
	}
	return nil
}
```

(Use the runtime's real default for `shutdownTimeoutSeconds`; read it from `runtime/src/ach_runtime/config/schema.py` `WorkspaceBlock`.)
6. Storage check -> set `WorkloadApplied=False/StorageUnavailable` and let `Ready` copy it (reason surfaces on `Ready`).
7. Secrets: every `ReferencedSecrets` entry (headers, ACH key, channels, memory, storage credentials) via `APIReader`; missing Secret or key -> `SecretsResolved=False/SecretMissing` with `secret <name> key <key>`.
8. Workspace key: `ach-agent-key-<uid>`; foreign/empty -> `SecretsResolved=False/SecretConflict`.
9. Env: `EnvBinding` -> `corev1.EnvVar` (secretKeyRef or value) + `ACH_SANDBOX_KEY` from the key Secret + storage env + user `env`; hash all referenced Secret contents into `configHash` (v0.14.2 `hashSecrets`).
10. Apply: ConfigMap, control SA (`AutomountServiceAccountToken=false`), headless Service 8081, PVC (if persistence), StatefulSet (label `istio.io/dataplane-mode: ambient`, ports 8080+8081), ClusterIP Service `-http` (if `expose.service`, else prune), HTTPRoute (if `expose.route` and `HTTPRouteAvailable`, else prune; if route requested but the CRD is absent -> `WorkloadApplied=False/RouteUnsupported`).
11. `WorkloadReady` as v0.14.2.

`agent_route.go`: `buildHTTPRoute(a *v1.Agent, def v1.ParentRef) *gatewayv1.HTTPRoute` using `sigs.k8s.io/gateway-api/apis/v1`; parentRef from `route.parentRef` else `def`; one rule, `PathPrefix` match on `route.path`, backend `ControlName(a.Name)+"-http"` port 8080; controller ownerRef. `HTTPRouteAvailable` is decided in `main.go` via discovery (`gateway.networking.k8s.io/v1` `httproutes` served); `Owns(&gatewayv1.HTTPRoute{})` only when available. Add `copySpec` cases for HTTPRoute.

RBAC markers (`+kubebuilder:rbac`): agents (+status, +finalizers) full; agentprofiles get/list/watch; statefulsets, services, configmaps, serviceaccounts, persistentvolumeclaims full; secrets get/list/watch/create; pods get/list/watch/delete (delete only for `replaceStaleControlPod`); events create/patch; httproutes full. Then `make gen`.

- [ ] **Step 4: Run tests**

Run: `cd operator && make test && timeout 1200 make test-envtest > /tmp/op-envtest.log 2>&1; echo "EXIT=$?"; grep -E "^(ok|FAIL|--- FAIL)" /tmp/op-envtest.log`
Expected: `EXIT=0`, no FAIL.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A operator
/usr/bin/git commit -q -m "feat(operator): Agent reconciler with header Secrets, key Secret and HTTPRoute" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 4: Sandbox policy merge, reserved rules and resources

**Files:**
- Create: `operator/third_party/openshellconv/{policy.go,network_policy.go,LICENSE,NOTICE}` (copied from the SDK module's `openshell/v1/internal/converter/` at the pinned version), `operator/internal/openshell/policy.go`, `operator/internal/openshell/policy_test.go`
- Modify: `operator/internal/openshell/openshell.go` (Create: replace the hardcoded policy L244-267, pass resources)

**Interfaces:**
- Produces:
  - `openshell.BuildPolicy(profile, agent *apiextensionsv1.JSON, facadeHost string, facadePort uint32) (*ostypes.SandboxPolicy, error)` — errors wrap `ErrPolicyRejected`
  - `openshell.Resources(r *v1.SandboxResources) (map[string]any, error)` — errors wrap `ErrResourcesUnsupported`
  - `var ErrPolicyRejected, ErrResourcesUnsupported error`

- [ ] **Step 1: Vendor the converter**

```bash
cd /home/coder/workspace/local/ach-agent/operator
SDK=$(GOTOOLCHAIN=go1.26.10 go list -m -f '{{.Dir}}' github.com/NVIDIA/OpenShell/sdk/go)
mkdir -p third_party/openshellconv
cp "$SDK/openshell/v1/internal/converter/policy.go" "$SDK/openshell/v1/internal/converter/network_policy.go" third_party/openshellconv/
cp "$SDK/LICENSE" third_party/openshellconv/LICENSE
chmod u+w third_party/openshellconv/*
```

Change `package converter` -> `package openshellconv`; keep the NVIDIA headers; write `NOTICE`: "Copied from github.com/NVIDIA/OpenShell/sdk/go v0.0.0-20261020123705-bdb956f2cd65, openshell/v1/internal/converter (Apache-2.0). Unmodified except the package name. Replace with the SDK's exported converter when one exists." If the files need other internal helpers, copy those too (same rules) or `go build` will say which.

- [ ] **Step 2: Write the failing tests**

`operator/internal/openshell/policy_test.go`:

```go
// SPDX-License-Identifier: MIT
package openshell

import (
	"errors"
	"testing"

	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
)

func raw(s string) *apiextensionsv1.JSON { return &apiextensionsv1.JSON{Raw: []byte(s)} }

func TestBuildPolicy_ReservedFacadeRuleAndDefaults(t *testing.T) {
	p, err := BuildPolicy(nil, nil, "agent-r.ns.svc.cluster.local", 8081)
	if err != nil {
		t.Fatal(err)
	}
	rule, ok := p.NetworkPolicies["ach-agent-facades"]
	if !ok || rule.Endpoints[0].Host != "agent-r.ns.svc.cluster.local" || rule.Endpoints[0].Port != 8081 {
		t.Fatalf("facade rule = %#v", p.NetworkPolicies)
	}
	if len(rule.Binaries) != 3 {
		t.Fatalf("binaries = %#v", rule.Binaries)
	}
	if p.Landlock.Compatibility != "hard_requirement" {
		t.Fatalf("landlock = %#v", p.Landlock)
	}
}

func TestBuildPolicy_AgentAddsEgressAndOverridesProfileRuleByName(t *testing.T) {
	profile := raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"pypi.org","port":443}]}}}`)
	agent := raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"files.pythonhosted.org","port":443}]},
	  "serper":{"name":"serper","endpoints":[{"host":"google.serper.dev","port":443}]}},
	  "landlock":{"compatibility":"best_effort"}}`)
	p, err := BuildPolicy(profile, agent, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if p.NetworkPolicies["pypi"].Endpoints[0].Host != "files.pythonhosted.org" {
		t.Fatalf("agent must win by name: %#v", p.NetworkPolicies["pypi"])
	}
	if _, ok := p.NetworkPolicies["serper"]; !ok {
		t.Fatal("agent egress dropped")
	}
	if p.Landlock.Compatibility != "best_effort" {
		t.Fatal("agent may set landlock")
	}
}

func TestBuildPolicy_FilesystemListsConcatenate(t *testing.T) {
	profile := raw(`{"filesystem_policy":{"read_only":["/data"]}}`)
	agent := raw(`{"filesystem_policy":{"read_write":["/scratch"]}}`)
	p, err := BuildPolicy(profile, agent, "f", 8081)
	if err != nil {
		t.Fatal(err)
	}
	if !contains(p.Filesystem.ReadOnly, "/data") || !contains(p.Filesystem.ReadWrite, "/scratch") ||
		!contains(p.Filesystem.ReadWrite, "/tmp") || !contains(p.Filesystem.ReadOnly, "/usr") {
		t.Fatalf("filesystem = %#v", p.Filesystem)
	}
}

func TestBuildPolicy_Rejections(t *testing.T) {
	for name, doc := range map[string]string{
		"unknown field":         `{"network_policy":{}}`,
		"reserved rule name":    `{"network_policies":{"ach-agent-x":{"name":"ach-agent-x"}}}`,
		"writable runtime bin":  `{"filesystem_policy":{"read_write":["/usr/local/bin"]}}`,
		"writable opencode dir": `{"filesystem_policy":{"read_write":["/opt/opencode/node_modules"]}}`,
		"writable parent":       `{"filesystem_policy":{"read_write":["/opt"]}}`,
	} {
		t.Run(name, func(t *testing.T) {
			if _, err := BuildPolicy(nil, raw(doc), "f", 8081); !errors.Is(err, ErrPolicyRejected) {
				t.Fatalf("err = %v", err)
			}
		})
	}
}

func TestResources_MapsCpuMemoryAndRejectsOthers(t *testing.T) {
	m, err := Resources(&v1.SandboxResources{CPU: "2", Memory: "4Gi"})
	if err != nil || m["cpu"] != "2" || m["memory"] != "4Gi" {
		t.Fatalf("m=%v err=%v", m, err)
	}
}

func contains(xs []string, x string) bool {
	for _, v := range xs {
		if v == x {
			return true
		}
	}
	return false
}
```

(Add the `v1` import. If the SDK's template `Resources map[string]any` expects a different key layout — check how the gateway reads it in the SDK docs/`driver` code — assert that layout instead and note it.)

- [ ] **Step 3: Run to verify failure**

Run: `cd operator && go test ./internal/openshell/ -run 'BuildPolicy|Resources' -count=1`
Expected: FAIL (undefined `BuildPolicy`).

- [ ] **Step 4: Implement `policy.go`**

```go
// SPDX-License-Identifier: MIT
package openshell

import (
	"errors"
	"fmt"
	"path"
	"strings"

	sbv1 "github.com/NVIDIA/OpenShell/sdk/go/proto/sandboxv1"
	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
	"google.golang.org/protobuf/encoding/protojson"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	"github.com/ackstorm/ach-agent/operator/third_party/openshellconv"
)

var (
	ErrPolicyRejected       = errors.New("sandbox policy rejected")
	ErrResourcesUnsupported = errors.New("sandbox resources unsupported")
)

const reservedPrefix = "ach-agent-"

// runtimeBinaries are the only binaries allowed to reach the facades; they and their
// directories stay read-only so a sandboxed process cannot replace a trusted binary.
var runtimeBinaries = []string{
	"/usr/local/bin/python3.13",
	"/usr/local/bin/node",
	"/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe",
}
var runtimeReadOnly = []string{"/usr", "/lib", "/etc", "/bin", "/sbin", "/opt"}
var runtimeReadWrite = []string{"/tmp", "/workspace", "/var/lib/ach-runtime", "/home/ach"}
var protectedDirs = []string{"/usr/local/bin", "/opt/opencode"}

func decode(doc *apiextensionsv1.JSON) (*sbv1.SandboxPolicy, error) {
	p := &sbv1.SandboxPolicy{}
	if doc == nil || len(doc.Raw) == 0 {
		return p, nil
	}
	if err := (protojson.UnmarshalOptions{}).Unmarshal(doc.Raw, p); err != nil {
		return nil, fmt.Errorf("%w: %v", ErrPolicyRejected, err)
	}
	return p, nil
}

// BuildPolicy merges profile then agent policy (rules by name, agent wins; filesystem
// lists concatenated; scalars agent over profile), then applies the reserved rules.
func BuildPolicy(profile, agent *apiextensionsv1.JSON, facadeHost string, facadePort uint32) (*ostypes.SandboxPolicy, error) {
	base, err := decode(profile)
	if err != nil {
		return nil, err
	}
	over, err := decode(agent)
	if err != nil {
		return nil, err
	}
	merged := openshellconv.SandboxPolicyFromProto(base)
	top := openshellconv.SandboxPolicyFromProto(over)
	if merged.NetworkPolicies == nil {
		merged.NetworkPolicies = map[string]ostypes.NetworkPolicyRule{}
	}
	for name, rule := range top.NetworkPolicies {
		merged.NetworkPolicies[name] = rule
	}
	for name := range merged.NetworkPolicies {
		if strings.HasPrefix(name, reservedPrefix) {
			return nil, fmt.Errorf("%w: rule name %q uses the reserved prefix %s", ErrPolicyRejected, name, reservedPrefix)
		}
	}
	if merged.Filesystem == nil {
		merged.Filesystem = &ostypes.FilesystemPolicy{}
	}
	if top.Filesystem != nil {
		merged.Filesystem.ReadOnly = append(merged.Filesystem.ReadOnly, top.Filesystem.ReadOnly...)
		merged.Filesystem.ReadWrite = append(merged.Filesystem.ReadWrite, top.Filesystem.ReadWrite...)
		merged.Filesystem.IncludeWorkdir = merged.Filesystem.IncludeWorkdir || top.Filesystem.IncludeWorkdir
	}
	if top.Landlock != nil {
		merged.Landlock = top.Landlock
	}
	if top.Process != nil {
		merged.Process = top.Process
	}
	if len(top.NetworkMiddlewares) > 0 {
		if merged.NetworkMiddlewares == nil {
			merged.NetworkMiddlewares = map[string]ostypes.NetworkMiddlewareConfig{}
		}
		for name, m := range top.NetworkMiddlewares {
			merged.NetworkMiddlewares[name] = m
		}
	}
	for _, rw := range merged.Filesystem.ReadWrite {
		if err := checkWritable(rw); err != nil {
			return nil, err
		}
	}
	// Reserved rules last.
	merged.Version = 1
	merged.Filesystem.IncludeWorkdir = true
	merged.Filesystem.ReadOnly = append(merged.Filesystem.ReadOnly, runtimeReadOnly...)
	merged.Filesystem.ReadWrite = append(merged.Filesystem.ReadWrite, runtimeReadWrite...)
	if merged.Landlock == nil || merged.Landlock.Compatibility == "" {
		merged.Landlock = &ostypes.LandlockPolicy{Compatibility: "hard_requirement"}
	}
	bins := make([]ostypes.PolicyNetworkBinary, 0, len(runtimeBinaries))
	for _, b := range runtimeBinaries {
		bins = append(bins, ostypes.PolicyNetworkBinary{Path: b})
	}
	merged.NetworkPolicies[reservedPrefix+"facades"] = ostypes.NetworkPolicyRule{
		Name:      reservedPrefix + "facades",
		Endpoints: []ostypes.PolicyNetworkEndpoint{{Host: facadeHost, Port: facadePort}},
		Binaries:  bins,
	}
	return merged, nil
}

// checkWritable rejects a read-write path equal to, inside, or above a protected directory.
func checkWritable(p string) error {
	clean := path.Clean(p)
	for _, dir := range protectedDirs {
		if clean == dir || strings.HasPrefix(clean, dir+"/") || strings.HasPrefix(dir, clean+"/") || clean == "/" {
			return fmt.Errorf("%w: read_write %q would make runtime binaries under %s writable", ErrPolicyRejected, p, dir)
		}
	}
	return nil
}

// Resources maps the profile's sandbox resources onto the template's resource map.
func Resources(r *v1.SandboxResources) (map[string]any, error) {
	if r == nil {
		return nil, nil
	}
	out := map[string]any{}
	if r.CPU != "" {
		out["cpu"] = r.CPU
	}
	if r.Memory != "" {
		out["memory"] = r.Memory
	}
	return out, nil
}
```

Use the SDK's real names (`PolicyNetworkBinary`, field types — the code map lists `Port uint32`); fix any mismatch the compiler reports. `ErrResourcesUnsupported` is returned when the SDK/gateway rejects the resource map (Step 5 maps the gateway error); `SandboxResources` only exposes fields the SDK supports, so nothing else can be set from the CRD. In `openshell.go` Create: `policy, err := BuildPolicy(profile.Spec.Sandbox.OpenShell.Policy, agentPolicy(a), facade.Hostname(), port)`; `Template.Resources, _ = Resources(...)`; replace the old hardcoded block. A gateway `InvalidArgument` on Create wraps `ErrPolicyRejected` with the gateway message (`osv1.IsInvalidArgument` or the SDK's status helper).

- [ ] **Step 5: Surface policy errors on the Agent**

The host loop runs outside the reconciler. Add `host.StatusWriter` (in `internal/host`): `SetCondition(ctx, agent types.NamespacedName, cond metav1.Condition) error` implemented with `client.Status().Patch` (merge patch on `status.conditions` via `meta.SetStatusCondition` on a fresh GET). When Create fails with `ErrPolicyRejected` -> condition `Ready=False`, reason `SandboxPolicyRejected`, message = error; `ErrResourcesUnsupported` -> reason `SandboxResourcesUnsupported`. The reconciler must not overwrite these: in `finish`, keep an existing `Ready=False` with one of these two reasons while `status.observedGeneration == generation` (cleared on the next spec change). Test with a fake gateway that returns InvalidArgument (extend `fakeGateway` from v0.14.2 `openshell_test.go`).

- [ ] **Step 6: Run tests**

Run: `cd operator && make test`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add -A operator
/usr/bin/git commit -q -m "feat(operator): merged sandbox policy with reserved rules, resources" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 5: Gateway availability, client rebuild and safe finalizer

**Files:**
- Modify: `operator/internal/host/client.go` (add `Gateway`), `host.go` (classify gateway errors, report availability), `runner.go` (per-agent stop + join, condition writes), `operator/internal/openshell/openshell.go` (rebuildable client), `operator/internal/controller/agent_controller.go` (finalizer)
- Test: `operator/internal/host/client_test.go`, `host_test.go`, `runner_test.go`, `operator/internal/openshell/openshell_test.go`, `operator/internal/controller/agent_finalizer_test.go`

**Interfaces:**
- Produces:
  - `func (c *Client) Gateway(ctx context.Context, available bool) error` — `PUT {BaseURL}/gateway`, body `{"available": bool}`, expects 204.
  - `openshell.IsUnavailable(err error) bool` — true for gRPC `Unavailable`, `DeadlineExceeded` from the gateway, and transport errors.
  - `type Rebuilding struct` implementing the `sandboxes` interface: holds the current SDK client under a mutex; on an `IsUnavailable` error it closes and recreates the client (at most once per 5s) before returning the error.
  - `Runner.Stop(ctx, uid types.UID) error` — cancels that agent's host loop and waits for it (and any in-flight Create) to return.

- [ ] **Step 1: Write the failing tests**

`client_test.go`:

```go
func TestClient_GatewayPutsAvailability(t *testing.T) {
	var got map[string]any
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPut || r.URL.Path != "/host/v1/gateway" {
			t.Errorf("%s %s", r.Method, r.URL.Path)
		}
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	c := &Client{BaseURL: srv.URL + "/host/v1", Token: "t"}
	if err := c.Gateway(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if got["available"] != false {
		t.Fatalf("body = %v", got)
	}
}
```

`host_test.go` (style of `TestPass_ReleasedRequestsDeletionAndConfirmsOnlyOnceTheStopIsConfirmed`, with the v0.14.2 `fakeRuntime` recording calls and a `fakeGateway` whose every call returns `status.Error(codes.Unavailable, "down")`):

```go
func TestPass_GatewayDownReportsUnavailableAndNeverConfirmsAStop(t *testing.T) {
	rt := newFakeRuntime(t)
	rt.listing = listingWith(ws("w1", "released", ptr("inst-1")))
	h := newHost(t, rt)
	h.backend.(*OpenShellBackend).Sandboxes = &fakeGateway{err: status.Error(codes.Unavailable, "down")}
	state := &State{}
	_ = h.Sync(context.Background(), state)
	if rt.count("DELETE /host/v1/workspaces/w1/instance") != 0 {
		t.Fatal("a stop was confirmed while the gateway was unavailable")
	}
	if rt.lastGateway() != false {
		t.Fatal("gateway unavailability was not reported")
	}
	if !h.GatewayDown() {
		t.Fatal("host does not track the outage")
	}
}

func TestPass_GatewayBackReportsAvailableOnce(t *testing.T) { /* down then up: exactly one PUT available=true */ }

func TestSync_ReportsGatewayAfterInventory(t *testing.T) { /* adopting listing -> PUT /inventory then PUT /gateway */ }
```

Fill the elided bodies in the same style (the implementer writes them; each asserts the exact recorded call sequence). `fakeRuntime` gets a `/host/v1/gateway` handler recording bodies.

`openshell_test.go`:

```go
func TestRebuilding_RecreatesClientAfterUnavailable(t *testing.T) {
	builds := 0
	r := NewRebuilding(func() (sandboxes, error) {
		builds++
		if builds == 1 {
			return &fakeGateway{err: status.Error(codes.Unavailable, "connection reset by peer")}, nil
		}
		return newFakeGateway(), nil
	}, 0) // 0 = no rebuild throttle in tests
	_, err := r.Get(context.Background(), "default", "x")
	if !IsUnavailable(err) {
		t.Fatalf("err = %v", err)
	}
	if _, err := r.ListAll(context.Background(), "default"); err != nil {
		t.Fatalf("after rebuild: %v", err)
	}
	if builds != 2 {
		t.Fatalf("builds = %d", builds)
	}
}
```

`agent_finalizer_test.go` (invert v0.14.2 `TestFinalize_NoGatewayRemovesTheFinalizer`):

```go
func TestFinalize_GatewayUnavailableKeepsTheFinalizer(t *testing.T)   // remover returns IsUnavailable error -> finalizer kept, requeue
func TestFinalize_StopsHostLoopBeforeInventory(t *testing.T)           // fake stopper records Stop(uid) before RemoveAgentSandboxes is called
func TestFinalize_RemovesFinalizerOnlyWhenNothingRemains(t *testing.T) // remaining=true -> kept; remaining=false and no per-sandbox Service -> removed
func TestFinalize_PerSandboxServicesAreDeleted(t *testing.T)           // ExternalName Services labelled with the agent UID are deleted before the finalizer goes
```

Write each body with the v0.14.2 helpers (`deletingAgent(t, objs...)`, `fakeRemover`) plus a `fakeStopper{calls []types.UID}`.

- [ ] **Step 2: Run to verify failure**

Run: `cd operator && make test`
Expected: new tests FAIL.

- [ ] **Step 3: Implement**

- `client.go`: `Gateway` as specified (reuse `do()`; 204 -> nil; other -> error with status).
- `openshell.go`: `IsUnavailable(err)` uses `status.Code(err)` in {`codes.Unavailable`, `codes.DeadlineExceeded`} or `errors.Is(err, context.DeadlineExceeded)` from `call()` or `*net.OpError`. `Rebuilding` wraps `sandboxes`; `OpenShellFromEnv` builds it with a factory that calls `osv1.NewClient(osv1.Config{Address: addr})` (no TLS: mesh only) and returns `c.Sandboxes()`; on rebuild, close the old client if it has `Close()`.
- `host.go`: in `Pass`/`ensureRunning`/`retire`, when any backend call fails with `IsUnavailable`: stop the pass without `API.Delete` or `Remove`, mark `gatewayDown=true`, call `API.Gateway(ctx, false)` once per transition, and have the Runner set `GatewayAvailable=False/GatewayUnavailable` on the Agent via `StatusWriter`. On the first successful backend call after an outage: `API.Gateway(ctx, true)`, `GatewayAvailable=True`. After a successful `API.Inventory`, always send `API.Gateway(ctx, !gatewayDown)`.
- `runner.go`: keep `cancels map[types.UID]context.CancelFunc` plus `done map[types.UID]chan struct{}` closed when the loop goroutine returns; `Stop(ctx, uid)` cancels and waits on `done` (bounded by ctx). The Create path in `openshell.go` must check `ctx.Err()` right before calling the gateway so a cancelled loop does not start a new create.
- Controller finalizer (`finalizeSandboxes`): (1) `r.Host.Stop(ctx, uid)` (interface `hostStopper` injected; nil in envtest -> skip); (2) `remaining, err := r.Sandboxes.RemoveAgentSandboxes(ctx, uid)`; `IsUnavailable(err)` or any error -> keep finalizer, `GatewayAvailable=False`, requeue after 15s; (3) list ExternalName Services with label `runtime.ach-agent.ackstorm.ai/agent-uid=<uid>` and delete them; (4) remove the finalizer only when `!remaining` and the Service list is empty. If `r.Sandboxes == nil` (cannot happen in production: start fails without a gateway) keep the finalizer and log.
- `main.go`: fail start if `ACH_AGENT_OPENSHELL_GATEWAY` is empty; inject the Runner as `hostStopper` into the reconciler.

- [ ] **Step 4: Run tests**

Run: `cd operator && make test && timeout 1200 make test-envtest > /tmp/op-envtest.log 2>&1; echo "EXIT=$?"; grep -E "^(ok|FAIL|--- FAIL)" /tmp/op-envtest.log`
Expected: PASS, `EXIT=0`.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A operator
/usr/bin/git commit -q -m "feat(operator): gateway availability, client rebuild and a finalizer that waits" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 6: Operator image and CI

**Files:**
- Create: `operator/Dockerfile`, `operator/.dockerignore`, `.github/workflows/operator.yml`, `operator/CLAUDE.md`

- [ ] **Step 1: Dockerfile**

```dockerfile
# syntax=docker/dockerfile:1
FROM golang:1.26-alpine AS builder
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY api/ api/
COPY cmd/ cmd/
COPY internal/ internal/
COPY third_party/ third_party/
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/operator ./cmd/operator

FROM gcr.io/distroless/static:nonroot
COPY --from=builder /out/operator /operator
USER 65532:65532
ENTRYPOINT ["/operator"]
```

`.dockerignore`: `bin/`, `*.out`, `.git`.

- [ ] **Step 2: Build it**

Run: `cd operator && timeout 900 docker build -t ach-agent-operator:dev . > /tmp/op-docker.log 2>&1; echo "EXIT=$?"`
Expected: `EXIT=0`.

- [ ] **Step 3: CI workflow**

`.github/workflows/operator.yml`: on push/PR with `paths: ["operator/**", ".github/workflows/operator.yml"]`; job `test` on `ubuntu-latest`: `actions/setup-go` with `go-version-file: operator/go.mod`, then in `operator/`: `make verify` and `make test-envtest`; job `image`: `docker build operator`. Pin action versions to full SHAs or exact tags as the runtime workflow does.

`operator/CLAUDE.md`: package layout, the make targets, the invariants (operator is the only K8s/gateway client; finalizer rule; reserved policy rules; no shortNames), and "source of the port: ach v0.14.2".

- [ ] **Step 4: Commit**

```bash
/usr/bin/git add operator/Dockerfile operator/.dockerignore operator/CLAUDE.md .github/workflows/operator.yml
/usr/bin/git commit -q -m "build(operator): image and CI" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Final gate (alongside the final review)

```bash
cd /home/coder/workspace/local/ach-agent/operator
timeout 1800 make verify > /tmp/op-verify.log 2>&1; echo "EXIT=$?"
timeout 1800 make test-envtest > /tmp/op-envtest.log 2>&1; echo "EXIT=$?"
grep -rn "ach.ackstorm.ai\|ACHAgent\|workspace-v2\|achagent-" --include=*.go --include=*.yaml . | grep -v third_party || echo "clean"
```

Expected: both `EXIT=0` and `clean`.
