# Environment credentials by token exchange: ach-agent side (plan B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An ACH-mode Agent can take its ACH credential from `spec.ach.keyFrom.tokenExchange`: the control pod exchanges its own projected ServiceAccount token for a short-lived ACH JWT and rotates it with zero failed requests; then the four production agents move to it in GitOps.

**Architecture:** The operator gives a `tokenExchange` Agent its own ServiceAccount `agent-<name>` and a projected token (audience = `spec.ach.baseUrl` without trailing slash) and renders `capability.ach.key = {"exchange": {...}}`. The runtime holds the credential in one `KeySource` (static `ek_` or exchanged JWT with a generation number) that every ACH consumer reads per request, either through `current()` or through an `httpx.Auth` (`KeyAuth`) installed on its client; `KeyAuth` also re-exchanges once (single-flight) and resends when ACH's own Authn rejects the credential. A process-wide log-record factory scrubs `ek-`/`ek_`, compact JWS and provider keys.

**Tech Stack:** Go 1.26 / controller-runtime / controller-gen v0.20.0 / envtest 1.36; Python 3.13, httpx 0.28, pydantic 2, prometheus_client, mcp 1.30, a2a-sdk 1.3; Helm, promtool (prom/prometheus:v3.5.0), kind e2e (`make e2e`).

**Spec:** `docs/superpowers/specs/2026-10-30-ek-token-exchange-design.md` (sections 1-3, 5, 6, 7.2, 7.3). Section 4 (ACH) is plan A; this plan consumes only its contract.

## Global Constraints

- Repository `/home/coder/workspace/local/ach-agent`. Work on branch `feat/ek-token-exchange` from `main` (`/usr/bin/git switch -c feat/ek-token-exchange`). Use `/usr/bin/git` for every git command. Never push.
- Every shell command runs in the foreground wrapped in `timeout N`. Never background a command, never poll without a bound.
- Every commit message ends with the session's attribution trailer lines (Co-Authored-By / Claude-Session) from the system reminder. Conventional commit subjects, imperative, < 72 chars.
- Comments and docs state the present: no history, no task/plan IDs, no "now/new".
- New Python files under `runtime/src` start with `# SPDX-License-Identifier: MIT`; new Go files with `// SPDX-License-Identifier: MIT`.
- The runtime never calls the Kubernetes API (repo invariant): it only reads the projected token file; TokenReview is ACH's job.
- ACH contract (from spec section 4, verbatim values):
  - `POST {baseUrl}/platform/keys/exchange`, `Authorization: Bearer <ServiceAccount token>`, body `{"identity": "<email>", "environment": "<name>"}`.
  - `200`: `{"token": "<jwt>", "expires_in": 900, "key_id": "ekid_…"}`. Schedule from `expires_in`, never from `exp`.
  - Errors: `401 invalid_token`, `403 not_granted`, `403 unauthorized_team`, `404`/`503 not_ready`, `429`. Envelope `{"error": {"code": "...", "message": "..."}, "request_id": "..."}`.
  - The JWT is sent in `x-ach-key`, like an `ek_`.
  - Audience rule: projected token audience = `spec.ach.baseUrl` with trailing slashes removed, which must equal ACH's `ACH_BASE_URL` normalised the same way.
- ACH's rejection of a credential before proxying is HTTP `401` with `{"error": {"code": "expired_or_revoked"}}` (ach `internal/platformapi/middleware/middleware.go:474`, `Authn`, `info == nil`). LiteLLM's own 401 carries `"code": "401"`. Only `expired_or_revoked` triggers a re-exchange and one retry.
- Projected token: path `/var/run/secrets/ach/token`, `expirationSeconds: 3600`.
- Refresh at 80% of `expires_in` on `time.monotonic`; backoff 1 s doubling to 60 s, clamped so a retry happens before expiry; not-ready after expiry until an exchange succeeds.
- ServiceAccount of a `tokenExchange` control pod: `render.ControlName(agent.Name)`, which is `agent-<agent name>` for names of up to 46 characters without dots (longer or dotted names get the same hashed form as the StatefulSet).
- Metrics: `ach_agent_key_refresh_failures_total` (counter), `ach_agent_key_expiry_timestamp_seconds` (gauge, Unix time), `ach_agent_key_lifetime_seconds` (gauge, `expires_in`). Alert `ACHAgentKeyRefreshFailing`.
- `keyFrom.secretKeyRef` agents behave exactly as before: same env `ACH_SECRET_IDENTITY`, same rendered `{"env": "ACH_SECRET_IDENTITY"}` (byte-identical config, so their pods do not roll).
- Release: version `0.2.0` (breaking at 0.x), `make release-bump VERSION=0.2.0`, commit `chore(release): v0.2.0`.
- Runtime unit test command (from `runtime/`), referred to below as **PYTEST**:
  `timeout 900 env PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/python -m pytest -q -p no:cacheprovider --import-mode=importlib <paths>`
- Operator test commands (from `operator/`): `timeout 900 make test` (vet + unit, no cluster); `timeout 1800 make test-envtest` (runs `make gen` first, then controller envtest); single envtest run:
  `timeout 1200 env GOTOOLCHAIN=go1.26.10 KUBEBUILDER_ASSETS="$(GOTOOLCHAIN=go1.26.10 go run sigs.k8s.io/controller-runtime/tools/setup-envtest@release-0.24 use 1.36.x --bin-dir bin/envtest -p path)" go test -race -count=1 ./internal/controller/ -run '<regex>'`

## Spec facts corrected while grounding this plan

1. **S3 access is tied to the profile ServiceAccount** (blocks the rollout, not the code). GitOps `workloads/agents/agentprofiles/default.yaml:7` sets `control.serviceAccountName: ach-agent-control`, and that ServiceAccount carries the ach-agent S3 backend through EKS Pod Identity (`workloads/agents/serviceaccount.yaml:1-3`; Terraform `nglz-genai/terraform-genai-blueprint-module/terraform.tfvars:36-38`, `modules/genai/ach-agent.tf:103-123`). Spec 5.2 moves `tokenExchange` pods to `agent-<name>`, so `k8s-doctor` (workspace persistence) and `observability-assistant` (session persistence) would lose S3. Task 10 adds Pod Identity associations for the four `agent-<name>` ServiceAccounts before the GitOps commit.
2. **Environment names** (spec 4.2/5.1 examples use `k8s-doctor`): the real Environments are `agent-k8s-doctor`, `agent-observability-assistant`, `agent-zohodesk-classifier`, `agent-gitlab` (for Agent `gitlab-reviewer`) (`gitops/workloads/ach/environments/*.yaml`). None of the four Agents sets `spec.ach.environment` today; `tokenExchange` requires it.
3. **Chart version pin**: `gitops/apps/ach-agent/base/helm.yaml:13` pins `version: 0.1.x`; 0.2.0 is never picked up without changing it. Spec 7.3 omits it.
4. **Relative alert threshold needs the lifetime**: with only the two metrics of spec 5.4, PromQL cannot express a threshold relative to the JWT lifetime. This plan adds `ach_agent_key_lifetime_seconds`.
5. **Redaction exists in two places, not one**: besides `control.py:83-95` (`_failure_reason`), `observability/runs.py:17-44` (`redact`, `register_secrets`) is used by the run formatter, `harness/workspaces.py:423,657` and `harness/handoff.py:32`. Neither covers JWS. Task 5 extends `runs.redact` and applies it to every record; `_failure_reason` stays as the length-capping reason formatter whose output is then also scrubbed.
6. **ACH-side assumption to confirm with plan A**: the exchange resolver must answer an invalid or expired exchange JWT with `(nil, nil)` so Authn returns `401 expired_or_revoked` (as `oauthresolver.go:59` does). If it returned an error, Authn would answer 500 and the agent would not re-exchange.
7. `zohodesk-classifier`'s Environment has no `spec.budget` (`gitops/workloads/ach/environments/agent-zohodesk-classifier.yaml`); its grant budget below is a proposal.

## File Structure

| File | Responsibility |
|---|---|
| `runtime/src/ach_runtime/config/schema.py` (modify) | `ExchangeKeyParams`, `ExchangeKey`; `AchServer.key: SecretAlias \| ExchangeKey` + rules |
| `runtime/docs/schemas/ach-workspace-config-v3.schema.json` (regenerated) + `operator/internal/render/testdata/` copy | wire schema shared by runtime and operator |
| `runtime/docs/schemas/ach-runtime-contract-v3.md` (modify) | contract text for the exchange key |
| `operator/api/v1alpha1/agent_types.go` (modify) | `AchKeyFrom`, `AchTokenExchange`, CEL |
| `operator/internal/render/wsconfig.go`, `render.go` (modify) | rendered key, SA name, audience, Secret discovery |
| `operator/internal/controller/agent_workload.go`, `agent_controller.go` (modify) | per-Agent SA, projected token volume |
| `runtime/src/ach_runtime/harness/keys.py` (create) | `KeySource`, `KeyAuth`, exchange client, rotation |
| `runtime/src/ach_runtime/observability/metrics.py` (modify) | three key metrics |
| `runtime/src/ach_runtime/facades/{_proxy,model,mcp,memory,_memory_client,_mcp_session,a2a}.py` (modify) | take an `httpx.Auth` |
| `runtime/src/ach_runtime/harness/{hydrate,control,app}.py` (modify) | wire `KeySource`; readiness |
| `runtime/src/ach_runtime/observability/runs.py` (modify) | JWS in `redact`; process-wide record factory |
| `chart/alerts/ach-agent.rules.yaml`, `chart/tests/alerts.test.yaml` (modify) | alert + promtool cases |
| `chart/templates/agent-pod-policy.yaml` (create), `chart/values.yaml`, `chart/values.schema.json`, `chart/tests/render_test.sh` (modify) | optional ValidatingAdmissionPolicy |
| `docs/install.md`, `operator/CLAUDE.md`, `CLAUDE.md` (modify) | operator docs |
| `test/e2e/stubs/{stub.py,test_stub.py,load.py,Dockerfile}`, `test/e2e/{stub.yaml,cluster.sh,control-ca.Dockerfile,e2e_test.go,helpers_test.go}`, `test/e2e/fixtures/{ach-agent.yaml,ach-exchange-agent.yaml}` | exchange stub with real TokenReview, acceptance test |
| `CHANGELOG.md`, `chart/Chart.yaml`, `runtime/pyproject.toml` | release 0.2.0 |
| GitOps repo (Task 10, described only) | KeyGrants, Agents, images, cleanup |

Task order: 1 → 2 (operator needs the schema) and 1 → 3 → 4 (runtime); 5, 6, 7 are independent of each other after 4; 8 needs 2 and 4-7; 9 needs all; 10 needs the published 0.2.0 and ACH 1.1.0.

---

### Task 1: Runtime config accepts `capability.ach.key.exchange`

**Files:**
- Modify: `runtime/src/ach_runtime/config/schema.py:43-58` (after `SecretAlias`), `:227-233` (`AchServer`)
- Regenerate: `runtime/docs/schemas/ach-workspace-config-v3.schema.json`
- Copy to: `operator/internal/render/testdata/ach-workspace-config-v3.schema.json`
- Modify: `runtime/docs/schemas/ach-runtime-contract-v3.md:82` and `:127`
- Test: `runtime/tests/config/test_capability.py`

**Interfaces:**
- Produces: `ExchangeKeyParams(identity: str, environment: str, token_path: str [alias tokenPath])`, `ExchangeKey(exchange: ExchangeKeyParams)`, `AchServer.key: SecretAlias | ExchangeKey`. Wire: `{"exchange": {"identity": "...", "environment": "...", "tokenPath": "/..."}}`.

- [ ] **Step 1: Write the failing tests** (append to `runtime/tests/config/test_capability.py`)

```python
def _exchange_wire() -> dict:
    wire = _wire("pr-review-runtime.json")
    ach = wire["capability"]["ach"]
    ach["key"] = {"exchange": {"identity": "ai.run@example.com", "environment": ach["environment"],
                               "tokenPath": "/var/run/secrets/ach/token"}}
    return wire


def test_ach_key_accepts_an_exchange_block():
    cfg = _parse(_exchange_wire())
    exchange = cfg.capability.ach.key.exchange
    assert (exchange.identity, exchange.environment, exchange.token_path) == (
        "ai.run@example.com", "engineering-prod", "/var/run/secrets/ach/token")


@pytest.mark.parametrize("mutate,match", [
    (lambda ach: ach.update(baseUrl="http://ach.ackstorm.ai"), "https"),
    (lambda ach: ach["key"]["exchange"].update(environment="other"), "environment"),
    (lambda ach: ach["key"]["exchange"].update(tokenPath="relative/token"), "pattern"),
    (lambda ach: ach["key"].update(env="ACH_SECRET_IDENTITY"), "Extra inputs"),
])
def test_exchange_key_rules(mutate, match):
    wire = _exchange_wire()
    mutate(wire["capability"]["ach"])
    with pytest.raises(ConfigError, match=match):
        _parse(wire)
```

- [ ] **Step 2: Run them to verify they fail**

Run (from `runtime/`): PYTEST `tests/config/test_capability.py -k exchange`
Expected: FAIL (`Input should be a valid dictionary or instance of SecretAlias` / extra inputs for `exchange`).

- [ ] **Step 3: Implement**

In `schema.py`, after the `SecretAlias` class (ends at line 58), add:

```python
class ExchangeKeyParams(BaseModel):
    """`capability.ach.key.exchange`: the control pod exchanges its projected ServiceAccount
    token, read from `tokenPath`, at `{baseUrl}/platform/keys/exchange` for a short-lived ACH
    credential granted to `identity` in `environment`."""

    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    identity: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    token_path: str = Field(alias="tokenPath", pattern=r"^/")


class ExchangeKey(BaseModel):
    """Wire shape `{"exchange": {...}}` of `capability.ach.key`."""

    model_config = ConfigDict(extra="forbid")

    exchange: ExchangeKeyParams
```

Replace `AchServer` (lines 227-233) with:

```python
class AchServer(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    base_url: str = Field(alias="baseUrl")
    environment: str
    key: SecretAlias | ExchangeKey
    model: ModelBlock

    @model_validator(mode="after")
    def _exchange_rules(self) -> AchServer:
        if isinstance(self.key, ExchangeKey):
            if not self.base_url.startswith("https://"):
                raise ValueError("capability.ach.key.exchange needs an https:// baseUrl")
            if self.key.exchange.environment != self.environment:
                raise ValueError("capability.ach.key.exchange.environment must equal capability.ach.environment")
        return self
```

Also update the `SecretAlias` docstring line 46 to: ``Wire shape for `capability.ach.key` (static key) and `handoff.secretEnv.<name>`: `{"env": "ACH_SECRET_..."}`.``

- [ ] **Step 4: Regenerate the JSON schema and vendor it**

```bash
cd /home/coder/workspace/local/ach-agent/runtime
timeout 120 .venv/bin/python scripts/gen_schema.py
timeout 120 .venv/bin/python scripts/gen_schema.py --check
cp docs/schemas/ach-workspace-config-v3.schema.json ../operator/internal/render/testdata/ach-workspace-config-v3.schema.json
```
Expected: `--check` exits 0; `AchServer.properties.key` is an `anyOf` of `SecretAlias` and `ExchangeKey`.

- [ ] **Step 5: Update the contract text** in `runtime/docs/schemas/ach-runtime-contract-v3.md`

Line 82, replace the row with:
```markdown
| The variable named by `capability.ach.key.env` | `agent`, `control` | when `capability.ach.key` is `{"env": …}` | The agent's static ACH `ek_` credential. With `{"exchange": {identity, environment, tokenPath}}` there is no variable: the control role reads the projected ServiceAccount token at `tokenPath` and exchanges it at `POST {baseUrl}/platform/keys/exchange` for a short-lived credential it renews at 80% of `expires_in`. |
```
Line 127, replace the rule with:
```markdown
- `capability.ach.key` is `{"env": NAME}` (a static credential, opaque to the runtime; a stand-in may accept any value) or `{"exchange": {...}}` (a stand-in answers `POST {baseUrl}/platform/keys/exchange` with `{"token", "expires_in", "key_id"}` and accepts that token in `x-ach-key` until it expires).
```

- [ ] **Step 6: Run the tests**

Run: PYTEST `tests/config`
Then (from `operator/`): `timeout 600 env GOTOOLCHAIN=go1.26.10 go test -count=1 ./internal/render/ -run 'TestWorkspaceSchema|TestWorkspaceFixtures'`
Expected: PASS (no schema drift).

- [ ] **Step 7: Commit**

```bash
/usr/bin/git add runtime/src/ach_runtime/config/schema.py runtime/docs/schemas runtime/tests/config/test_capability.py operator/internal/render/testdata/ach-workspace-config-v3.schema.json
/usr/bin/git commit -m "feat(config): accept an exchanged ACH key in capability.ach.key"
```

---

### Task 2: Operator `keyFrom` API, rendering and per-Agent ServiceAccount

**Files:**
- Modify: `operator/api/v1alpha1/agent_types.go:463-478`
- Regenerate: `operator/api/v1alpha1/zz_generated.deepcopy.go`, `operator/config/crd/*`, `chart/crds/*` (`make gen`, `make chart-sync`)
- Modify: `operator/internal/render/wsconfig.go:124-131` (`ControlServiceAccountName`), `:214-220` (`WSAchServer`), `:291-298` (ach key)
- Modify: `operator/internal/render/render.go:491-493` (`ReferencedSecrets`)
- Modify: `operator/internal/controller/agent_workload.go:23-49` (consts), `:57-61`, `:187-196`, `:267-277`
- Modify: `operator/internal/controller/agent_controller.go:248-260`, `:337-342`
- Modify tests: `operator/internal/render/render_v3_test.go:83-118,153-176`, `operator/internal/render/wsconfig_test.go:101-102`, `operator/internal/controller/agent_envtest_test.go:354-393`, `operator/internal/controller/agent_workload_test.go`
- Modify: `test/e2e/fixtures/ach-agent.yaml:10`, `operator/CLAUDE.md`

**Interfaces:**
- Consumes: Task 1 schema (vendored in `testdata`).
- Produces (Go, package `render`): `AchTokenDir = "/var/run/secrets/ach"`, `AchTokenFile = "token"`, `AchTokenPath = AchTokenDir + "/" + AchTokenFile`; `type WSAchKey struct{ Env string; Exchange *WSExchangeKey }`; `type WSExchangeKey struct{ Identity, Environment, TokenPath string }`; `func TokenExchange(a *v1.Agent) *v1.AchTokenExchange`; `func ExchangeAudience(baseURL string) string`; `func ControlServiceAccountName(a *v1.Agent, p *v1.AgentProfile) string` (signature change).
- Produces (API): `AchServerSpec.KeyFrom AchKeyFrom`; `AchKeyFrom{TokenExchange *AchTokenExchange; SecretKeyRef *SecretKeyRef}`; `AchTokenExchange{Identity string}`.

- [ ] **Step 1: Write the failing tests**

`operator/internal/render/render_v3_test.go`: replace the three `KeySecretRef: v1.SecretKeyRef{...}` literals (lines 88, 110, 160) with `KeyFrom: v1.AchKeyFrom{SecretKeyRef: &v1.SecretKeyRef{...}}` (same names/keys), and replace `TestControlServiceAccountName` (lines 167-176) with:

```go
func TestControlServiceAccountName(t *testing.T) {
	p, a := minimalFixture()
	if got, want := ControlServiceAccountName(&a, &p), "ach-agent-control-"+fixtureAgentUID; got != want {
		t.Fatalf("unset = %q, want %q", got, want)
	}
	p.Spec.Control.ServiceAccountName = "shared-control"
	if got := ControlServiceAccountName(&a, &p); got != "shared-control" {
		t.Fatalf("set = %q", got)
	}
	a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://ach.example.com", Environment: "prod",
		KeyFrom: v1.AchKeyFrom{TokenExchange: &v1.AchTokenExchange{Identity: "ai.run@example.com"}}, Model: v1.AchModelSpec{Name: "m"}}
	if got := ControlServiceAccountName(&a, &p); got != "agent-a" {
		t.Fatalf("tokenExchange = %q, want agent-a (the profile's ServiceAccount never applies)", got)
	}
}

func TestRender_AchTokenExchangeRendersTheExchangeKeyAndNoSecret(t *testing.T) {
	p, a := minimalFixture()
	a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
	a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://api.example/", Environment: "prod",
		KeyFrom: v1.AchKeyFrom{TokenExchange: &v1.AchTokenExchange{Identity: "ai.run@example.com"}}, Model: v1.AchModelSpec{Name: "m"}}
	cfg, env, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	want := `"key":{"exchange":{"identity":"ai.run@example.com","environment":"prod","tokenPath":"/var/run/secrets/ach/token"}}`
	if !strings.Contains(string(raw), want) {
		t.Errorf("config lacks %s: %s", want, raw)
	}
	if len(env) != 0 {
		t.Errorf("env bindings = %#v, want none", env)
	}
	if got := ReferencedSecrets(p, a); len(got) != 0 {
		t.Errorf("referenced Secrets = %v, want none", got)
	}
}

func TestExchangeAudience(t *testing.T) {
	for in, want := range map[string]string{"https://api.ackstorm.ai": "https://api.ackstorm.ai", "https://api.ackstorm.ai/": "https://api.ackstorm.ai", "https://h/p//": "https://h/p"} {
		if got := ExchangeAudience(in); got != want {
			t.Errorf("ExchangeAudience(%q) = %q, want %q", in, got, want)
		}
	}
}
```

`operator/internal/render/wsconfig_test.go:101-102`: change `KeySecretRef: v1.SecretKeyRef{Name: "test", Key: "ek"}` to `KeyFrom: v1.AchKeyFrom{SecretKeyRef: &v1.SecretKeyRef{Name: "test", Key: "ek"}}`, and add one more entry to the `fixtures` map of `TestRender_FullOutputConformsToSchemaAndJCSHash`:

```go
		"full ach token exchange": func() (v1.AgentProfile, v1.Agent) {
			p, a := full()
			a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
			a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://ach.example.com", Environment: "engineering-prod",
				KeyFrom: v1.AchKeyFrom{TokenExchange: &v1.AchTokenExchange{Identity: "ai.run@example.com"}},
				Model:   v1.AchModelSpec{Name: "gemini.gemini-flash-latest", API: "gemini"}}
			return p, a
		},
```

`operator/internal/controller/agent_workload_test.go`: append

```go
func tokenExchangeAgent() *achv1alpha1.Agent {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace, a.UID = "doctor", "ns", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
	a.Spec.CapabilityServer = "ach"
	a.Spec.Ach = &achv1alpha1.AchServerSpec{BaseURL: "https://api.example.com/", Environment: "prod",
		KeyFrom: achv1alpha1.AchKeyFrom{TokenExchange: &achv1alpha1.AchTokenExchange{Identity: "ai.run@example.com"}},
		Model:   achv1alpha1.AchModelSpec{Name: "m"}}
	return a
}

func TestBuildStatefulSet_TokenExchangeRunsAsTheAgentServiceAccountWithAProjectedToken(t *testing.T) {
	a := tokenExchangeAgent()
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.ServiceAccountName = "shared-control" // never applies to a tokenExchange agent
	ps := buildStatefulSet(a, p, "h", nil).Spec.Template.Spec
	if ps.ServiceAccountName != "agent-doctor" {
		t.Fatalf("serviceAccountName = %q, want agent-doctor", ps.ServiceAccountName)
	}
	if ps.AutomountServiceAccountToken == nil || *ps.AutomountServiceAccountToken {
		t.Fatal("the API token must stay unmounted; only the audience-bound projected token is mounted")
	}
	var tok *corev1.ServiceAccountTokenProjection
	for _, v := range ps.Volumes {
		if v.Name == "ach-token" && v.Projected != nil && len(v.Projected.Sources) == 1 {
			tok = v.Projected.Sources[0].ServiceAccountToken
		}
	}
	if tok == nil || tok.Audience != "https://api.example.com" || tok.ExpirationSeconds == nil || *tok.ExpirationSeconds != 3600 || tok.Path != "token" {
		t.Fatalf("projected token = %+v", tok)
	}
	mounted := false
	for _, m := range ps.Containers[0].VolumeMounts {
		mounted = mounted || (m.Name == "ach-token" && m.MountPath == "/var/run/secrets/ach" && m.ReadOnly)
	}
	if !mounted {
		t.Fatalf("mounts = %+v, want ach-token read-only at /var/run/secrets/ach", ps.Containers[0].VolumeMounts)
	}
	if sa := buildControlServiceAccount(a); sa.Name != "agent-doctor" {
		t.Fatalf("operator-built ServiceAccount = %q, want agent-doctor", sa.Name)
	}
}

func TestBuildStatefulSet_SecretKeyAgentKeepsItsServiceAccountAndNoToken(t *testing.T) {
	a := tokenExchangeAgent()
	a.Spec.Ach.KeyFrom = achv1alpha1.AchKeyFrom{SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "ek", Key: "ek"}}
	p := &achv1alpha1.AgentProfile{}
	ps := buildStatefulSet(a, p, "h", nil).Spec.Template.Spec
	if ps.ServiceAccountName != "ach-agent-control-"+string(a.UID) {
		t.Fatalf("serviceAccountName = %q", ps.ServiceAccountName)
	}
	for _, v := range ps.Volumes {
		if v.Name == "ach-token" {
			t.Fatal("a secretKeyRef agent must not mount a projected token")
		}
	}
	p.Spec.Control.ServiceAccountName = "shared-control"
	if got := buildStatefulSet(a, p, "h", nil).Spec.Template.Spec.ServiceAccountName; got != "shared-control" {
		t.Fatalf("profile ServiceAccount = %q, want shared-control", got)
	}
}
```

`operator/internal/controller/agent_envtest_test.go`: in `TestAgent_CEL_Rejections` (line 354), change the `"both servers"` literal to `KeyFrom: achv1alpha1.AchKeyFrom{SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "k", Key: "k"}}`; before the map add

```go
	exchange := func(base, env string) *achv1alpha1.AchServerSpec {
		return &achv1alpha1.AchServerSpec{BaseURL: base, Environment: env,
			KeyFrom: achv1alpha1.AchKeyFrom{TokenExchange: &achv1alpha1.AchTokenExchange{Identity: "ai.run@example.com"}},
			Model:   achv1alpha1.AchModelSpec{Name: "m"}}
	}
	achOnly := func(a *achv1alpha1.Agent, s *achv1alpha1.AchServerSpec) {
		a.Spec.CapabilityServer, a.Spec.Standalone, a.Spec.Ach = "ach", nil, s
	}
```
and add these map entries:

```go
		"keyFrom with both": func(a *achv1alpha1.Agent) {
			s := exchange("https://a", "e")
			s.KeyFrom.SecretKeyRef = &achv1alpha1.SecretKeyRef{Name: "k", Key: "k"}
			achOnly(a, s)
		},
		"keyFrom with neither": func(a *achv1alpha1.Agent) {
			s := exchange("https://a", "e")
			s.KeyFrom = achv1alpha1.AchKeyFrom{}
			achOnly(a, s)
		},
		"tokenExchange over http":           func(a *achv1alpha1.Agent) { achOnly(a, exchange("http://a", "e")) },
		"tokenExchange without environment": func(a *achv1alpha1.Agent) { achOnly(a, exchange("https://a", "")) },
		"tokenExchange identity pattern": func(a *achv1alpha1.Agent) {
			s := exchange("https://a", "e")
			s.KeyFrom.TokenExchange.Identity = "*@example.com"
			achOnly(a, s)
		},
```
and after the existing `"route with service is accepted"` subtest:

```go
	t.Run("tokenExchange over https with an environment is accepted", func(t *testing.T) {
		a := testAgentObject("aa-cel-exchange-ok", "aa-cel-p")
		achOnly(a, exchange("https://a", "e"))
		if err := k8sClient.Create(ctx, a); err != nil {
			t.Fatalf("tokenExchange must be accepted: %v", err)
		}
	})
```
Fix the other compile site at line 359 the same way. Append the reconcile test:

```go
func TestAgent_TokenExchange_OwnsItsServiceAccountAndIgnoresTheProfileOne(t *testing.T) {
	ctx := context.Background()
	const name = "aa-exchange"
	p := testProfile("aa-exchange-p")
	p.Spec.Control.ServiceAccountName = "never-created"
	mustApply(t, ctx, p)
	a := testAgentObject(name, "aa-exchange-p")
	a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
	a.Spec.Ach = &achv1alpha1.AchServerSpec{BaseURL: "https://ach.example.com", Environment: "prod",
		KeyFrom: achv1alpha1.AchKeyFrom{TokenExchange: &achv1alpha1.AchTokenExchange{Identity: "ai.run@example.com"}},
		Model:   achv1alpha1.AchModelSpec{Name: "m"}}
	mustApply(t, ctx, a)
	waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionTrue)
	agent := getAgent(t, ctx, name)
	var sa corev1.ServiceAccount
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(name)}, &sa); err != nil {
		t.Fatalf("get agent ServiceAccount: %v", err)
	}
	if owner := metav1.GetControllerOf(&sa); owner == nil || owner.UID != agent.UID {
		t.Errorf("ServiceAccount controller = %+v, want the Agent", owner)
	}
	if got := getControlStatefulSet(t, ctx, name).Spec.Template.Spec.ServiceAccountName; got != render.ControlName(name) {
		t.Errorf("pod ServiceAccount = %q, want %q", got, render.ControlName(name))
	}
}
```

- [ ] **Step 2: Run them to verify they fail**

Run (from `operator/`): `timeout 600 env GOTOOLCHAIN=go1.26.10 go vet ./...`
Expected: FAIL to compile (`unknown field KeyFrom`, `undefined: AchKeyFrom`).

- [ ] **Step 3: Implement the API** (`agent_types.go`, replace lines 463-478)

```go
// AchTokenExchange exchanges the control pod's projected ServiceAccount token for a
// short-lived ACH credential. ACH issues it only when a KeyGrant names this identity, the
// Environment and the ServiceAccount <namespace>/agent-<agent name>.
type AchTokenExchange struct {
	// Identity is the ACH identity (an email) the credential acts as.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[^@\s*?\[\]]+@[^@\s*?\[\]]+$`
	Identity string `json:"identity"`
}

// AchKeyFrom is where the ACH credential comes from: exactly one of tokenExchange or
// secretKeyRef.
// +kubebuilder:validation:XValidation:rule="has(self.tokenExchange) != has(self.secretKeyRef)",message="ach.keyFrom: set exactly one of tokenExchange or secretKeyRef"
type AchKeyFrom struct {
	// +optional
	TokenExchange *AchTokenExchange `json:"tokenExchange,omitempty"`
	// SecretKeyRef is a Secret holding a static ek_.
	// +optional
	SecretKeyRef *SecretKeyRef `json:"secretKeyRef,omitempty"`
}

// AchServerSpec is the capability source of an Agent hydrated from ACH.
// +kubebuilder:validation:XValidation:rule="!has(self.keyFrom.tokenExchange) || (has(self.environment) && size(self.environment) > 0)",message="ach.environment is required with keyFrom.tokenExchange"
// +kubebuilder:validation:XValidation:rule="!has(self.keyFrom.tokenExchange) || self.baseUrl.startsWith('https://')",message="ach.baseUrl must be https:// with keyFrom.tokenExchange"
type AchServerSpec struct {
	// BaseURL is ACH's public URL. With tokenExchange it is also the projected token's
	// audience (trailing slashes removed) and must equal ACH's ACH_BASE_URL.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https?://[^/\s]+`
	BaseURL string `json:"baseUrl"`
	// Environment is the ACH Hub Environment name. Required with tokenExchange.
	// +optional
	Environment string `json:"environment,omitempty"`
	// +kubebuilder:validation:Required
	KeyFrom AchKeyFrom `json:"keyFrom"`
	// +kubebuilder:validation:Required
	Model AchModelSpec `json:"model"`
}
```

- [ ] **Step 4: Implement rendering** (`operator/internal/render/wsconfig.go`)

Replace `ControlServiceAccountName` (lines 124-131) with:

```go
// AchTokenDir/AchTokenPath are where a tokenExchange control pod reads its projected
// ServiceAccount token.
const (
	AchTokenDir  = "/var/run/secrets/ach"
	AchTokenFile = "token"
	AchTokenPath = AchTokenDir + "/" + AchTokenFile
)

// TokenExchange is the Agent's spec.ach.keyFrom.tokenExchange, or nil.
func TokenExchange(a *v1.Agent) *v1.AchTokenExchange {
	if a.Spec.CapabilityServer != "ach" || a.Spec.Ach == nil {
		return nil
	}
	return a.Spec.Ach.KeyFrom.TokenExchange
}

// ExchangeAudience is the projected token's audience: spec.ach.baseUrl without trailing
// slashes, which ACH compares with its own ACH_BASE_URL normalised the same way.
func ExchangeAudience(baseURL string) string { return strings.TrimRight(baseURL, "/") }

// ControlServiceAccountName is the effective control ServiceAccount. A tokenExchange agent
// always runs as its own ControlName (agent-<agent name>), so no profile can point a pod at
// another agent's KeyGrant; otherwise the profile's control.serviceAccountName, else
// ach-agent-control-<uid>.
func ControlServiceAccountName(a *v1.Agent, p *v1.AgentProfile) string {
	if TokenExchange(a) != nil {
		return ControlName(a.Name)
	}
	if p.Spec.Control.ServiceAccountName != "" {
		return p.Spec.Control.ServiceAccountName
	}
	return "ach-agent-control-" + string(a.UID)
}
```

Replace `WSAchServer` (lines 214-220) with:

```go
// WSAchServer is capability.ach (schema AchServer).
type WSAchServer struct {
	BaseURL     string     `json:"baseUrl"`
	Environment string     `json:"environment"`
	Key         WSAchKey   `json:"key"`
	Model       ModelBlock `json:"model"`
}

// WSAchKey is capability.ach.key: {"env": NAME} for a Secret, {"exchange": {...}} for
// tokenExchange. omitempty keeps the Secret form byte-identical to {"env": ...}.
type WSAchKey struct {
	Env      string         `json:"env,omitempty"`
	Exchange *WSExchangeKey `json:"exchange,omitempty"`
}

// WSExchangeKey is capability.ach.key.exchange (schema ExchangeKeyParams).
type WSExchangeKey struct {
	Identity    string `json:"identity"`
	Environment string `json:"environment"`
	TokenPath   string `json:"tokenPath"`
}
```

Replace lines 291-298 (from `cap.Ach = &WSAchServer{` through `return cap, []EnvBinding{...}, nil`) with:

```go
		cap.Ach = &WSAchServer{
			BaseURL: ach.BaseURL, Environment: ach.Environment,
			Model: ModelBlock{Name: ach.Model.Name, Type: api, Params: params, Thinking: renderThinking(ach.Model.Thinking)},
		}
		switch kf := ach.KeyFrom; {
		case kf.TokenExchange != nil:
			cap.Ach.Key = WSAchKey{Exchange: &WSExchangeKey{Identity: kf.TokenExchange.Identity, Environment: ach.Environment, TokenPath: AchTokenPath}}
			return cap, nil, nil
		case kf.SecretKeyRef != nil:
			cap.Ach.Key = WSAchKey{Env: AchKeyEnv}
			key := *kf.SecretKeyRef
			return cap, []EnvBinding{{Name: AchKeyEnv, SecretKeyRef: &key}}, nil
		}
		return cap, nil, fmt.Errorf("spec.ach.keyFrom needs tokenExchange or secretKeyRef")
```

`render.go:491-493` becomes:

```go
	if ach := a.Spec.Ach; ach != nil && ach.KeyFrom.SecretKeyRef != nil {
		add(ach.KeyFrom.SecretKeyRef.Name, ach.KeyFrom.SecretKeyRef.Key)
	}
```

- [ ] **Step 5: Implement the workload** (`operator/internal/controller/agent_workload.go`)

In the `const` block (after `tmpMountDir`, line 33) add:

```go
	// achTokenVolumeName holds a tokenExchange control pod's projected ServiceAccount token
	// (render.AchTokenPath); the kubelet renews it.
	achTokenVolumeName        = "ach-token"
	achTokenExpirationSeconds = int64(3600)
```

Lines 57-61:

```go
// effectiveControlServiceAccountName is what the control pod runs as (render.ControlServiceAccountName).
func effectiveControlServiceAccountName(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) string {
	return render.ControlServiceAccountName(a, p)
}
```

Lines 187-196:

```go
// buildControlServiceAccount is the operator-owned control identity: agent-<agent name> for
// a tokenExchange agent, ach-agent-control-<uid> otherwise. It automounts no API token and
// has no Role: the operator owns the sandboxes. Not applied when a non-tokenExchange agent's
// profile sets control.serviceAccountName (that SA is external).
func buildControlServiceAccount(a *achv1alpha1.Agent) *corev1.ServiceAccount {
	falseVal := false
	return &corev1.ServiceAccount{
		ObjectMeta:                   metav1.ObjectMeta{Name: render.ControlServiceAccountName(a, &achv1alpha1.AgentProfile{}), Namespace: a.Namespace, Labels: agentLabels(a)},
		AutomountServiceAccountToken: &falseVal,
	}
}
```

In `buildStatefulSet`, after the persistence block (after line 277) add:

```go
	if render.TokenExchange(a) != nil {
		exp := achTokenExpirationSeconds
		volumes = append(volumes, corev1.Volume{Name: achTokenVolumeName, VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
				ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
					Audience: render.ExchangeAudience(a.Spec.Ach.BaseURL), ExpirationSeconds: &exp, Path: render.AchTokenFile,
				},
			}}},
		}})
		mounts = append(mounts, corev1.VolumeMount{Name: achTokenVolumeName, MountPath: render.AchTokenDir, ReadOnly: true})
	}
```

`agent_controller.go:248-250` (step 1b) — change the condition to:

```go
	// 1b. A profile-named control SA is external: it must already exist. Created later, the
	// ServiceAccount watch (agentsForControlServiceAccount) re-enqueues this agent. A
	// tokenExchange agent never uses it.
	if name := profile.Spec.Control.ServiceAccountName; name != "" && render.TokenExchange(&agent) == nil {
```

`agent_controller.go:337-342`:

```go
	// The operator owns the control SA unless a non-tokenExchange agent's profile names an external one.
	if profile.Spec.Control.ServiceAccountName == "" || render.TokenExchange(&agent) != nil {
		if err := r.apply(ctx, &agent, buildControlServiceAccount(&agent)); err != nil {
			return r.applyFail(ctx, &agent, conds, "ServiceAccount", err)
		}
	}
```

- [ ] **Step 6: Regenerate and fix the e2e fixture**

```bash
cd /home/coder/workspace/local/ach-agent/operator && timeout 900 make gen
cd /home/coder/workspace/local/ach-agent && timeout 300 make chart-sync
```
In `test/e2e/fixtures/ach-agent.yaml` line 10 replace `keySecretRef: {name: e2e-ek, key: token}` with `keyFrom: {secretKeyRef: {name: e2e-ek, key: token}}`.

In `operator/CLAUDE.md`, replace the invariant sentence `The ACH key is \`ACH_SECRET_IDENTITY\`.` with:
```markdown
  The ACH key from `keyFrom.secretKeyRef` is `ACH_SECRET_IDENTITY`. A `keyFrom.tokenExchange` Agent gets no key env: its
  pod runs as ServiceAccount `ControlName` (`agent-<name>`, owned by the Agent; the profile's `serviceAccountName`
  never applies) with a projected token (audience `baseUrl` without trailing slash, 3600 s) at `/var/run/secrets/ach/token`.
```

- [ ] **Step 7: Run the tests**

```bash
cd /home/coder/workspace/local/ach-agent/operator && timeout 900 make test
cd /home/coder/workspace/local/ach-agent/operator && timeout 1800 make test-envtest
cd /home/coder/workspace/local/ach-agent && timeout 600 make chart-test > /tmp/chart-test.log 2>&1; echo "EXIT=$?"; tail -5 /tmp/chart-test.log
```
Expected: all PASS; `chart-test` fails only on "chart/ drifted" if the regenerated `chart/crds` is not committed yet — commit first, then rerun.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add operator chart/crds chart/templates/clusterrole.yaml test/e2e/fixtures/ach-agent.yaml
/usr/bin/git commit -m "feat(operator)!: move the ACH key under keyFrom and add tokenExchange"
```

---

### Task 3: `KeySource`: exchange, rotation, single-flight re-exchange, metrics

**Files:**
- Create: `runtime/src/ach_runtime/harness/keys.py`
- Modify: `runtime/src/ach_runtime/observability/metrics.py:42-46` (add metrics) and `__all__`
- Test: `runtime/tests/harness/test_keys.py`

**Interfaces:**
- Produces:
  - `class ExchangeRefused(Exception)` with `.code: str`; message `"ACH token exchange refused: <code>"`.
  - `@dataclass(frozen=True) class Exchange(base_url: str, identity: str, environment: str, token_path: str)`.
  - `def error_code(response: httpx.Response) -> str`.
  - `class KeySource(value: str = "", *, exchange: Exchange | None = None, client: httpx.AsyncClient | None = None, clock=time.monotonic, wall=time.time, sleep=asyncio.sleep)` with `generation: int`, `auth: KeyAuth`, `current() -> str`, `ready() -> bool`, `async start() -> None`, `async close() -> None`, `async refresh_rejected(generation: int) -> bool`.
  - `class KeyAuth(httpx.Auth)`: sets `x-ach-key`; on `401 expired_or_revoked` re-exchanges once and resends.
  - `metrics.KEY_REFRESH_FAILURES`, `metrics.KEY_EXPIRY_TIMESTAMP`, `metrics.KEY_LIFETIME`.

- [ ] **Step 1: Write the failing tests** (`runtime/tests/harness/test_keys.py`)

```python
# SPDX-License-Identifier: MIT
"""KeySource: exchange, rotation at 80% on the monotonic clock, clamped backoff, single-flight
re-exchange, and KeyAuth's one retry on ACH's own rejection."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from prometheus_client import REGISTRY

from ach_runtime.harness.keys import Exchange, ExchangeRefused, KeySource

EX = Exchange("https://ach.example", "ai.run@example.com", "prod", "")
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_1"}


class FakeTime:
    """Monotonic and wall clocks that advance only when the rotation loop sleeps. The
    `stop_after`-th sleep is recorded and then ends the loop."""

    def __init__(self, stop_after: int, probe: Callable[[], object] | None = None) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.probes: list[object] = []
        self._stop_after, self._probe = stop_after, probe

    def clock(self) -> float:
        return self.now

    def wall(self) -> float:
        return self.now + 4000.0

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self._probe is not None:
            self.probes.append(self._probe())
        if len(self.sleeps) >= self._stop_after:
            raise asyncio.CancelledError
        self.now += seconds


def ok(token: str, expires_in: float = 100) -> httpx.Response:
    return httpx.Response(200, json={"token": token, "expires_in": expires_in, "key_id": "ekid_1"})


def refused(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, json={"error": {"code": code, "message": "x"}, "request_id": "req_1"})


def _exchanger(replies: list[httpx.Response], calls: list[httpx.Request]) -> httpx.AsyncClient:
    """Answers each exchange with the next reply; the last one repeats."""
    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        await asyncio.sleep(0)
        return replies.pop(0) if len(replies) > 1 else replies[0]
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _token(tmp_path: Path, value: str = "sa-1") -> str:
    path = tmp_path / "token"
    path.write_text(value + "\n")
    return str(path)


def _keys(tmp_path: Path, replies, calls, time: FakeTime | None = None) -> KeySource:
    kwargs = {} if time is None else {"clock": time.clock, "wall": time.wall, "sleep": time.sleep}
    return KeySource(exchange=replace(EX, token_path=_token(tmp_path)), client=_exchanger(replies, calls), **kwargs)


def _failures() -> float:
    return REGISTRY.get_sample_value("ach_agent_key_refresh_failures_total") or 0.0


async def test_each_exchange_reads_the_token_file_and_posts_identity_and_environment(tmp_path) -> None:
    calls: list[httpx.Request] = []
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls)
    assert await keys.refresh_rejected(0)
    (tmp_path / "token").write_text("sa-2")
    assert await keys.refresh_rejected(1)
    assert [c.headers["authorization"] for c in calls] == ["Bearer sa-1", "Bearer sa-2"]
    assert str(calls[0].url) == "https://ach.example/platform/keys/exchange"
    assert json.loads(calls[0].content) == {"identity": "ai.run@example.com", "environment": "prod"}
    assert (keys.current(), keys.generation) == ("jwt-2", 2)
    await keys.close()


@pytest.mark.parametrize("reply,code", [
    (refused(403, "not_granted"), "not_granted"),
    (refused(401, "invalid_token"), "invalid_token"),
    (httpx.Response(307, headers={"location": "https://elsewhere.example/x"}), "http_307"),
    (httpx.Response(200, json={"token": "", "expires_in": 900}), "invalid_response"),
])
async def test_start_fails_with_achs_code_and_follows_no_redirect(tmp_path, reply, code) -> None:
    calls: list[httpx.Request] = []
    keys = _keys(tmp_path, [reply], calls)
    with pytest.raises(ExchangeRefused, match=code) as raised:
        await keys.start()
    assert raised.value.code == code and len(calls) == 1
    await keys.close()


async def test_rotation_runs_at_80_percent_of_expires_in_on_the_monotonic_clock(tmp_path) -> None:
    time, calls = FakeTime(stop_after=3), []
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2"), ok("jwt-3")], calls, time)
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    assert time.sleeps == pytest.approx([80.0, 80.0, 80.0])
    assert (len(calls), keys.generation, keys.current()) == (3, 3, "jwt-3")
    assert REGISTRY.get_sample_value("ach_agent_key_expiry_timestamp_seconds") == pytest.approx(time.wall() + 100)
    assert REGISTRY.get_sample_value("ach_agent_key_lifetime_seconds") == 100
    await keys.close()


async def test_refresh_failures_back_off_but_always_retry_before_expiry(tmp_path) -> None:
    time, calls = FakeTime(stop_after=10), []
    keys = _keys(tmp_path, [ok("jwt-1"), refused(503, "not_ready")], calls, time)
    before = _failures()
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    # Refresh due at 1080, expiry at 1100: retries at 1081, 1083, 1087, 1093.5, 1096.75,
    # 1098.375, 1099.375 (all before expiry), then 1100.375, then the 60 s cap.
    assert time.sleeps == pytest.approx([80, 1, 2, 4, 6.5, 3.25, 1.625, 1, 1, 60])
    assert _failures() - before == 9 and len(calls) == 10
    assert keys.current() == "jwt-1" and not keys.ready()
    await keys.close()


async def test_not_ready_after_expiry_until_an_exchange_succeeds(tmp_path) -> None:
    calls: list[httpx.Request] = []
    holder: dict[str, KeySource] = {}
    time = FakeTime(stop_after=5, probe=lambda: holder["keys"].ready())
    replies = [ok("jwt-1", 10), refused(503, "not_ready"), refused(503, "not_ready"), refused(503, "not_ready"), ok("jwt-2", 10)]
    holder["keys"] = keys = _keys(tmp_path, replies, calls, time)
    await keys.start()
    await asyncio.gather(keys._task, return_exceptions=True)
    assert time.sleeps == pytest.approx([8, 1, 1, 4, 8])
    assert time.probes == [True, True, True, False, True]
    assert keys.current() == "jwt-2" and keys.ready()
    await keys.close()


async def test_concurrent_rejections_share_one_reexchange(tmp_path) -> None:
    calls: list[httpx.Request] = []
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls)
    assert await keys.refresh_rejected(0)
    results = await asyncio.gather(*(keys.refresh_rejected(1) for _ in range(5)))
    assert results == [True] * 5 and len(calls) == 2 and keys.generation == 2
    await keys.close()


async def test_a_static_key_never_reexchanges() -> None:
    keys = KeySource("ek_static")
    await keys.start()
    assert (keys.current(), keys.ready(), keys.generation) == ("ek_static", True, 0)
    assert await keys.refresh_rejected(0) is False
    await keys.close()


def _upstream(seen: list[str], reply: Callable[[str], httpx.Response]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return reply(request.headers["x-ach-key"])
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_keyauth_retries_once_with_the_new_generation_when_ach_rejects(tmp_path) -> None:
    calls, seen = [], []
    keys = _keys(tmp_path, [ok("jwt-1"), ok("jwt-2")], calls)
    await keys.refresh_rejected(0)
    client = _upstream(seen, lambda key: httpx.Response(401, json=REJECTED) if key == "jwt-1" else httpx.Response(200))
    response = await client.get("https://ach.example/v1/models", auth=keys.auth)
    assert response.status_code == 200 and seen == ["jwt-1", "jwt-2"] and len(calls) == 2
    await keys.close()


@pytest.mark.parametrize("reply", [
    httpx.Response(401, json={"error": {"message": "invalid api key", "type": "auth_error", "code": "401"}}),
    httpx.Response(401, text="unauthorized"),
    httpx.Response(403, json={"error": {"code": "expired_or_revoked"}}),
])
async def test_keyauth_passes_other_rejections_through(tmp_path, reply) -> None:
    calls, seen = [], []
    keys = _keys(tmp_path, [ok("jwt-1")], calls)
    await keys.refresh_rejected(0)
    response = await _upstream(seen, lambda key: reply).get("https://ach.example/v1/models", auth=keys.auth)
    assert response.status_code == reply.status_code and seen == ["jwt-1"] and len(calls) == 1
    await keys.close()


async def test_keyauth_with_a_static_key_does_not_retry() -> None:
    seen: list[str] = []
    keys = KeySource("ek_static")
    response = await _upstream(seen, lambda key: httpx.Response(401, json=REJECTED)).get("https://ach.example/x", auth=keys.auth)
    assert response.status_code == 401 and seen == ["ek_static"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: PYTEST `tests/harness/test_keys.py`
Expected: FAIL with `ModuleNotFoundError: No module named 'ach_runtime.harness.keys'`.

- [ ] **Step 3: Add the metrics** (`observability/metrics.py`, after `AGENT_INFO`, line 44)

```python
KEY_REFRESH_FAILURES = Counter(
    "ach_agent_key_refresh_failures_total", "Failed exchanges of the ACH credential after boot"
)
KEY_EXPIRY_TIMESTAMP = Gauge(
    "ach_agent_key_expiry_timestamp_seconds", "Unix time at which the current exchanged ACH credential expires"
)
KEY_LIFETIME = Gauge(
    "ach_agent_key_lifetime_seconds", "Lifetime (expires_in) of the current exchanged ACH credential; 0 for a static key"
)
```
Add `"KEY_EXPIRY_TIMESTAMP", "KEY_LIFETIME", "KEY_REFRESH_FAILURES"` to `__all__`.

- [ ] **Step 4: Implement** `runtime/src/ach_runtime/harness/keys.py`

```python
# SPDX-License-Identifier: MIT
"""The control process's ACH credential.

`KeySource` holds a static ek_ (`keyFrom.secretKeyRef`) or a JWT exchanged for the pod's
projected ServiceAccount token (`keyFrom.tokenExchange`). Every consumer reads it per request,
through `current()` or through `auth` on its httpx client, so a rotation reaches clients and
MCP sessions that are already open. A rotation replaces one reference: a request already sent
keeps its JWT, which ACH accepts until it expires.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ach_runtime.observability.metrics import KEY_EXPIRY_TIMESTAMP, KEY_LIFETIME, KEY_REFRESH_FAILURES

log = logging.getLogger(__name__)

REFRESH_AT = 0.8
MIN_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 60.0
# ACH's Authn answers a credential it does not accept with 401 and this code, before it
# proxies anything; an upstream's own 401 carries another code and is never retried.
REJECTED_CODE = "expired_or_revoked"


class ExchangeRefused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(f"ACH token exchange refused: {code}")
        self.code = code


@dataclass(frozen=True, slots=True)
class Exchange:
    base_url: str
    identity: str
    environment: str
    token_path: str


def error_code(response: httpx.Response) -> str:
    """ACH's `{"error": {"code": ...}}`, else http_<status>."""
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    return code if isinstance(code, str) and code else f"http_{response.status_code}"


async def _exchange(client: httpx.AsyncClient, exchange: Exchange) -> tuple[str, float]:
    # The kubelet renews the projected token in place: read it for every exchange.
    token = Path(exchange.token_path).read_text(encoding="utf-8").strip()
    response = await client.post(
        f"{exchange.base_url.rstrip('/')}/platform/keys/exchange",
        headers={"Authorization": f"Bearer {token}"},
        json={"identity": exchange.identity, "environment": exchange.environment},
        follow_redirects=False,
    )
    if response.status_code != 200:
        raise ExchangeRefused(error_code(response))
    try:
        body = response.json()
        jwt, expires_in = body["token"], float(body["expires_in"])
    except (ValueError, KeyError, TypeError) as exc:
        raise ExchangeRefused("invalid_response") from exc
    if not isinstance(jwt, str) or not jwt or expires_in <= 0:
        raise ExchangeRefused("invalid_response")
    return jwt, expires_in


class KeySource:
    def __init__(
        self,
        value: str = "",
        *,
        exchange: Exchange | None = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._value = value
        self._exchange = exchange
        self._client = client
        self._clock, self._wall, self._sleep = clock, wall, sleep
        self._expires_at = float("inf")
        self._refresh_at = float("inf")
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self.generation = 0
        self.auth = KeyAuth(self)

    def current(self) -> str:
        return self._value

    def ready(self) -> bool:
        """False once an exchanged credential expired with no replacement."""
        return bool(self._value) and self._clock() < self._expires_at

    async def start(self) -> None:
        """Exchange once (a refusal raises ExchangeRefused with ACH's code), then keep rotating."""
        if self._exchange is None:
            return
        await self._renew()
        self._task = asyncio.create_task(self._rotate())

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._client is not None:
            await self._client.aclose()

    async def refresh_rejected(self, generation: int) -> bool:
        """ACH rejected a request sent with `generation`. Concurrent callers wait on one shared
        re-exchange; True when a newer credential is there to retry with."""
        if self._exchange is None:
            return False
        async with self._lock:
            if self.generation == generation:
                try:
                    await self._renew()
                except Exception as exc:  # noqa: BLE001 - the caller keeps ACH's answer
                    KEY_REFRESH_FAILURES.inc()
                    log.warning("ACH credential re-exchange failed (%s)", exc)
        return self.generation != generation

    async def _renew(self) -> None:
        jwt, expires_in = await _exchange(self._client, self._exchange)
        now = self._clock()
        self._value, self.generation = jwt, self.generation + 1
        self._expires_at, self._refresh_at = now + expires_in, now + REFRESH_AT * expires_in
        KEY_EXPIRY_TIMESTAMP.set(self._wall() + expires_in)
        KEY_LIFETIME.set(expires_in)

    async def _rotate(self) -> None:
        backoff = MIN_BACKOFF_SECONDS
        while True:
            wait = self._refresh_at - self._clock()
            if wait > 0:
                await self._sleep(wait)
                continue
            try:
                async with self._lock:
                    await self._renew()
            except Exception as exc:  # noqa: BLE001 - retried until an exchange succeeds
                KEY_REFRESH_FAILURES.inc()
                log.warning("ACH credential refresh failed (%s)", exc)
                remaining = self._expires_at - self._clock()
                # Clamped so a retry still happens before the current credential expires.
                delay = min(backoff, max(remaining / 2, MIN_BACKOFF_SECONDS)) if remaining > 0 else backoff
                backoff = min(backoff * 2, MAX_BACKOFF_SECONDS)
                await self._sleep(delay)
            else:
                backoff = MIN_BACKOFF_SECONDS


class KeyAuth(httpx.Auth):
    """Sets `x-ach-key` from the KeySource on every request. When ACH itself rejects the
    credential (401 `expired_or_revoked`, before proxying), waits for the shared re-exchange
    and resends once, so the upstream never sees a request twice."""

    def __init__(self, keys: KeySource) -> None:
        self._keys = keys

    async def async_auth_flow(self, request: httpx.Request) -> AsyncGenerator[httpx.Request, httpx.Response]:
        generation = self._keys.generation
        request.headers["x-ach-key"] = self._keys.current()
        response = yield request
        if response.status_code != 401 or "json" not in response.headers.get("content-type", ""):
            return
        await response.aread()
        if error_code(response) == REJECTED_CODE and await self._keys.refresh_rejected(generation):
            request.headers["x-ach-key"] = self._keys.current()
            yield request
```

- [ ] **Step 5: Run the tests**

Run: PYTEST `tests/harness/test_keys.py tests/observability`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add runtime/src/ach_runtime/harness/keys.py runtime/src/ach_runtime/observability/metrics.py runtime/tests/harness/test_keys.py
/usr/bin/git commit -m "feat(harness): add KeySource with ACH token exchange and rotation"
```

---

### Task 4: Every ACH consumer reads the `KeySource` per request

**Files:**
- Modify: `runtime/src/ach_runtime/facades/_proxy.py:205-224,228-240,302`
- Modify: `runtime/src/ach_runtime/facades/model.py:113-160`, `facades/mcp.py:149-209`
- Modify: `runtime/src/ach_runtime/facades/_mcp_session.py:22-31`, `facades/_memory_client.py:160-232`, `facades/memory.py:133-177`
- Modify: `runtime/src/ach_runtime/facades/a2a.py:31-56,180-191,252-284`
- Modify: `runtime/src/ach_runtime/harness/hydrate.py:320-345,363-391,407-431`
- Modify: `runtime/src/ach_runtime/harness/control.py:51-58,216-218,504-510,526-537,776-786,884-1035`
- Modify: `runtime/src/ach_runtime/harness/app.py:37-69`
- Modify tests: `runtime/tests/harness/test_hydrate.py:394-405,435,448,466,474-480,491,509,538,568,601,632,668`, `runtime/tests/harness/test_control.py:2002,2145-2210`, `runtime/tests/facades/test_a2a.py:176-201` and every `FakeAsyncClient.__init__` in that file, `runtime/tests/harness/test_a2a_egress_assembly.py:205`
- Create tests: `runtime/tests/facades/test_key_rotation.py`, `runtime/tests/harness/test_control_exchange.py`; add one test to `runtime/tests/harness/test_app.py`

**Interfaces:**
- Consumes: `KeySource`, `KeyAuth`, `Exchange`, `ExchangeRefused` (Task 3); `ExchangeKey`, `SecretAlias`, `AchServer` (Task 1).
- Produces:
  - `forward(..., auth: httpx.Auth | None = None)`; `ModelFacade(..., auth=None)`; `McpFacade(..., auth=None)`; `AchMemoryFacade(..., auth=None)`.
  - `mcp_session(endpoint, headers, auth=None)`; `call_ach_memory(..., *, identity, auth=None)`; `fetch_context(..., *, identity, auth=None)`; `prepare_ach_memory(..., *, identity, auth=None)`.
  - `A2AAgentClient(url, auth: httpx.Auth | None = None, timeout=120.0, *, identity)` (replaces `api_key`); `build_a2a_tools(agents, auth=None, client_factory=None, *, identity)`; `A2AEgressFacade(*, agents, auth, identity, key, owned_workspace, exclude_tools, client_factory=None)`.
  - `Hydrator(*, resolve_ek: Callable[[], str], client=None)`; `HydrationInput` without `key_env`; `stage_context(catalog, root, *, excluded_skills, client)` (the client carries the key).
  - `Control.ach_key: KeySource | None`; `Control._memory_auth`.
  - Readiness (`/readyz`, webhook, A2A bridge) is false while `control.ach_key.ready()` is false.

- [ ] **Step 1: Write the failing facade tests** (`runtime/tests/facades/test_key_rotation.py`)

```python
# SPDX-License-Identifier: MIT
"""Model, MCP, memory and A2A clients pick up every new ACH credential generation without a
restart, and retry once only when ACH itself rejected the credential."""

from __future__ import annotations

import asyncio

import httpx

from ach_runtime.facades import _mcp_session
from ach_runtime.facades._memory_client import call_ach_memory
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.a2a import A2AAgentClient
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.keys import Exchange, KeySource
from ach_runtime.harness.tokens import facade_token
from fixtures.memory_mcp import RecordingUpstream

KEY = b"k" * 32
IDENTITY = ProcessIdentity(agent="a", environment="prod")
TOKEN = facade_token(KEY, "ws")
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_1"}


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def _keys(tmp_path, exchanges: list[httpx.Request]) -> KeySource:
    """An exchanging KeySource whose n-th exchange returns jwt-<n>; already holds jwt-1."""
    token = tmp_path / "token"
    token.write_text("sa-token")

    def handler(request: httpx.Request) -> httpx.Response:
        exchanges.append(request)
        return httpx.Response(200, json={"token": f"jwt-{len(exchanges)}", "expires_in": 900, "key_id": "ekid_1"})

    keys = KeySource(exchange=Exchange("https://ach.example", "ai.run@example.com", "prod", str(token)),
                     client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await keys.refresh_rejected(0)
    return keys


def _model(keys: KeySource, upstream) -> ModelFacade:
    return ModelFacade(endpoint="https://ach.example/v1", headers={}, api="openai", model="m", key=KEY,
                       owned_workspace=_owned, identity=IDENTITY, auth=keys.auth,
                       client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))


async def _chat(facade: ModelFacade) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        return await client.post(f"/t/{TOKEN}/chat/completions", json={"model": "m", "messages": []},
                                 headers={"x-ach-key": "sandbox-guess"})


async def test_model_and_mcp_facades_send_the_generation_current_at_each_request(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    model = _model(keys, upstream)
    mcp = McpFacade(servers={"s": "https://ach.example/mcp/s"}, server_headers={"s": {}}, key=KEY,
                    owned_workspace=_owned, exclude=frozenset(), exclude_tools=frozenset(), identity=IDENTITY,
                    auth=keys.auth, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp.app()), base_url="http://f") as client:
        assert (await _chat(model)).status_code == 200
        assert (await client.post(f"/t/{TOKEN}/mcp/s", json=ping)).status_code == 200
        assert await keys.refresh_rejected(keys.generation)  # a rotation: jwt-2
        assert (await _chat(model)).status_code == 200
        assert (await client.post(f"/t/{TOKEN}/mcp/s", json=ping)).status_code == 200
    assert seen == ["jwt-1", "jwt-1", "jwt-2", "jwt-2"]
    await keys.close()


async def test_a_credential_ach_rejects_is_reexchanged_and_the_request_sent_once_more(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        if request.headers["x-ach-key"] == "jwt-1":
            return httpx.Response(401, json=REJECTED)
        return httpx.Response(200, json={"ok": True})

    response = await _chat(_model(keys, upstream))
    assert response.status_code == 200 and response.json() == {"ok": True}
    assert seen == ["jwt-1", "jwt-2"] and len(exchanges) == 2
    await keys.close()


async def test_an_upstream_401_is_passed_through_without_a_retry(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(401, json={"error": {"message": "invalid api key", "type": "auth_error", "code": "401"}})

    assert (await _chat(_model(keys, upstream))).status_code == 401
    assert seen == ["jwt-1"] and len(exchanges) == 1
    await keys.close()


async def test_concurrent_requests_ach_rejects_share_one_reexchange(tmp_path) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    gate = asyncio.Event()

    async def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        if request.headers["x-ach-key"] == "jwt-1":
            await gate.wait()
            return httpx.Response(401, json=REJECTED)
        return httpx.Response(200, json={})

    model = _model(keys, upstream)
    tasks = [asyncio.create_task(_chat(model)) for _ in range(5)]
    async with asyncio.timeout(5):
        while seen.count("jwt-1") < 5:
            await asyncio.sleep(0.01)
    gate.set()
    responses = await asyncio.gather(*tasks)
    assert [r.status_code for r in responses] == [200] * 5
    assert len(exchanges) == 2 and seen.count("jwt-2") == 5
    await keys.close()


async def test_an_open_memory_mcp_session_carries_the_new_generation(tmp_path, monkeypatch) -> None:
    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    upstream = RecordingUpstream()
    upstream.responses["recall"] = "remembered"
    app = upstream.app()

    async def record(request: httpx.Request) -> None:
        seen.append(request.headers["x-ach-key"])
        if len(seen) == 1:  # rotate right after the session's initialize was sent
            await keys.refresh_rejected(keys.generation)

    def client(headers=None, auth=None, **_):
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost:8080/",
                                 headers=headers, auth=auth, event_hooks={"request": [record]})

    monkeypatch.setattr(_mcp_session, "create_mcp_http_client", client)
    async with app.router.lifespan_context(app):
        text = await call_ach_memory("http://localhost:8080/", {}, "recall", {"query": "q"},
                                     identity=IDENTITY, auth=keys.auth)
    assert text == "remembered"
    assert seen[0] == "jwt-1" and len(seen) >= 3 and set(seen[1:]) == {"jwt-2"}
    await keys.close()


async def test_the_a2a_client_built_once_sends_each_new_generation(tmp_path, monkeypatch) -> None:
    import a2a.client

    exchanges, seen = [], []
    keys = await _keys(tmp_path, exchanges)
    real = httpx.AsyncClient
    built: dict[str, httpx.AsyncClient] = {}

    def peer(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["x-ach-key"])
        return httpx.Response(200, json={})

    async def fake_create_client(*, agent: str, client_config: object) -> object:
        built["http"] = client_config.httpx_client
        return object()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(peer), **kw))
    monkeypatch.setattr(a2a.client, "create_client", fake_create_client)
    client = A2AAgentClient("https://ach.example/a2a/peer", auth=keys.auth, identity=IDENTITY)
    await client._ensure_client()
    await built["http"].get("https://ach.example/a2a/peer")
    await keys.refresh_rejected(keys.generation)
    await built["http"].get("https://ach.example/a2a/peer")
    await client.close()
    assert seen == ["jwt-1", "jwt-2"]
    await keys.close()
```

- [ ] **Step 2: Write the failing assembly and readiness tests**

`runtime/tests/harness/test_control_exchange.py`:

```python
# SPDX-License-Identifier: MIT
"""assemble() with capability.ach.key.exchange: exchange before hydrate, ACH's code on refusal."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.control import assemble
from ach_runtime.harness.keys import ExchangeRefused


def _exchange_config(token_path: Path):
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    ach = wire["capability"]["ach"]
    ach["key"] = {"exchange": {"identity": "ai.run@example.com", "environment": ach["environment"],
                               "tokenPath": str(token_path)}}
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _ach(exchange_reply: httpx.Response, seen: list[tuple[str, str | None, str | None]]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.path, request.headers.get("x-ach-key"), request.headers.get("authorization")))
        if request.url.path == "/platform/keys/exchange":
            return exchange_reply
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json={
                "environment": "engineering-prod",
                "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                            "mcpServers": [{"id": "mcp-x", "endpoint": "https://ach.ackstorm.ai/mcp/mcp-x"}], "a2aAgents": []},
                "context": {"skills": [], "prompts": [], "artifacts": []},
            })
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": []})
        raise AssertionError(request.url)
    return httpx.MockTransport(handler)


async def test_boot_exchanges_the_projected_token_before_hydrate(tmp_path) -> None:
    token = tmp_path / "token"
    token.write_text("sa-token\n")
    seen: list[tuple[str, str | None, str | None]] = []
    control = await assemble(
        _exchange_config(token), environ={"ACH_SANDBOX_KEY": "secret"},
        ach_transport=_ach(httpx.Response(200, json={"token": "jwt-1", "expires_in": 900, "key_id": "ekid_1"}), seen),
    )
    try:
        assert seen[0] == ("/platform/keys/exchange", None, "Bearer sa-token")
        assert ("/platform/hydrate", "jwt-1", None) in seen
        assert control.ach_key.current() == "jwt-1" and control.ach_key.ready()
        assert control._model_headers == {} and control._mcp_headers == {"mcp-x": {}}
        assert control.upstream_secrets == ()
    finally:
        await control.close()


async def test_a_refused_exchange_fails_boot_with_achs_code(tmp_path) -> None:
    token = tmp_path / "token"
    token.write_text("sa-token")
    with pytest.raises(ExchangeRefused, match="not_granted"):
        await assemble(
            _exchange_config(token), environ={"ACH_SANDBOX_KEY": "secret"},
            ach_transport=_ach(httpx.Response(403, json={"error": {"code": "not_granted", "message": "x"}}), []),
        )
```

Append to `runtime/tests/harness/test_app.py`:

```python
async def test_readyz_follows_the_ach_credential() -> None:
    state = {"ready": True}
    control = SimpleNamespace(
        config=SimpleNamespace(channels=[], agent=SimpleNamespace(name="a"), identity_environment="e"),
        admission=SimpleNamespace(), _stopping=False, ach_key=SimpleNamespace(ready=lambda: state["ready"]),
    )
    app = create_app(control)
    app.state.ready = True
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control") as client:
        assert (await client.get("/readyz")).status_code == 200
        state["ready"] = False
        assert (await client.get("/readyz")).status_code == 503
```

- [ ] **Step 3: Run them to verify they fail**

Run: PYTEST `tests/facades/test_key_rotation.py tests/harness/test_control_exchange.py tests/harness/test_app.py`
Expected: FAIL (`unexpected keyword argument 'auth'`, `assemble` raises `KeyError`/`AttributeError` on `ach.key.env`, readiness stays 200).

- [ ] **Step 4: Facades take an `httpx.Auth`**

`facades/_proxy.py`: change `_send_until_headers_or_disconnect` (line 205) to take and use `auth`:

```python
async def _send_until_headers_or_disconnect(
    client: httpx.AsyncClient, upstream_request: httpx.Request, request: Request,
    auth: httpx.Auth | None = None,
) -> httpx.Response | None:
```
and inside it `send_task = asyncio.create_task(client.send(upstream_request, stream=True, auth=auth))`.
In `forward` add the keyword parameter `auth: httpx.Auth | None = None,` after `transform`, document it in the docstring with one sentence (`` `auth` sets the upstream credential per request (`KeyAuth`), after `inject_headers`. ``), and change line 302 to `upstream = await _send_until_headers_or_disconnect(client, upstream_request, request, auth)`.

`facades/model.py`: add `auth: httpx.Auth | None = None,` to `ModelFacade.__init__` (after `client`), and pass `auth=auth,` in the `forward(...)` call (line 152-160).
`facades/mcp.py`: same in `McpFacade.__init__` (after `client`, line 160) and its `forward(...)` call (line 196-207).

`facades/_mcp_session.py`:

```python
import httpx
...
@asynccontextmanager
async def mcp_session(
    endpoint: str, headers: dict[str, str] | None, auth: httpx.Auth | None = None
) -> AsyncIterator[ClientSession]:
    """Open an initialized MCP session against `endpoint` with `headers` injected and `auth`
    applied to every request of the session (initialize, calls and the closing DELETE).

    Empty/absent headers and no auth -> unauthenticated client (the internal/no-auth URL path).
    """
    async with create_mcp_http_client(headers=headers or None, auth=auth) as http_client:
```

`facades/_memory_client.py`: add `auth: httpx.Auth | None = None` as a keyword-only parameter to `call_ach_memory`, `fetch_context` and `prepare_ach_memory`; pass it down: `async with mcp_session(endpoint, with_identity_headers(headers, identity), auth) as session:`, `await call_ach_memory(endpoint, headers, ACH_MEMORY_LOAD_CONTEXT, {"project_slug": project}, identity=identity, auth=auth)`, `await fetch_context(endpoint, headers, project, identity=identity, auth=auth)`. Add `import httpx` to its imports.

`facades/memory.py`: `AchMemoryFacade.__init__` gains `auth: httpx.Auth | None = None,` (after `exclude_tools`), stores `self._auth = auth`, and `_invoke` calls `call_ach_memory(self._endpoint, self._headers, tool, payload, identity=self._identity, auth=self._auth)`. Add `import httpx`.

`facades/a2a.py`: add `import httpx` to the module imports and delete the `import httpx` line inside `_ensure_client`. Replace the constructor and header block:

```python
    def __init__(
        self,
        url: str,
        auth: httpx.Auth | None = None,
        timeout: float = 120.0,
        *,
        identity: ProcessIdentity,
    ) -> None:
        self._url = url
        self._auth = auth
        ...
```
```python
                    from a2a.client import ClientConfig, create_client

                    # The credential comes from `auth` per request, so a rotation reaches
                    # this long-lived client.
                    headers = with_identity_headers({}, self._identity)
                    httpx_client = httpx.AsyncClient(
                        headers=headers,
                        auth=self._auth,
                        timeout=httpx.Timeout(timeout=300.0),
                    )
```
`build_a2a_tools(agents, auth: httpx.Auth | None = None, client_factory=None, *, identity)` builds `A2AAgentClient(url=agent.endpoint, auth=auth, timeout=120.0, identity=identity)`. `A2AEgressFacade.__init__` takes `auth: httpx.Auth | None` instead of `ek: str`, uses `self._default_client(agent, auth, identity)` and `build_a2a_tools(list(agents), auth=auth, ...)`; `_default_client(agent, auth, identity)` returns `A2AAgentClient(agent.endpoint, auth=auth, timeout=120.0, identity=identity)`.

- [ ] **Step 5: Hydrate reads the key through a zero-argument resolver**

`harness/hydrate.py`: delete `key_env: str` from `HydrationInput` (line 329) and `key_env=ach.key.env,` from `from_config` (line 341). Replace lines 363-391 with:

```python
EkResolver = Callable[[], str]


class Hydrator:
    """`resolve_ek` returns the current ACH credential (`KeySource.current`); tests supply a
    fixture — same DI convention as `Admission`'s injected `clock`/`runner`."""

    def __init__(self, *, resolve_ek: EkResolver, client: httpx.AsyncClient | None = None) -> None:
        self._resolve_ek = resolve_ek
        self._client = client

    async def load(self, source: HydrationInput) -> HydratedCatalog:
        identity = ProcessIdentity(agent=source.agent, environment=source.environment)
        ek = self._resolve_ek()
```
(the rest of `load` is unchanged; remove the now-unused `import os` if nothing else uses it).
`stage_context` (line 407): remove the `ek: str` parameter, the `headers = {"x-ach-key": ek}` line and the `if not ek: raise HydrationError(...)` block; the download becomes `response = await client.get(item.download_url, timeout=_REQUEST_TIMEOUT_SECONDS)`. Docstring: `"""Download authorized context into the legacy on-disk skills/prompts layout; `client` carries the ACH credential (KeyAuth)."""`.

Test updates in `tests/harness/test_hydrate.py`: delete `key_env="ACH_SECRET_IDENTITY",` (line 398); replace every `resolve_ek=lambda name: "<v>"` with `resolve_ek=lambda: "<v>"`; `failing_resolver` becomes `def failing_resolver() -> str: raise HydrationError("ACH credential is unset")`; at line 632 build the client as `httpx.AsyncClient(transport=httpx.MockTransport(handler), auth=KeySource("ek_secret").auth)` (import `KeySource` from `ach_runtime.harness.keys`) and call `stage_context(catalog, root, excluded_skills=[], client=client)`; at line 668 drop `, ek="ek"`.

- [ ] **Step 6: Wire `control.py`**

Imports: add `from ach_runtime.config.schema import AchServer, HeaderLiteral, HeaderValue, RuntimeConfig, SecretAlias` (extend line 27) and `from ach_runtime.harness.keys import Exchange, KeySource`.

`Control` fields: replace `_ek: str = field(default="", repr=False)` (line 218) with
```python
    ach_key: KeySource | None = field(default=None, repr=False)
    _memory_auth: Any = field(default=None, repr=False)
```

`prepare()` (line 507): `memory_available, memory_section = await prepare_ach_memory(self._memory_endpoint, self._memory_project, self._memory_headers, identity=self._memory_identity, auth=getattr(self, "_memory_auth", None))`.

`_transfer_context` (lines 526-537):
```python
    async def _transfer_context(self, ticket: AdmissionTicket, endpoint: WorkspaceEndpoint) -> None:
        # The credential travels only in the download requests (KeyAuth), never in the archive
        # or the request body.
        ach_key = getattr(self, "ach_key", None)
        with tempfile.TemporaryDirectory(prefix="ach-context-") as temporary:
            root = Path(temporary) / "context"
            root.mkdir()
            async with httpx.AsyncClient(timeout=30, auth=ach_key.auth if ach_key else None) as client:
                await stage_context(
                    self.catalog, root, excluded_skills=self.config.capability.filter.exclude.skills,
                    client=client,
                )
```

`close()` (after line 783): 
```python
        ach_key = getattr(self, "ach_key", None)
        if ach_key is not None:
            closers.append(ach_key.close)
```

Add before `assemble`:
```python
async def _ach_key(ach: AchServer, env: Mapping[str, str],
                   transport: httpx.AsyncBaseTransport | None) -> KeySource:
    """The static ek_ named by the env alias, or a credential exchanged for the pod's projected
    ServiceAccount token. A refused exchange raises ExchangeRefused with ACH's code."""
    if isinstance(ach.key, SecretAlias):
        value = env.get(ach.key.env, "")
        if not value:
            raise WorkspaceAssemblyUnavailable("ACH identity credential is not configured")
        return KeySource(value)
    params = ach.key.exchange
    key = KeySource(exchange=Exchange(ach.base_url, params.identity, params.environment, params.token_path),
                    client=httpx.AsyncClient(transport=transport, timeout=30))
    try:
        await key.start()
    except BaseException:
        await key.close()
        raise
    return key
```

In `assemble`, replace the ACH branch (lines 885-900) and the standalone `ek = ""` (line 903):
```python
    if config.capability.ach is not None:
        ach = config.capability.ach
        ach_key = await _ach_key(ach, env, ach_transport)
        ach_client = httpx.AsyncClient(transport=ach_transport, timeout=30)
        hydrator = Hydrator(resolve_ek=ach_key.current, client=ach_client)
        try:
            catalog = await hydrator.load(HydrationInput.from_config(config))
        except BaseException:
            await ach_key.close()
            raise
        finally:
            await ach_client.aclose()
        prefix = {"openai": "v1", "gemini": "gemini/v1beta", "anthropic": "anthropic"}[config.model.type]
        model_endpoint = f"{ach.base_url.rstrip('/')}/{prefix}"
        # The ACH credential is set per request by ach_key.auth, never fixed in a header dict.
        model_headers: dict[str, str] = {}
        mcp_headers = {item.id: {} for item in catalog.mcp_servers}
        upstream_secrets = (ach_key.current(),) if isinstance(ach.key, SecretAlias) else ()
    else:
        server = config.capability.standalone
        ach_key = None
```
(keep the rest of the standalone branch). Memory (lines 925-941):
```python
        ok, headers = resolve_ach_memory_auth(params.auth, ach_key.current() if ach_key else None,
                                              resolve_secret=lambda alias: env.get(alias))
        memory_auth = None
        if ok and params.auth is not None and params.auth.type == "ach":
            headers, memory_auth = {}, ach_key.auth
        if ok:
            ...  # unchanged body
        if ok and memory_endpoint:
            memory = AchMemoryFacade(
                endpoint=memory_endpoint, headers=memory_headers, project=memory_project,
                key=key, owned_workspace=lambda claim: control._owned_live(claim), identity=identity,
                exclude_tools=frozenset(config.capability.filter.exclude.tools), auth=memory_auth,
            )
```
and declare `memory_auth = None` next to `memory_auth_secrets: tuple[str, ...] = ()` (line 921) so it exists without memory. Replace `control._ek = ek` (line 966) with `control.ach_key, control._memory_auth = ach_key, memory_auth`. Pass `auth=ach_key.auth if ach_key else None` to `McpFacade(...)` and `ModelFacade(...)`, and replace `ek=ek,` in `A2AEgressFacade(...)` (line 1020) with `auth=ach_key.auth if ach_key else None,`.

- [ ] **Step 7: Readiness** (`harness/app.py`)

After `app.state.a2a_bridges = {}` (line 39) add:
```python
    def serving() -> bool:
        ach_key = getattr(control, "ach_key", None)
        return app.state.ready and not control._stopping and (ach_key is None or ach_key.ready())
```
Use it: the bridge argument becomes `ready=serving,`; `readyz` returns `Response(status_code=200 if serving() else 503)`; the webhook check becomes `if not serving():`.

- [ ] **Step 8: Update the existing tests that named `ek`**

- `tests/harness/test_control.py:2002`: `async def stage(catalog, root, *, excluded_skills, client):`.
- `tests/harness/test_control.py:2145-2150`: `async def fake_call(endpoint, headers, tool, args, *, identity, auth=None):` and record `"auth": auth`; line 2210 becomes
  `assert calls[0]["headers"] == calls[1]["headers"] == {}` followed by `assert calls[0]["auth"] is calls[1]["auth"] is control.ach_key.auth`.
- `tests/facades/test_a2a.py`: every `FakeAsyncClient.__init__(self, *, headers: dict[str, str], timeout: httpx.Timeout)` gains `, auth: object = None`; `A2AEgressFacade(..., ek="...", ...)` (lines 376, 404, 458) becomes `auth=None`; `test_real_a2a_client_receives_process_identity_headers` (lines 176-201) captures `captured["auth"] = auth`, constructs `A2AAgentClient("https://peer.example/a2a", KeySource("ek-peer").auth, identity=IDENTITY)` and asserts
  `captured["headers"] == {"x-ach-agent": "classifier", "x-ach-environment": "platform"}` and `isinstance(captured["auth"], KeyAuth)` (import `KeySource`, `KeyAuth` from `ach_runtime.harness.keys`).
- `tests/harness/test_a2a_egress_assembly.py:205`: `staticmethod(lambda entry, auth, identity: Client())`.

- [ ] **Step 9: Run the tests**

Run: PYTEST `tests/harness tests/facades tests/config tests/observability tests/channels tests/test_mcpcall.py`
Expected: PASS.

- [ ] **Step 10: Commit**

```bash
/usr/bin/git add runtime/src runtime/tests
/usr/bin/git commit -m "feat(harness): read the ACH credential per request in every consumer"
```

---

### Task 5: One redaction for every log record

**Files:**
- Modify: `runtime/src/ach_runtime/observability/runs.py:17` (`_SECRET`), `:81-96` (`configure_logging`), add `install_redaction`
- Modify: `runtime/src/ach_runtime/facades/a2a.py:202,209,222` (tool error text)
- Test: `runtime/tests/observability/test_redaction.py`, one test in `runtime/tests/facades/test_a2a.py`

**Interfaces:**
- Produces: `runs.redact(text)` also scrubs compact JWS (`eyJ…​.….…`); `runs.install_redaction() -> None` (idempotent record factory); `configure_logging()` calls it.

- [ ] **Step 1: Write the failing tests**

`runtime/tests/observability/test_redaction.py`:

```python
# SPDX-License-Identifier: MIT
"""Every log record of the control process is scrubbed: ek_/ek-, compact JWS (an ACH JWT, a
ServiceAccount token) and provider keys, in the message, its arguments and tracebacks."""

from __future__ import annotations

import logging

import pytest

from ach_runtime.observability.runs import install_redaction, redact

JWT = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJla2lkXzEifQ.c2lnbmF0dXJl"
SA = "eyJhbGciOiJSUzI1NiIsImtpZCI6IngifQ.eyJpc3MiOiJrdWJlcm5ldGVzIn0.sig-part_1"
EK = "ek_live_0123456789abcdef"


@pytest.fixture
def redacting():
    previous = logging.getLogRecordFactory()
    install_redaction()
    yield
    logging.setLogRecordFactory(previous)


def test_redact_scrubs_jws_and_keys() -> None:
    assert redact(f"a {JWT} b {SA} c {EK} d sk-abc123") == "a [redacted] b [redacted] c [redacted] d [redacted]"


def test_every_logger_message_argument_and_traceback_is_scrubbed(redacting, caplog) -> None:
    caplog.set_level(logging.INFO)
    for name in ("ach_runtime.facades._memory_client", "ach_runtime.facades.a2a",
                 "ach_runtime.harness.keys", "uvicorn.error", "httpx"):
        logging.getLogger(name).warning("call failed (%s) %s", RuntimeError(f"bad header {JWT}"), EK)
    try:
        raise RuntimeError(f"token {SA}")
    except RuntimeError:
        logging.getLogger("ach_runtime.harness.control").exception("boom %s", JWT)
    text = caplog.text + "".join(record.exc_text or "" for record in caplog.records)
    for secret in (JWT, SA, EK, "eyJ"):
        assert secret not in text
    assert "[redacted]" in text and "RuntimeError" in text


def test_install_is_idempotent(redacting) -> None:
    factory = logging.getLogRecordFactory()
    install_redaction()
    assert logging.getLogRecordFactory() is factory
```

Append to `runtime/tests/facades/test_a2a.py`:

```python
async def test_a2a_tool_errors_returned_to_the_engine_are_redacted() -> None:
    jwt = "eyJhbGciOiJFZERTQSJ9.eyJzdWIiOiJ4In0.c2ln"
    broken = FakeClient(raises=RuntimeError(f"401 with x-ach-key {jwt}"))
    tool = _by_name(build_a2a_tools([_agent("rev")], client_factory=_factory(broken), identity=IDENTITY), "a2a_rev")
    result = await tool.handler(prompt="go")
    assert result["ok"] is False and jwt not in result["error"] and "[redacted]" in result["error"]
```

- [ ] **Step 2: Run them to verify they fail**

Run: PYTEST `tests/observability/test_redaction.py tests/facades/test_a2a.py -k "redact"`
Expected: FAIL (`ImportError: cannot import name 'install_redaction'`).

- [ ] **Step 3: Implement** (`observability/runs.py`)

Replace line 17:
```python
_SECRET = re.compile(
    r"(ek[-_][A-Za-z0-9_\-]+|sk-[A-Za-z0-9_\-]+"
    # A compact JWS (an ACH JWT, a ServiceAccount token): the base64url JSON header starts eyJ.
    r"|eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*)"
)
```
Update the `redact` docstring to `"""Scrub ACH keys, compact JWS and provider keys, plus registered process secrets, without truncating the value."""`.

After `_StderrHandler`, add:
```python
_REDACTING = "_ach_redacting"
_TRACEBACK = logging.Formatter()


def install_redaction() -> None:
    """Scrub every log record of the process when it is created: message, arguments, traceback
    and stack. A record factory rather than a handler filter, so uvicorn's handlers and any
    handler configured later are covered too. Idempotent."""
    previous = logging.getLogRecordFactory()
    if getattr(previous, _REDACTING, False):
        return

    def factory(*args: object, **kwargs: object) -> logging.LogRecord:
        record = previous(*args, **kwargs)
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed format string must not break the caller
            message = str(record.msg)
        record.msg, record.args = redact(message), None
        if record.exc_info:
            record.exc_text = redact(_TRACEBACK.formatException(record.exc_info))
            record.exc_info = None
        if record.stack_info:
            record.stack_info = redact(record.stack_info)
        return record

    setattr(factory, _REDACTING, True)
    logging.setLogRecordFactory(factory)
```
In `configure_logging`, first line of the body: `install_redaction()`; extend its docstring with `Installs the process-wide redaction first.`

`facades/a2a.py`: import `from ach_runtime.observability.runs import redact` and return `{"ok": False, "error": redact(str(exc))}` in the three `except` blocks of `_make_agent_tools` (lines 202-224).

- [ ] **Step 4: Run the tests**

Run: PYTEST `tests/observability tests/facades/test_a2a.py tests/harness/test_handoff.py tests/harness/test_workspaces.py`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add runtime/src/ach_runtime/observability/runs.py runtime/src/ach_runtime/facades/a2a.py runtime/tests/observability/test_redaction.py runtime/tests/facades/test_a2a.py
/usr/bin/git commit -m "fix(observability): redact keys and JWS from every log record"
```

---

### Task 6: Alert `ACHAgentKeyRefreshFailing`

**Files:**
- Modify: `chart/alerts/ach-agent.rules.yaml` (append to group `ach-runtime`)
- Modify: `chart/tests/alerts.test.yaml` (append cases)
- Modify: `chart/tests/render_test.sh:49` (`len(alerts) == 5` → 6), `docs/install.md:71` (Five → Six)

**Interfaces:**
- Consumes: the three gauges/counter from Task 3 (labels `agent`, `environment` from `IdentityRegistry`, `namespace` from the PodMonitor).

- [ ] **Step 1: Write the failing promtool cases** (append to `chart/tests/alerts.test.yaml`)

```yaml
  - name: exchanged credential healthy, failing, expired, static
    interval: 30s
    input_series:
      # Refreshed at 80% of a 900 s lifetime: expiry jumps by 720 s every 720 s.
      - series: 'ach_agent_key_expiry_timestamp_seconds{namespace="ns", agent="ok", environment="prod"}'
        values: '900x23 1620x23 2340x23'
      - series: 'ach_agent_key_lifetime_seconds{namespace="ns", agent="ok", environment="prod"}'
        values: '900x71'
      # Never refreshed after boot.
      - series: 'ach_agent_key_expiry_timestamp_seconds{namespace="ns", agent="stuck", environment="prod"}'
        values: '900x71'
      - series: 'ach_agent_key_lifetime_seconds{namespace="ns", agent="stuck", environment="prod"}'
        values: '900x71'
      # A static ek_ agent exports both gauges at 0.
      - series: 'ach_agent_key_expiry_timestamp_seconds{namespace="ns", agent="static", environment="prod"}'
        values: '0x71'
      - series: 'ach_agent_key_lifetime_seconds{namespace="ns", agent="static", environment="prod"}'
        values: '0x71'
    alert_rule_test:
      - eval_time: 13m
        alertname: ACHAgentKeyRefreshFailing
        exp_alerts: []
      - eval_time: 14m
        alertname: ACHAgentKeyRefreshFailing
        exp_alerts:
          - exp_labels: {namespace: ns, agent: stuck, environment: prod, severity: warning}
            exp_annotations:
              summary: ACH agent stuck cannot refresh its ACH credential
              description: Agent stuck in namespace ns has 60s left on its exchanged ACH credential (negative means expired and the agent is not ready). See ach_agent_key_refresh_failures_total and the control pod log for ACH's error code.
      - eval_time: 20m
        alertname: ACHAgentKeyRefreshFailing
        exp_alerts:
          - exp_labels: {namespace: ns, agent: stuck, environment: prod, severity: warning}
            exp_annotations:
              summary: ACH agent stuck cannot refresh its ACH credential
              description: Agent stuck in namespace ns has -300s left on its exchanged ACH credential (negative means expired and the agent is not ready). See ach_agent_key_refresh_failures_total and the control pod log for ACH's error code.
```

- [ ] **Step 2: Run promtool to verify it fails**

Run (repo root): `timeout 300 docker run --rm --entrypoint promtool -v "$PWD/chart:/chart:ro" -w /chart/tests prom/prometheus:v3.5.0 test rules alerts.test.yaml`
Expected: FAIL (alert `ACHAgentKeyRefreshFailing` not found / expected alerts missing).

- [ ] **Step 3: Add the rule** (append to `chart/alerts/ach-agent.rules.yaml`)

```yaml
    # An exchanged credential is refreshed at 80% of its lifetime, so a healthy agent always
    # has at least 20% left. Under 10% means refresh has failed for half of that window or
    # longer; a negative remainder means it expired. Static ek_ agents export lifetime 0.
    - alert: ACHAgentKeyRefreshFailing
      expr: |
        min by (namespace, agent, environment) (ach_agent_key_expiry_timestamp_seconds - time())
        < 0.1 * max by (namespace, agent, environment) (ach_agent_key_lifetime_seconds > 0)
      labels:
        severity: warning
      annotations:
        summary: ACH agent {{ $labels.agent }} cannot refresh its ACH credential
        description: >-
          Agent {{ $labels.agent }} in namespace {{ $labels.namespace }} has
          {{ $value | printf "%.0f" }}s left on its exchanged ACH credential (negative means
          expired and the agent is not ready). See ach_agent_key_refresh_failures_total and the
          control pod log for ACH's error code.
```
`chart/tests/render_test.sh:49`: `assert len(alerts) == 6 and "ACHAgentKeyRefreshFailing" in alerts, alerts`.
`docs/install.md:71`: replace `Five alerts on \`ach_agent_anomalies_total\` and \`ach_agent_runs_total\`.` with `Six alerts on \`ach_agent_anomalies_total\`, \`ach_agent_runs_total\` and the exchanged ACH credential (\`ACHAgentKeyRefreshFailing\`).`

- [ ] **Step 4: Run the tests**

Run: `timeout 600 make chart-test > /tmp/chart-test.log 2>&1; echo "EXIT=$?"; grep -E 'SUCCESS|FAIL|PASS' /tmp/chart-test.log`
Expected: `EXIT=0`, promtool `SUCCESS`, render `PASS`.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add chart/alerts chart/tests docs/install.md
/usr/bin/git commit -m "feat(chart): alert when an agent cannot refresh its ACH credential"
```

---

### Task 7: Optional agent pod admission policy and install docs

**Files:**
- Create: `chart/templates/agent-pod-policy.yaml`
- Modify: `chart/values.yaml` (after `admissionPolicy`), `chart/values.schema.json` (`properties`), `chart/tests/render_test.sh`
- Modify: `docs/install.md` (new section 7), `CLAUDE.md` (chart line)

**Interfaces:**
- Produces: value `agentPodPolicy.enabled` (bool, default `false`); ValidatingAdmissionPolicy `ach-agent-operator-agent-pods` + binding.

- [ ] **Step 1: Write the failing render assertions** (append to `chart/tests/render_test.sh`, before `echo PASS`)

```bash
out=$(helm template t . "${base[@]}" --set agentPodPolicy.enabled=true)
python3 - "$out" <<'PY'
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.argv[1]) if d]
vaps = {d["metadata"]["name"]: d for d in docs if d["kind"] == "ValidatingAdmissionPolicy"}
pol = vaps["ach-agent-operator-agent-pods"]
text = str(pol["spec"])
assert "system:serviceaccount:kube-system:statefulset-controller" in text, text
assert "system:serviceaccount:ach-agent-system:ach-agent-operator" in text, text
assert {r["resources"][0] for r in pol["spec"]["matchConstraints"]["resourceRules"]} == {"pods", "statefulsets"}
binding = [d for d in docs if d["kind"] == "ValidatingAdmissionPolicyBinding" and d["spec"]["policyName"] == "ach-agent-operator-agent-pods"]
assert binding and binding[0]["spec"]["validationActions"] == ["Deny"]
print("agent pod policy ok")
PY
```
(The existing default render keeps asserting exactly one ValidatingAdmissionPolicy, which proves the policy is off by default.)

- [ ] **Step 2: Run to verify it fails**

Run (repo root): `timeout 300 env PATH="$PWD/runtime/.venv/bin:$PATH" bash chart/tests/render_test.sh`
Expected: FAIL with `KeyError: 'ach-agent-operator-agent-pods'`.

- [ ] **Step 3: Implement**

`chart/templates/agent-pod-policy.yaml`:

```yaml
{{- if .Values.agentPodPolicy.enabled }}
# A pod running as an agent-* ServiceAccount can exchange that Agent's ACH credential. Admit
# one only when the StatefulSet controller creates it for the StatefulSet of the same name, and
# let only the operator create or change such StatefulSets.
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicy
metadata:
  name: {{ include "ach-agent.fullname" . }}-agent-pods
  labels:
    {{- include "ach-agent.labels" . | nindent 4 }}
spec:
  failurePolicy: Fail
  matchConstraints:
    resourceRules:
      - apiGroups: [""]
        apiVersions: ["v1"]
        operations: ["CREATE"]
        resources: ["pods"]
      - apiGroups: ["apps"]
        apiVersions: ["v1"]
        operations: ["CREATE", "UPDATE"]
        resources: ["statefulsets"]
  matchConditions:
    - name: agent-service-account
      expression: >-
        (request.kind.kind == "Pod" && has(object.spec.serviceAccountName) &&
         object.spec.serviceAccountName.startsWith("agent-")) ||
        (request.kind.kind == "StatefulSet" && has(object.spec.template.spec.serviceAccountName) &&
         object.spec.template.spec.serviceAccountName.startsWith("agent-"))
  validations:
    - expression: >-
        request.kind.kind != "Pod" ||
        (request.userInfo.username == "system:serviceaccount:kube-system:statefulset-controller" &&
         has(object.metadata.ownerReferences) &&
         object.metadata.ownerReferences.exists(r, r.kind == "StatefulSet" && r.name == object.spec.serviceAccountName))
      message: "a pod may run as an agent-* ServiceAccount only when the StatefulSet controller creates it for the StatefulSet of that name"
    # A StatefulSet being deleted stays updatable: the garbage collector removes its finalizers.
    - expression: >-
        request.kind.kind != "StatefulSet" || has(object.metadata.deletionTimestamp) ||
        request.userInfo.username == "system:serviceaccount:{{ .Release.Namespace }}:{{ include "ach-agent.serviceAccountName" . }}"
      message: "only the ach-agent operator may create or change a StatefulSet whose pods run as an agent-* ServiceAccount"
---
apiVersion: admissionregistration.k8s.io/v1
kind: ValidatingAdmissionPolicyBinding
metadata:
  name: {{ include "ach-agent.fullname" . }}-agent-pods
  labels:
    {{- include "ach-agent.labels" . | nindent 4 }}
spec:
  policyName: {{ include "ach-agent.fullname" . }}-agent-pods
  validationActions: [Deny]
{{- end }}
```

`chart/values.yaml`, after the `admissionPolicy` block:
```yaml
# Admits a pod running as an agent-* ServiceAccount (a tokenExchange Agent's identity) only from the
# StatefulSet controller for the Agent's own StatefulSet, and lets only the operator create those
# StatefulSets. Profile ServiceAccount names must not start with agent- when this is on.
agentPodPolicy:
  enabled: false
```
`chart/values.schema.json`, add to `properties`: `"agentPodPolicy": {"type": "object", "properties": {"enabled": {"type": "boolean"}}}`.

`docs/install.md`: append section

````markdown
## 7. An ACH Agent

With `capabilityServer: ach` the Agent takes its model, MCP servers and context from an ACH Environment,
with a credential for one identity in that Environment from exactly one of:

- `keyFrom.tokenExchange` (ACH 1.1.0 or later). The control pod runs as its own ServiceAccount
  `agent-<agent name>` (created by the operator; a profile's `control.serviceAccountName` does not apply)
  and exchanges that ServiceAccount's token at ACH for a short-lived credential, which it renews at 80% of
  its lifetime. No key is stored in the cluster. `environment` is required and `baseUrl` must be
  `https://` and equal ACH's `ACH_BASE_URL` (a trailing slash is ignored): it is the token's audience.
- `keyFrom.secretKeyRef`: a static `ek_` in a Secret, for an ACH outside this cluster or a manual key.

```yaml
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: Agent
metadata: {name: k8s-doctor, namespace: ach-agent}
spec:
  profileRef: {name: default}
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
the ServiceAccount `<namespace>/agent-<agent name>` (see the ACH documentation). Without one the control pod
fails to start and logs ACH's code, for example `ACH token exchange refused: not_granted`.

Anyone who can create a pod in the Agent's namespace can run it as `agent-<name>` and act as that Agent's
identity. Keep Agents in a namespace where only Flux and the ach-agent operator create pods, StatefulSets
and Agents, and consider `--set agentPodPolicy.enabled=true` (needs the controller manager's
`--use-service-account-credentials`, the default on kubeadm and EKS). ACH reviews the token with a
TokenReview, whose request body contains it: keep `tokenreviews` at audit level `Metadata`.

The control pod exports `ach_agent_key_expiry_timestamp_seconds`, `ach_agent_key_lifetime_seconds` and
`ach_agent_key_refresh_failures_total`; `ACHAgentKeyRefreshFailing` fires when less than 10% of the
credential's lifetime is left.
````

`CLAUDE.md` line 6: `- \`chart/\` Helm chart (operator, CRDs, RBAC, Secret admission policy, optional agent pod policy); \`make chart-test\`.`

- [ ] **Step 4: Run the tests**

Run: `timeout 600 make chart-test > /tmp/chart-test.log 2>&1; echo "EXIT=$?"; grep -E 'ok|PASS|FAIL' /tmp/chart-test.log`
Expected: `EXIT=0`, `agent pod policy ok`, `PASS`.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add chart docs/install.md CLAUDE.md
/usr/bin/git commit -m "feat(chart): optional admission policy for agent ServiceAccounts"
```

---

### Task 8: e2e exchange stub with a real TokenReview and the rotation acceptance test

**Files:**
- Modify: `test/e2e/stubs/stub.py` (full replacement below), `test/e2e/stubs/test_stub.py`, `test/e2e/stubs/Dockerfile`
- Create: `test/e2e/stubs/load.py`, `test/e2e/control-ca.Dockerfile`, `test/e2e/fixtures/ach-exchange-agent.yaml`
- Modify: `test/e2e/stub.yaml`, `test/e2e/cluster.sh`, `test/e2e/e2e_test.go`, `test/e2e/helpers_test.go:206-225`, `.gitignore`, `CLAUDE.md` (e2e line)

**Interfaces:**
- Consumes: everything above. Stub port 8093 = HTTPS ACH surface at `https://e2e-stub.ach-agent-e2e.svc.cluster.local:8093` (also the token audience), JWT lifetime `STUB_JWT_TTL_SECONDS=60`.
- Produces: `stub.State(review=...)`, `State.issue()`, `State.jwt_valid()`, kind `"exchange"`; `load.py` JSON summary `{model_ok, model_failed, mcp_ok, mcp_failed, stream_ok, stream_error, errors}`; Go helpers `facadeToken(t, uid, claim)`, `loadSummary`.

- [ ] **Step 1: Write the failing stub tests** (append to `test/e2e/stubs/test_stub.py`; add `import time` and `import pytest` at the top if missing)

```python
GRANT = {"identity": stub.EXCHANGE_IDENTITY, "environment": "e2e"}


@pytest.fixture
def exchange():
    reviews = []

    def review(token):
        reviews.append(token)
        return {"authenticated": token == "good-sa", "audiences": [stub.EXCHANGE_BASE],
                "user": {"username": "system:serviceaccount:ach-agent-e2e:agent-x",
                         "extra": {"authentication.kubernetes.io/pod-name": ["agent-x-0"]}}}

    state = stub.State(review=review)
    server = stub.make_servers(state, {"exchange": 0})["exchange"]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", state, reviews
    server.shutdown()
    server.server_close()


def test_exchange_issues_a_jwt_accepted_until_it_expires(exchange):
    url, state, reviews = exchange
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer good-sa"}, GRANT)
    assert status == 200
    reply = json.loads(text)
    assert reply["expires_in"] == stub.JWT_TTL and reply["token"].startswith("eyJ") and reviews == ["good-sa"]
    chat = {"model": "e2e-model", "messages": []}
    assert call(url + "/v1/chat/completions", "POST", {"x-ach-key": reply["token"]}, chat)[0] == 200
    state.jwts[reply["token"]] = 0  # expired
    status, text = call(url + "/v1/chat/completions", "POST", {"x-ach-key": reply["token"]}, chat)
    assert status == 401 and json.loads(text)["error"]["code"] == "expired_or_revoked"


def test_exchange_refusals_carry_achs_codes(exchange):
    url, _, _ = exchange
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer bad-sa"}, GRANT)
    assert status == 401 and json.loads(text)["error"]["code"] == "invalid_token"
    status, text = call(url + "/platform/keys/exchange", "POST", {"Authorization": "Bearer good-sa"},
                        {**GRANT, "identity": "ceo@example.com"})
    assert status == 403 and json.loads(text)["error"]["code"] == "not_granted"
    assert call(url + "/platform/keys/exchange", "POST", {}, GRANT)[0] == 401


def test_exchange_mcp_needs_the_session_it_opened(exchange):
    url, state, _ = exchange
    jwt = state.issue()
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    req = urllib.request.Request(url + "/mcp/e2e", data=json.dumps(init).encode(), method="POST",
                                 headers={"x-ach-key": jwt})
    with urllib.request.urlopen(req, timeout=5) as r:
        sid = r.headers["Mcp-Session-Id"]
    echo = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": "hi"}}}
    assert call(url + "/mcp/e2e", "POST", {"x-ach-key": jwt}, echo)[0] == 404
    status, text = call(url + "/mcp/e2e", "POST", {"x-ach-key": jwt, "Mcp-Session-Id": sid}, echo)
    assert status == 200 and json.loads(text)["result"]["content"][0]["text"] == "hi"


def test_a_slow_stream_drips_one_chunk_per_second(exchange):
    url, state, _ = exchange
    started = time.monotonic()
    status, text = call(url + "/v1/chat/completions", "POST", {"x-ach-key": state.issue()},
                        {"model": "e2e-model", "messages": [], "stream": True, "e2e_stream_seconds": 2})
    assert status == 200 and text.endswith("data: [DONE]\n\n") and text.count("data: ") == 2 + 4
    assert time.monotonic() - started >= 2
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd test/e2e/stubs && timeout 120 env PYTHONDONTWRITEBYTECODE=1 ../../../runtime/.venv/bin/python -m pytest -q -p no:cacheprovider test_stub.py`
Expected: FAIL (`State() got an unexpected keyword argument 'review'`).

- [ ] **Step 3: Implement the stub** — replace `test/e2e/stubs/stub.py` with:

```python
"""Stub upstreams for the ach-agent e2e: an OpenAI-compatible model (8090), a streamable-HTTP MCP
server (8091), an ACH server keyed by a static ek_ (8092) and an HTTPS ACH server keyed by JWTs it
issues in exchange for a ServiceAccount token it checks with a real TokenReview (8093). Each
records every request, headers included, so tests assert what reached the upstream. stdlib only."""

import base64
import json
import os
import secrets
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

REPLY = "E2E-OK"
BODY_LIMIT = 64 * 1024
ACH_BASE = os.environ.get("STUB_ACH_BASE", "http://e2e-stub.ach-agent-e2e.svc.cluster.local:8092")
# The exchange server's URL is also the audience the Agent's projected token must carry.
EXCHANGE_BASE = os.environ.get("STUB_EXCHANGE_BASE", "https://e2e-stub.ach-agent-e2e.svc.cluster.local:8093")
EXCHANGE_IDENTITY = os.environ.get("STUB_EXCHANGE_IDENTITY", "ai.run@example.com")
EXCHANGE_SA_PREFIX = "system:serviceaccount:ach-agent-e2e:agent-"
JWT_TTL = int(os.environ.get("STUB_JWT_TTL_SECONDS", "60"))
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
# kind -> (header, accepted value); PUT /_creds replaces a value.
DEFAULT_CREDS = {
    "model": ("x-litellm-api-key", "e2e-model-key"),
    "mcp": ("x-api-key", "e2e-serper-key"),
    "ach": ("x-ach-key", "e2e-ek"),
}
REJECTED = {"error": {"code": "expired_or_revoked", "message": "key expired or revoked"}, "request_id": "req_e2e"}


def token_review(token):
    """A real TokenReview with the stub's own ServiceAccount (bound to system:auth-delegator)."""
    with open(f"{SA_DIR}/token") as f:
        own = f.read().strip()
    body = json.dumps({"apiVersion": "authentication.k8s.io/v1", "kind": "TokenReview",
                       "spec": {"token": token, "audiences": [EXCHANGE_BASE]}}).encode()
    req = urllib.request.Request("https://kubernetes.default.svc/apis/authentication.k8s.io/v1/tokenreviews",
                                 data=body, method="POST",
                                 headers={"Authorization": f"Bearer {own}", "Content-Type": "application/json"})
    context = ssl.create_default_context(cafile=f"{SA_DIR}/ca.crt")
    with urllib.request.urlopen(req, context=context, timeout=10) as r:
        return json.loads(r.read())["status"]


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


class State:
    def __init__(self, review=token_review):
        self.lock = threading.Lock()
        self.creds = dict(DEFAULT_CREDS)
        self.records = {}  # port -> [request dict]
        self.review = review
        self.jwts = {}  # issued JWT -> monotonic expiry
        self.sessions = set()

    def record(self, port, entry):
        with self.lock:
            self.records.setdefault(port, []).append(entry)

    def listing(self, port):
        with self.lock:
            return list(self.records.get(port, []))

    def clear(self):
        with self.lock:
            self.records.clear()

    def issue(self):
        jwt = ".".join((_b64(b'{"alg":"none","typ":"JWT"}'), _b64(b'{"aud":"ach-ek"}'), secrets.token_urlsafe(16)))
        with self.lock:
            self.jwts[jwt] = time.monotonic() + JWT_TTL
        return jwt

    def jwt_valid(self, jwt):
        with self.lock:
            return time.monotonic() < self.jwts.get(jwt, 0)

    def open_session(self):
        sid = secrets.token_hex(8)
        with self.lock:
            self.sessions.add(sid)
        return sid


def _chat(model, stream):
    base = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": int(time.time()), "model": model}
    usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}
    if not stream:
        return {**base, "object": "chat.completion", "usage": usage, "choices": [
            {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": REPLY}}]}
    chunks = [
        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {"content": REPLY}, "finish_reason": None}]},
        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}], "usage": usage},
    ]
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def _mcp(msg):
    method, mid = msg.get("method"), msg.get("id")
    if mid is None:
        return None  # notification
    if method == "initialize":
        version = (msg.get("params") or {}).get("protocolVersion", "2025-03-26")
        result = {"protocolVersion": version, "capabilities": {"tools": {}},
                  "serverInfo": {"name": "e2e-stub", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Echo the text",
                             "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}}]}
    elif method == "tools/call":
        text = ((msg.get("params") or {}).get("arguments") or {}).get("text", "")
        result = {"content": [{"type": "text", "text": text}]}
    elif method == "ping":
        result = {}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def handler_for(state, kind):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _send(self, status, body=b"", ctype="application/json", headers=None):
            if not isinstance(body, bytes):
                body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)

        def _body(self):
            return self.rfile.read(int(self.headers.get("Content-Length") or 0))

        def _admin(self, url):
            query = parse_qs(url.query)
            if self.command == "GET":
                port = int(query["port"][0])
                return self._send(200, state.listing(port))
            if self.command == "DELETE":
                state.clear()
                return self._send(200, {"cleared": True})
            if self.command == "PUT":  # /_creds {"model": "new", ...}
                for k, v in json.loads(self._body()).items():
                    state.creds[k] = (state.creds[k][0], v)
                return self._send(200, {"ok": True})
            return self._send(405)

        def _stream(self, model, seconds):
            """An SSE reply that sends one chunk per second for `seconds`, then ends normally."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            def chunk(text):
                data = text.encode()
                self.wfile.write(b"%x\r\n%s\r\n" % (len(data), data))
                self.wfile.flush()

            tick = {"id": "chatcmpl-e2e", "object": "chat.completion.chunk", "created": int(time.time()),
                    "model": model, "choices": [{"index": 0, "delta": {"content": "."}, "finish_reason": None}]}
            for _ in range(seconds):
                chunk(f"data: {json.dumps(tick)}\n\n")
                time.sleep(1)
            chunk(_chat(model, True))
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def _exchange(self, body):
            auth = self.headers.get("Authorization", "")
            if not auth.startswith("Bearer "):
                return self._send(401, {"error": {"code": "invalid_token", "message": "bearer token required"}})
            try:
                status = state.review(auth[len("Bearer "):])
            except Exception as exc:  # noqa: BLE001 - the API server did not answer the review
                return self._send(503, {"error": {"code": "not_ready", "message": type(exc).__name__}})
            user = status.get("user") or {}
            if not (status.get("authenticated") and EXCHANGE_BASE in (status.get("audiences") or [])
                    and user.get("username", "").startswith(EXCHANGE_SA_PREFIX)
                    and (user.get("extra") or {}).get("authentication.kubernetes.io/pod-name")):
                return self._send(401, {"error": {"code": "invalid_token", "message": "token review failed"}})
            doc = json.loads(body or b"{}")
            if doc.get("identity") != EXCHANGE_IDENTITY or doc.get("environment") != "e2e":
                return self._send(403, {"error": {"code": "not_granted", "message": "no grant"}})
            return self._send(200, {"token": state.issue(), "expires_in": JWT_TTL, "key_id": "ekid_e2e"})

        def _mcp_session(self, doc, headers):
            if self.command != "POST":
                return self._send(405)
            sid = headers.get("mcp-session-id", "")
            if doc.get("method") == "initialize":
                sid = state.open_session()
            elif sid not in state.sessions:
                return self._send(404, {"error": "unknown MCP session"})
            reply = _mcp(doc)
            return self._send(202 if reply is None else 200, b"" if reply is None else reply,
                              headers={"Mcp-Session-Id": sid})

        def _handle(self):
            url = urlsplit(self.path)
            if url.path in ("/_requests", "/_creds"):
                return self._admin(url)
            body = self._body()
            headers = {k.lower(): v for k, v in self.headers.items()}
            state.record(self.server.server_address[1], {
                "method": self.command, "path": url.path, "headers": headers,
                "raw_headers": [[k.lower(), v] for k, v in self.headers.items()],
                "body": body[:BODY_LIMIT].decode("utf-8", "replace"), "time": time.time()})
            if url.path == "/healthz":
                return self._send(200, {"status": "ok"})
            if kind == "exchange":
                if url.path == "/platform/keys/exchange" and self.command == "POST":
                    return self._exchange(body)
                if not state.jwt_valid(headers.get("x-ach-key", "")):
                    return self._send(401, REJECTED)
            else:
                name, want = state.creds[kind]
                if headers.get(name) != want:
                    return self._send(401, {"error": f"{name} missing or wrong"})
            try:
                doc = json.loads(body) if body else {}
            except ValueError:
                return self._send(400, {"error": "invalid JSON"})
            if kind != "mcp" and url.path.endswith("/chat/completions") and self.command == "POST":
                seconds = int(doc.get("e2e_stream_seconds") or 0)
                if doc.get("stream") and seconds:
                    return self._stream(doc.get("model", ""), seconds)
                if doc.get("stream"):
                    return self._send(200, _chat(doc.get("model", ""), True), "text/event-stream")
                return self._send(200, _chat(doc.get("model", ""), False))
            if kind == "mcp" and url.path == "/mcp":
                if self.command == "POST":
                    reply = _mcp(doc)
                    return self._send(202) if reply is None else self._send(200, reply)
                return self._send(405) if self.command == "GET" else self._send(200)
            if kind == "exchange" and url.path == "/mcp/e2e":
                return self._mcp_session(doc, headers)
            if kind in ("ach", "exchange") and url.path == "/platform/hydrate" and self.command == "POST":
                base = EXCHANGE_BASE if kind == "exchange" else ACH_BASE
                mcp = [{"id": "e2e-mcp", "endpoint": f"{EXCHANGE_BASE}/mcp/e2e"}] if kind == "exchange" else []
                return self._send(200, {
                    "environment": "e2e",
                    "runtime": {"models": [{"id": "e2e-model", "endpoint": f"{base}/v1"}],
                                "mcpServers": mcp, "a2aAgents": []},
                    "context": {"skills": [], "prompts": [], "artifacts": []}})
            if kind in ("ach", "exchange") and url.path == "/v2/model/info":
                return self._send(200, {"data": []})
            return self._send(404, {"error": "not found"})

        do_GET = do_POST = do_PUT = do_DELETE = _handle

    return Handler


def make_servers(state, ports):
    """ports: kind -> port (0 picks one). Returns kind -> ThreadingHTTPServer, not yet serving."""
    return {kind: ThreadingHTTPServer(("0.0.0.0", port), handler_for(state, kind)) for kind, port in ports.items()}


if __name__ == "__main__":
    state = State()
    servers = make_servers(state, {"model": 8090, "mcp": 8091, "ach": 8092, "exchange": 8093})
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(os.environ.get("STUB_TLS_CERT", "/tls/tls.crt"), os.environ.get("STUB_TLS_KEY", "/tls/tls.key"))
    servers["exchange"].socket = tls.wrap_socket(servers["exchange"].socket, server_side=True)
    for s in list(servers.values())[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    servers["model"].serve_forever()
```

- [ ] **Step 4: Run the stub tests**

Run: `cd test/e2e/stubs && timeout 120 env PYTHONDONTWRITEBYTECODE=1 ../../../runtime/.venv/bin/python -m pytest -q -p no:cacheprovider test_stub.py`
Expected: PASS (old and new tests).

- [ ] **Step 5: Add the load driver** `test/e2e/stubs/load.py`

```python
"""Acceptance load across credential rotations: model calls, MCP calls on one session kept open
throughout, and one long model stream, all through an Agent's facades. Prints one JSON summary
line. stdlib only."""

import argparse
import json
import threading
import time
import urllib.request


def _post(url, body, headers=None, timeout=30):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json, text/event-stream", **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


class Tally:
    def __init__(self):
        self.lock = threading.Lock()
        self.ok = {"model": 0, "mcp": 0}
        self.failed = {"model": 0, "mcp": 0}
        self.errors = []

    def mark(self, kind, error=None):
        with self.lock:
            if error is None:
                self.ok[kind] += 1
                return
            self.failed[kind] += 1
            if len(self.errors) < 10:
                self.errors.append(f"{kind}: {error}")


def model_loop(base, model, deadline, tally):
    while time.monotonic() < deadline:
        try:
            with _post(f"{base}/chat/completions", {"model": model, "messages": [{"role": "user", "content": "hi"}]}) as r:
                body = json.loads(r.read())
            if body["choices"][0]["message"]["content"] != "E2E-OK":
                raise ValueError(body)
            tally.mark("model")
        except Exception as exc:  # noqa: BLE001 - every failure counts
            tally.mark("model", exc)
        time.sleep(0.5)


def mcp_loop(url, deadline, tally):
    init = {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {
        "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e-load", "version": "1"}}}
    try:
        with _post(url, init) as r:
            session = r.headers["Mcp-Session-Id"]
            r.read()
        with _post(url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, {"Mcp-Session-Id": session}) as r:
            r.read()
    except Exception as exc:  # noqa: BLE001
        tally.mark("mcp", exc)
        return
    n = 0
    while time.monotonic() < deadline:
        n += 1
        call = {"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": {"name": "echo", "arguments": {"text": f"n{n}"}}}
        try:
            with _post(url, call, {"Mcp-Session-Id": session}) as r:
                body = json.loads(r.read())
            if body["result"]["content"][0]["text"] != f"n{n}":
                raise ValueError(body)
            tally.mark("mcp")
        except Exception as exc:  # noqa: BLE001
            tally.mark("mcp", exc)
        time.sleep(0.5)


def stream(base, model, seconds, result):
    body = {"model": model, "stream": True, "e2e_stream_seconds": seconds, "messages": [{"role": "user", "content": "hi"}]}
    try:
        with _post(f"{base}/chat/completions", body, timeout=60) as r:
            text = r.read().decode()
        result["ok"] = text.rstrip().endswith("data: [DONE]") and text.count("data: ") > seconds
    except Exception as exc:  # noqa: BLE001
        result["error"] = str(exc)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--facade", required=True)  # http://agent-<name>.<ns>.svc.cluster.local:8081/facades
    p.add_argument("--token", required=True)
    p.add_argument("--model", default="e2e-model")
    p.add_argument("--mcp", default="e2e-mcp")
    p.add_argument("--seconds", type=int, default=200)
    a = p.parse_args()
    base = f"{a.facade}/model/t/{a.token}"
    deadline = time.monotonic() + a.seconds
    tally, streamed = Tally(), {"ok": False}
    threads = [
        threading.Thread(target=model_loop, args=(base, a.model, deadline, tally)),
        threading.Thread(target=mcp_loop, args=(f"{a.facade}/mcp/t/{a.token}/mcp/{a.mcp}", deadline, tally)),
        threading.Thread(target=stream, args=(base, a.model, a.seconds, streamed)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(json.dumps({"model_ok": tally.ok["model"], "model_failed": tally.failed["model"],
                      "mcp_ok": tally.ok["mcp"], "mcp_failed": tally.failed["mcp"],
                      "stream_ok": streamed["ok"], "stream_error": streamed.get("error", ""), "errors": tally.errors}))


if __name__ == "__main__":
    main()
```

`test/e2e/stubs/Dockerfile`: add `COPY load.py /app/load.py` after the `stub.py` copy and change `EXPOSE 8090 8091 8092` to `EXPOSE 8090 8091 8092 8093`.

- [ ] **Step 6: Cluster plumbing**

`test/e2e/stub.yaml` — prepend:
```yaml
apiVersion: v1
kind: ServiceAccount
metadata: {name: e2e-stub, namespace: ach-agent-e2e}
---
# The exchange stub reviews ServiceAccount tokens like ACH does.
apiVersion: rbac.authorization.k8s.io/v1
kind: ClusterRoleBinding
metadata: {name: ach-agent-e2e-stub-tokenreview}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: system:auth-delegator}
subjects: [{kind: ServiceAccount, name: e2e-stub, namespace: ach-agent-e2e}]
---
```
In the Deployment pod spec add `serviceAccountName: e2e-stub`, the volume `volumes: [{name: tls, secret: {secretName: e2e-stub-tls}}]`, and in the container:
```yaml
          ports: [{containerPort: 8090}, {containerPort: 8091}, {containerPort: 8092}, {containerPort: 8093}]
          env:
            - {name: STUB_JWT_TTL_SECONDS, value: "60"}
          volumeMounts: [{name: tls, mountPath: /tls, readOnly: true}]
```
In the Service add `- {name: exchange, port: 8093}`.

`test/e2e/control-ca.Dockerfile`:
```dockerfile
# The e2e control image trusts the exchange stub's CA; httpx reads SSL_CERT_FILE.
ARG BASE=ach-agent-control:e2e
FROM ${BASE}
COPY ca.crt /etc/ach-e2e/ca.crt
ENV SSL_CERT_FILE=/etc/ach-e2e/ca.crt
```

`test/e2e/cluster.sh`: after `HERE=...` add
```bash
STUB_HOST=e2e-stub.$AGENT_NS.svc.cluster.local
TLS_DIR="$HERE/.tls"
```
add the function
```bash
gen_tls() {
  [ -s "$TLS_DIR/tls.crt" ] && return 0
  log "generating the exchange stub's CA and certificate"
  mkdir -p "$TLS_DIR"
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=ach-agent-e2e-ca" \
    -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$TLS_DIR/ca.key" -out "$TLS_DIR/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=$STUB_HOST" \
    -keyout "$TLS_DIR/tls.key" -out "$TLS_DIR/tls.csr" 2>/dev/null
  printf '%s\n' "subjectAltName=DNS:$STUB_HOST" "basicConstraints=CA:FALSE" \
    "keyUsage=critical,digitalSignature,keyEncipherment" "extendedKeyUsage=serverAuth" \
    "subjectKeyIdentifier=hash" "authorityKeyIdentifier=keyid,issuer" > "$TLS_DIR/ext.cnf"
  openssl x509 -req -in "$TLS_DIR/tls.csr" -CA "$TLS_DIR/ca.crt" -CAkey "$TLS_DIR/ca.key" -CAcreateserial \
    -days 30 -extfile "$TLS_DIR/ext.cnf" -out "$TLS_DIR/tls.crt" 2>/dev/null
}
```
in `build_images`, right after the `--target control` build:
```bash
  docker build -q -t ach-agent-control:e2e -f "$HERE/control-ca.Dockerfile" "$TLS_DIR"
```
in `deploy`, before `k apply -f "$HERE/stub.yaml"`:
```bash
  k -n "$AGENT_NS" create secret tls e2e-stub-tls --cert "$TLS_DIR/tls.crt" --key "$TLS_DIR/tls.key" \
    --dry-run=client -o yaml | k apply -f -
```
and add `--set agentPodPolicy.enabled=true` to the `helm upgrade --install ach-agent` line. In the `up)` case call `gen_tls` before `build_images`; in `load)` call `gen_tls` after `guard`.

`.gitignore`: add `test/e2e/.tls/`.
`CLAUDE.md` e2e line: append ` The exchange stub (8093, HTTPS) uses a CA generated into the gitignored \`test/e2e/.tls/\`; the e2e control image trusts it via \`SSL_CERT_FILE\`.`

`test/e2e/fixtures/ach-exchange-agent.yaml`:
```yaml
apiVersion: ach-agent.ackstorm.ai/v1alpha1
kind: Agent
metadata: {name: "${NAME}", namespace: ach-agent-e2e}
spec:
  profileRef: {name: e2e}
  capabilityServer: ach
  ach:
    baseUrl: https://e2e-stub.ach-agent-e2e.svc.cluster.local:8093
    environment: e2e
    keyFrom:
      tokenExchange: {identity: ai.run@example.com}
    model: {name: e2e-model, api: openai}
  channels:
    - name: hook
      type: webhook
      source: generic
      routing: {sessionKey: "e2e"}
      prompt: "Reply briefly. {{ payload | json }}"
      webhook:
        auth: {type: header_token, header: X-E2E-Token, secretRef: {name: e2e-hook, key: token}}
  expose: {service: true}
```

- [ ] **Step 7: Write the acceptance test**

`test/e2e/helpers_test.go`: `stubRequests` reads every port's records through the plain-HTTP admin port; replace its `kubectl get --raw` line with
```go
	out, err := kubectl("", "get", "--raw", fmt.Sprintf("/api/v1/namespaces/%s/services/e2e-stub:8090/proxy/_requests?port=%d", agentNS, port))
```
and update its comment to `// stubRequests returns what the stub received on one port (8090 model, 8091 MCP, 8092 ACH, 8093 exchange).` Append:

```go
// facadeToken is the Workspace facade token the runtime derives (harness/tokens.py
// facade_token): <claim>.<hex HMAC-SHA256(K, "facade:<claim>")>, K = the agent key Secret's value.
func facadeToken(t *testing.T, agentUID, claim string) string {
	t.Helper()
	raw := mustKubectl(t, "", "-n", agentNS, "get", "secret", "ach-agent-key-"+agentUID, "-o", "jsonpath={.data.key}")
	key, err := base64.StdEncoding.DecodeString(strings.TrimSpace(raw))
	if err != nil {
		t.Fatalf("decode agent key: %v", err)
	}
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte("facade:" + claim))
	return claim + "." + hex.EncodeToString(mac.Sum(nil))
}

type loadSummary struct {
	ModelOK     int      `json:"model_ok"`
	ModelFailed int      `json:"model_failed"`
	McpOK       int      `json:"mcp_ok"`
	McpFailed   int      `json:"mcp_failed"`
	StreamOK    bool     `json:"stream_ok"`
	StreamError string   `json:"stream_error"`
	Errors      []string `json:"errors"`
}
```
(add imports `crypto/hmac`, `crypto/sha256`, `encoding/base64`, `encoding/hex`).

`test/e2e/e2e_test.go`: add imports `encoding/json` and `fmt`, and insert after the `ach-mode` subtest:

```go
	t.Run("token-exchange", func(t *testing.T) {
		const name = "exchange"
		pod := "agent-" + name + "-0"
		clearStub(t)
		apply(t, "fixtures/ach-exchange-agent.yaml", map[string]string{"NAME": name})
		t.Cleanup(func() { kubectl("", "-n", agentNS, "delete", "agent", name, "--wait=false") })
		waitCond(t, name, "Ready", "True", 5*time.Minute)
		if sa := jsonpath(t, "-n", agentNS, "get", "pod", pod, "-o", "jsonpath={.spec.serviceAccountName}"); sa != "agent-"+name {
			t.Fatalf("control pod runs as %q, want agent-%s", sa, name)
		}
		// agentPodPolicy: a pod created by hand cannot borrow the Agent's ServiceAccount.
		if out, err := kubectl("", "-n", agentNS, "run", "sa-borrow", "--image=busybox:1.36", "--restart=Never",
			`--overrides={"apiVersion":"v1","spec":{"serviceAccountName":"agent-`+name+`"}}`); err == nil {
			kubectl("", "-n", agentNS, "delete", "pod", "sa-borrow", "--wait=false")
			t.Errorf("a hand-made pod was admitted as agent-%s:\n%s", name, out)
		} else if !strings.Contains(out, "StatefulSet controller") {
			t.Errorf("the hand-made pod was refused for another reason:\n%s", out)
		}
		postAccepted(t, name, 2*time.Minute)
		waitChat(t, 8093, 4*time.Minute)
		uid := agentUID(t, name)
		workspaces := sandboxes(t, uid)
		if len(workspaces) == 0 {
			t.Fatalf("no workspace for agent uid %s\n%s", uid, agentYAML(name)())
		}
		clearStub(t)
		facade := fmt.Sprintf("http://agent-%s.%s.svc.cluster.local:8081/facades", name, agentNS)
		// JWT lifetime 60 s, refreshed every 48 s: 200 s spans at least three rotations.
		out, err := kubectl("", "-n", agentNS, "exec", "deploy/e2e-stub", "--", "python", "/app/load.py",
			"--facade", facade, "--token", facadeToken(t, uid, workspaces[0]), "--seconds", "200")
		if err != nil {
			t.Fatalf("load driver: %v\n%s", err, out)
		}
		lines := strings.Split(strings.TrimSpace(out), "\n")
		var sum loadSummary
		if err := json.Unmarshal([]byte(lines[len(lines)-1]), &sum); err != nil {
			t.Fatalf("load summary: %v\n%s", err, out)
		}
		if sum.ModelFailed != 0 || sum.McpFailed != 0 || !sum.StreamOK || sum.ModelOK == 0 || sum.McpOK == 0 {
			t.Errorf("requests failed across rotations: %+v", sum)
		}
		exchanges, jwts := 0, map[string]bool{}
		for _, r := range stubRequests(t, 8093) {
			if r.Path == "/platform/keys/exchange" {
				exchanges++
			}
			if strings.HasSuffix(r.Path, "/chat/completions") {
				for _, v := range r.headerValues("x-ach-key") {
					jwts[v] = true
				}
			}
		}
		if exchanges < 3 || len(jwts) < 4 {
			t.Errorf("want >= 3 rotations in the window: %d exchanges, %d distinct credentials on model calls", exchanges, len(jwts))
		}
		if logs, _ := kubectl("", "-n", agentNS, "logs", pod, "-c", "agent"); strings.Contains(logs, "eyJ") {
			t.Error("a JWT or ServiceAccount token reached the control log")
		}
	})
```

- [ ] **Step 8: Run the e2e suite**

```bash
cd /home/coder/workspace/local/ach-agent
timeout 600 bash -c 'cd test/e2e && GOFLAGS=-tags=e2e go vet ./...'
LOG=/tmp/ach-agent-e2e.log; timeout 4800 make e2e > "$LOG" 2>&1; echo "EXIT=$?"; grep -E -- '--- (PASS|FAIL)|^(ok|FAIL)' "$LOG"
timeout 600 make e2e-down
```
Expected: `EXIT=0`, `--- PASS: TestE2E/token-exchange`, every other subtest PASS (`ach-mode` exercises `keyFrom.secretKeyRef`).

- [ ] **Step 9: Commit**

```bash
/usr/bin/git add test/e2e .gitignore CLAUDE.md
/usr/bin/git commit -m "test(e2e): rotate exchanged credentials under load with zero failures"
```

---

### Task 9: Release 0.2.0

**Files:**
- Modify: `runtime/pyproject.toml`, `chart/Chart.yaml`, `CHANGELOG.md` (via `make release-bump`), `chart/tests/render_test.sh:26`, `docs/install.md:55,118-119`

**Interfaces:**
- Consumes: Tasks 1-8 merged on the branch.

- [ ] **Step 1: Bump**

```bash
cd /home/coder/workspace/local/ach-agent && timeout 60 make release-bump VERSION=0.2.0
```
Then `chart/tests/render_test.sh:26` → `assert c["image"].endswith(":0.2.0"), c["image"]`; `docs/install.md` → `--version 0.2.0` (line 55) and `ach-agent-control:0.2.0` / `ach-agent-execution:0.2.0` (lines 118-119).

- [ ] **Step 2: Write the CHANGELOG entry** under the generated `## [0.2.0] - <date>` heading:

```markdown
### Changed

- **Breaking:** `Agent.spec.ach.keySecretRef` is `spec.ach.keyFrom.secretKeyRef`. Rename it in every ACH Agent
  when upgrading. Until it is renamed the operator reports `WorkloadApplied=False` (`RenderFailed`) for that Agent
  and leaves its running pod untouched.

### Added

- `spec.ach.keyFrom.tokenExchange.identity` (needs ACH 1.1.0 and a `KeyGrant`): the control pod runs as its own
  ServiceAccount `agent-<name>`, exchanges a projected token (audience `spec.ach.baseUrl`) at
  `POST /platform/keys/exchange` for a short-lived ACH credential and renews it at 80% of its lifetime with no
  failed request; a credential ACH rejects is re-exchanged once and the request retried. `environment` and an
  `https://` `baseUrl` are required. No Secret is referenced, so rotation never restarts the pod.
- Metrics `ach_agent_key_refresh_failures_total`, `ach_agent_key_expiry_timestamp_seconds`,
  `ach_agent_key_lifetime_seconds` and the alert `ACHAgentKeyRefreshFailing`.
- `agentPodPolicy.enabled` (off by default): a ValidatingAdmissionPolicy that admits a pod running as an
  `agent-*` ServiceAccount only from the StatefulSet controller for the StatefulSet of that name, and lets only
  the operator create those StatefulSets.

### Security

- The control process scrubs ACH keys (`ek-`/`ek_`), compact JWS (ACH credentials, ServiceAccount tokens) and
  provider keys from every log record, tracebacks included, and from A2A tool errors returned to the engine.
```

- [ ] **Step 3: Run the release gates (once)**

```bash
cd /home/coder/workspace/local/ach-agent
timeout 60 make release-check VERSION=0.2.0
L=/tmp/rel-operator.log; timeout 1800 make -C operator verify > $L 2>&1; echo "verify EXIT=$?"; tail -3 $L
L=/tmp/rel-envtest.log; timeout 2400 make -C operator test-envtest > $L 2>&1; echo "envtest EXIT=$?"; grep -E '^(ok|FAIL)' $L
L=/tmp/rel-chart.log; timeout 900 make chart-test > $L 2>&1; echo "chart EXIT=$?"; tail -3 $L
L=/tmp/rel-runtime.log; timeout 3600 make -C runtime test-unit > $L 2>&1; echo "runtime EXIT=$?"; tail -5 $L
```
Expected: `release-check ok: 0.2.0` and every `EXIT=0`. The e2e of Task 8 is not rerun unless a file under `operator/`, `runtime/src`, `chart/` or `test/e2e/` changed after it (`/usr/bin/git diff --stat <task-8 commit>..HEAD -- operator runtime/src chart test/e2e`).

- [ ] **Step 4: Commit (do not push)**

```bash
/usr/bin/git add runtime/pyproject.toml chart/Chart.yaml chart/tests/render_test.sh CHANGELOG.md docs/install.md
/usr/bin/git commit -m "chore(release): v0.2.0"
```
The user fast-forwards `main` to this commit and pushes; `.github/workflows/release.yml` publishes images and chart from the `chore(release): v0.2.0` commit on `main`.

---

### Task 10: GitOps rollout (exact manifests; not executed by this plan)

Repositories (read-only for this plan; the change is applied by the user or a separate session):
- GitOps: `/workspace/private/ackstorm/blueprints/genai-blueprint/gitops`
- Terraform: `/workspace/private/ackstorm/nglz-genai/terraform-genai-blueprint-module`

**Prerequisites (all must hold before commit 1):**
1. ACH 1.1.0 is deployed (plan A), with `KeyGrant` CRD; `gitops/apps/ach/base/helm.yaml:13` pins `version: 1.0.x` today and must allow 1.1.x. ACH's `ACH_BASE_URL` is `https://api.${GENAI_BASE_DOMAIN}` (`apps/ach/base/helm.yaml:131-132`) = `https://api.ackstorm.ai`, equal to the Agents' `baseUrl`.
2. ach-agent 0.2.0 is published (Task 9).
3. `ai.run@ackstorm.com` has logged in to ACH once (spec 4.2).
4. **Terraform applied** (S3 for the per-agent ServiceAccounts; spec fact 1). In `terraform-genai-blueprint-module/terraform.tfvars:36-38` replace the list with:

```hcl
# ach-agent: control pod ServiceAccounts in namespace `ach-agent` that use the S3 backend.
# A keyFrom.tokenExchange Agent runs as its own agent-<name>.
ach_agent_service_accounts = [
  "ach-agent-control",
  "agent-gitlab-reviewer",
  "agent-k8s-doctor",
  "agent-observability-assistant",
  "agent-zohodesk-classifier",
]
```
then `terraform plan` shows only four new `aws_eks_pod_identity_association` resources in `module.ach_agent_pod_identity`; apply.

**Commit 1 (`feat(agents): ACH credentials by token exchange, ach-agent 0.2.0`)**

a) Create `workloads/ach/keygrants/kustomization.yaml`:
```yaml
apiVersion: kustomize.config.k8s.io/v1beta1
kind: Kustomization

resources:
- ai-run-agents.yaml
```
b) Create `workloads/ach/keygrants/ai-run-agents.yaml`:
```yaml
# One KeyGrant per agent: ai.run@ackstorm.com in exactly that agent's Environment, for exactly that
# agent's control ServiceAccount (ach-agent runs a keyFrom.tokenExchange Agent as agent-<name>).
# spec.budget caps that agent's exchange key; the Environment budget still caps the pool.
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-gitlab-reviewer
spec:
  identity: ai.run@ackstorm.com
  environments: [agent-gitlab]
  requesters:
    serviceAccounts: [ach-agent/agent-gitlab-reviewer]
  budget:
    maxBudget: 50
    budgetDuration: 30d
---
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-k8s-doctor
spec:
  identity: ai.run@ackstorm.com
  environments: [agent-k8s-doctor]
  requesters:
    serviceAccounts: [ach-agent/agent-k8s-doctor]
  budget:
    maxBudget: 120
    budgetDuration: 30d
---
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-observability-assistant
spec:
  identity: ai.run@ackstorm.com
  environments: [agent-observability-assistant]
  requesters:
    serviceAccounts: [ach-agent/agent-observability-assistant]
  budget:
    maxBudget: 30
    budgetDuration: 30d
---
apiVersion: ach.ackstorm.ai/v1alpha1
kind: KeyGrant
metadata:
  name: ai-run-zohodesk-classifier
spec:
  identity: ai.run@ackstorm.com
  environments: [agent-zohodesk-classifier]
  requesters:
    serviceAccounts: [ach-agent/agent-zohodesk-classifier]
  budget:
    maxBudget: 30
    budgetDuration: 30d
```
c) `workloads/ach/kustomization.yaml`: add `  - keygrants` to `resources` (after `environments`).

d) Agents — replace each `ach:` key block:

`workloads/agents/achagents/k8s-doctor.yaml:39-42`
```yaml
    baseUrl: https://api.ackstorm.ai
    environment: agent-k8s-doctor
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```
`workloads/agents/achagents/observability-assistant.yaml:32-35`
```yaml
    baseUrl: https://api.ackstorm.ai
    environment: agent-observability-assistant
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```
`workloads/agents/achagents/zohodesk-classifier.yaml:47-51` (drops the `# ek_ of Environment …` comment)
```yaml
    baseUrl: https://api.ackstorm.ai
    environment: agent-zohodesk-classifier
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```
`workloads/agents/achagents/gitlab-pr.yaml:85-89` (drops the `# ek_ of Environment agent-gitlab (ai.run).` comment)
```yaml
    baseUrl: https://api.ackstorm.ai
    environment: agent-gitlab
    keyFrom:
      tokenExchange:
        identity: ai.run@ackstorm.com
```
e) `workloads/agents/agentprofiles/default.yaml`: `ghcr.io/ackstorm/ach-agent-control:0.1.5` → `:0.2.0`; `ghcr.io/ackstorm/ach-agent-execution:0.1.5` → `:0.2.0`.
f) `apps/ach-agent/base/helm.yaml:13`: `version: 0.1.x` → `version: 0.2.x`.
g) Optional (recommended by spec 5.2): in `apps/ach-agent/base/helm.yaml` `values:` add
```yaml
    # Pods running as agent-* (a tokenExchange Agent's identity) only from the StatefulSet controller.
    agentPodPolicy:
      enabled: true
```

Local check before committing: `for layer in infra observability platform-services apps workloads; do timeout 120 kustomize build "$layer" > /dev/null || echo "FAIL $layer"; done` (expected: no `FAIL`).

Expected transient during reconcile: until Helm has replaced the Agent CRD, Flux's apply of the four Agents fails validation (`keyFrom` unknown, `keySecretRef` required); it succeeds on the next Flux retry. The running pods keep serving until their StatefulSets change.

**Verification after commit 1 (read-only; production cluster context):**
```bash
kubectl -n ach get keygrants -o custom-columns='NAME:.metadata.name,READY:.status.conditions[?(@.type=="Ready")].status,REASON:.status.conditions[?(@.type=="Ready")].reason'
kubectl -n ach-agent get agents
for a in gitlab-reviewer k8s-doctor observability-assistant zohodesk-classifier; do
  kubectl -n ach-agent get pod "agent-$a-0" -o jsonpath='{.metadata.name}{" sa="}{.spec.serviceAccountName}{" aud="}{.spec.volumes[?(@.name=="ach-token")].projected.sources[0].serviceAccountToken.audience}{" ready="}{.status.containerStatuses[0].ready}{"\n"}'
  kubectl -n ach-agent get configmap "agent-$a-config" -o jsonpath='{.data.config\.json}' | python3 -c 'import json,sys; print(json.load(sys.stdin)["capability"]["ach"]["key"])'
  kubectl -n ach-agent get statefulset "agent-$a" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}' | tr ' ' '\n' | grep -c '^ACH_SECRET_IDENTITY$' || true
  kubectl -n ach-agent logs "agent-$a-0" -c agent --since=1h | grep -c 'eyJ\|ek_\|refused' || true
done
q() { kubectl get --raw "/api/v1/namespaces/observability/services/prometheus-operated:9090/proxy/api/v1/query?query=$(python3 -c 'import sys,urllib.parse; print(urllib.parse.quote(sys.argv[1]))' "$1")"; echo; }
q 'ach_agent_key_expiry_timestamp_seconds{namespace="ach-agent"} - time()'
q 'increase(ach_agent_key_refresh_failures_total{namespace="ach-agent"}[1h])'
q 'ALERTS{alertname="ACHAgentKeyRefreshFailing"}'
q 'increase(ach_agent_anomalies_total{namespace="ach-agent",kind=~"persistence_failed|restore_failed|configuration_invalid|start_failed"}[1h])'
q 'increase(ach_agent_runs_total{namespace="ach-agent",outcome="completed"}[1h])'
```
Expected: four grants `Ready=True`; Agents `Ready=True`; each pod `sa=agent-<name> aud=https://api.ackstorm.ai ready=true`; key `{'exchange': {...}}`; `ACH_SECRET_IDENTITY` count 0; log matches 0; expiry remainder between 180 and 900 s; refresh failures 0; no alert; anomaly increases 0 (S3 works for `k8s-doctor` and `observability-assistant`); completed runs > 0 for agents that ran. Wait at least 45 minutes (three 12-minute rotations of a 15-minute JWT) with these results before commit 2.

**Commit 2 (`chore(agents): drop the manual ek_ ExternalSecrets`)**
- `workloads/agents/achagents/k8s-doctor.yaml`: delete lines 11-29 (the `ExternalSecret k8s-doctor-ek` document, its `---` and the blank line).
- `workloads/agents/achagents/observability-assistant.yaml`: delete lines 5-22 (`ExternalSecret observability-assistant-ek` and its `---`).
- `workloads/agents/achagents/zohodesk-classifier.yaml`: delete lines 18-36 (the `# ek_ for Agent zohodesk-classifier …` comment, `ExternalSecret zohodesk-classifier-ek`, its `---`).
- `workloads/agents/achagents/gitlab-pr.yaml`: delete lines 46-65 (blank line, `# ek_ for Agent gitlab-reviewer …` comment, `ExternalSecret gitlab-reviewer-ek`, its `---`).
  `creationPolicy: Owner` makes External Secrets delete the four `*-ek` Secrets.

Terraform, same change window (`terraform-genai-blueprint-module`):
- `modules/genai/ach.tf:67-80`: delete the `ACH - AGENT ENVIRONMENT KEYS` comment block and `resource "aws_secretsmanager_secret" "ach_agent_ek"`.
- `modules/genai/kubernetes.tf:54-57`: delete `GENAI_SM_ACH_AGENT_GITLAB_REVIEWER`, `GENAI_SM_ACH_AGENT_K8S_DOCTOR`, `GENAI_SM_ACH_AGENT_OBSERVABILITY_ASSISTANT`, `GENAI_SM_ACH_AGENT_ZOHODESK_CLASSIFIER`.
- `terraform plan` destroys the Secrets Manager secrets `terraform/pro-ack-ai-platform/ach-agent-gitlab-reviewer`, `…/ach-agent-k8s-doctor`, `…/ach-agent-observability-assistant`, `…/ach-agent-zohodesk-classifier` (AWS keeps them for the default recovery window) and updates the `terraform-genai-data` Secret. Apply only after GitOps commit 2 is reconciled (no manifest references `${GENAI_SM_ACH_AGENT_*}`: `grep -rn GENAI_SM_ACH_AGENT_ workloads apps` returns only `docs/`).

Revoke the old manual keys (as an ACH admin; never `revoke --owner`, which would also revoke the exchange keys):
```bash
for env in agent-gitlab agent-k8s-doctor agent-observability-assistant agent-zohodesk-classifier; do
  ach-cli admin keys list --owner ai.run@ackstorm.com --type ek --env "$env" -o json
done
# For each row whose name is not "exchange:ach-agent/agent-<name>":
ach-cli admin keys revoke ekid_<id from the listing>
```
Verify again with the commands after commit 1 (the agents keep `Ready=True`; `kubectl -n ach-agent get externalsecrets` lists none of the `*-ek`).

Optional cleanup once every Agent uses `tokenExchange` (not required by the spec): remove `serviceAccountName: ach-agent-control` from `agentprofiles/default.yaml`, delete `workloads/agents/serviceaccount.yaml` and its kustomization entry, drop `"ach-agent-control"` from `ach_agent_service_accounts`, and update the comment in `apps/ach-agent/base/helm.yaml:35-36` and `apps/openshell/base/mesh.yaml:10-11`.

---

## Self-review

**Spec coverage**
- 5.1 API `keyFrom`, CEL (exactly one; environment and https with tokenExchange): Task 2 (types, envtest CEL cases).
- 5.2 SA `agent-<name>` owned by the Agent, profile SA ignored, projected token (audience, 3600, path), workspace config `exchange`, schema/vendored/hydration/Secret discovery together, no Secret: Tasks 1-2 (and `HydrationInput` in Task 4). Trust boundary docs + optional VAP: Task 7.
- 5.3 KeySource per request (model, MCP, memory auth + memory MCP client incl. initialize/cleanup, A2A long-lived client, context downloads, hydrate): Tasks 3-4. Exchange before hydrate, refusal fails boot with code, no redirects: Tasks 3-4. Rotation at 80% monotonic, swap one reference: Task 3. Refresh failure backoff clamped, not-ready after expiry: Tasks 3-4 (`/readyz`). Single-flight re-exchange on ACH rejection only, no double upstream: Tasks 3-4. Global redaction filter incl. memory/A2A exception text: Task 5. secretKeyRef unchanged: Tasks 2, 4, 8 (`ach-mode`).
- 5.4 metrics + alert: Tasks 3, 6 (plus the lifetime gauge, spec fact 4).
- 6 ach-agent tests: KeySource consumers (Task 4 `test_key_rotation.py`), refresh/backoff/not-ready (Task 3), concurrent re-exchange and upstream 401 not retried (Tasks 3-4), redaction (Task 5, e2e log check), operator SA/projected token/profile SA/CEL (Task 2). Acceptance on kind, 1-minute JWT, model + MCP session + long stream, >= 3 rotations, zero failures, real TokenReview: Task 8.
- 7.2 release 0.2.0 breaking: Task 9. 7.3 GitOps commit 1 / commit 2: Task 10.

**Open points that change scope if answered differently:** budgets in the KeyGrants (Environment budgets mirrored; `zohodesk-classifier` 30 proposed), enabling `agentPodPolicy` in production, and the ACH resolver behaviour in spec fact 6.
