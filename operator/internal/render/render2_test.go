// SPDX-License-Identifier: MIT

package render

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

const fixtureAgentUID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"

func fixtureExecutionSpec() *achv1alpha1.ExecutionInfraSpec {
	return &achv1alpha1.ExecutionInfraSpec{
		Image: "registry.ackstorm.ai/ach-runtime/execution:0.1.0",
		Resources: &corev1.ResourceRequirements{
			Requests: corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("250m"), corev1.ResourceMemory: resource.MustParse("512Mi")},
			Limits:   corev1.ResourceList{corev1.ResourceCPU: resource.MustParse("1"), corev1.ResourceMemory: resource.MustParse("1Gi")},
		},
		EphemeralStorage:              "2Gi",
		TerminationGracePeriodSeconds: ptr(int64(330)),
	}
}

func TestWorkspaceV1_InfrastructureMatchesFixture(t *testing.T) {
	got := RenderInfrastructureV2("gitlab-reviewer", "ach")
	assertJSONEqual(t, got, "infrastructure")
}

// minimalRender2Fixture builds the smallest profile+agent pair Render2 accepts (every
// wire-required block fully resolved), for tests that only care about one dimension.
func minimalRender2Fixture() (achv1alpha1.AgentProfile, achv1alpha1.ACHAgent) {
	profile := achv1alpha1.AgentProfile{
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{
				Image: "img", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"},
				Engine: &achv1alpha1.EngineSpec{Compaction: &achv1alpha1.CompactionSpec{Auto: ptr(true), Keep: &achv1alpha1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))}},
				Limits: &achv1alpha1.LimitsSpec{
					MaxActiveWorkspaces: ptr(int64(4)), MaxConcurrentInvocations: ptr(int64(4)), MaxInvocationSeconds: ptr(int64(900)),
					MaxQueuedTotal: ptr(int64(100)), IdempotencyWindowSeconds: ptr(int64(3600)), MaxSteps: ptr(int64(30)),
				},
				Workspace: &achv1alpha1.WorkspaceSpec{
					IdleTimeoutSeconds: ptr(int64(600)), ShutdownTimeoutSeconds: ptr(int64(300)), MaxConcurrentSessions: ptr(int64(1)),
					Persistence: &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(false), RetentionDays: ptr(int64(90))},
					Session: &achv1alpha1.WorkspaceSessionSpec{
						IdleTimeoutSeconds: ptr(int64(300)),
						Persistence:        &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(false), RetentionDays: ptr(int64(90))},
					},
				},
				Artifacts: &achv1alpha1.ArtifactsSpec{Enabled: ptr(false), MaxArtifactBytes: ptr(int64(1000)), RetentionDays: ptr(int64(90))},
			},
			Execution: *fixtureExecutionSpec(),
		},
	}
	agent := achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ach"},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef: achv1alpha1.LocalObjectRef{Name: "p"},
			AgentDefaults: achv1alpha1.AgentDefaults{
				Ach: &achv1alpha1.AchSpec{Identity: &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: "ek", Key: "ek"}}},
			},
			Channels: []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	}
	agent.UID = fixtureAgentUID
	return profile, agent
}

// TestRender2_InheritsProfileAchEnvironmentAndCapability is the Important review finding 2
// end-to-end regression: an agent that omits ach.environment/capability must inherit the
// profile's, through the real Render2 entrypoint (not just ResolveAch in isolation).
func TestRender2_InheritsProfileAchEnvironmentAndCapability(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	profile.Spec.Achagent.Ach.Environment = ptr("engineering-prod")
	profile.Spec.Achagent.Ach.Capability = &achv1alpha1.CapabilitySpec{Filter: &achv1alpha1.FilterSpec{Exclude: &achv1alpha1.ExcludeSpec{Skills: []string{"send-email"}}}}

	cfg, err := Render2(profile, agent, "")
	if err != nil {
		t.Fatalf("Render2: %v", err)
	}
	if cfg.Ach.Environment != "engineering-prod" {
		t.Errorf("ach.environment = %q, want inherited profile default", cfg.Ach.Environment)
	}
	if cfg.Ach.Capability == nil || cfg.Ach.Capability.Filter == nil || len(cfg.Ach.Capability.Filter.Exclude.Skills) != 1 {
		t.Errorf("ach.capability = %+v, want inherited profile default", cfg.Ach.Capability)
	}
}

// TestRender2_RejectsUnsupportedMemoryType is the Important review finding 4 regression:
// workspace-v1's memory union is ach-memory or absent — "codemem" (a retired
// agent-config-v1-only variant) must fail closed through Render2, not be silently delegated
// to the legacy renderMemory mapping.
func TestRender2_RejectsUnsupportedMemoryType(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	agent.Spec.Memory = &achv1alpha1.MemorySpec{Type: "codemem"}
	if _, err := Render2(profile, agent, ""); err == nil {
		t.Fatal("expected rejection of memory.type=codemem under workspace-v1")
	}
}

// TestRender2_FullOutputConformsToSchemaAndJCSHash is the end-to-end gate the root asked
// for: the earlier workspacev1_test.go tests only ever feed individual blocks (ach, engine,
// limits, ...) through their own RenderXV1 function and compare that one subtree against the
// fixture — never Render2 itself, and never schema-validate the actual produced document.
// This test runs the REAL orchestrator end to end (full profile+agent -> Render2 -> WSConfig)
// and checks two things the subtree tests cannot: (1) the complete marshaled document
// satisfies the vendored ach-workspace-config-v2 JSON Schema, not just each block in
// isolation, and (2) ComputeConfigVersion recomputed over Render2's own output (with
// configVersion stripped, exactly as Render2 computed it) reproduces the SAME hash Render2
// already set — proving configVersion was computed over what actually got serialized, not
// over some earlier or different struct.
func TestRender2_FullOutputConformsToSchemaAndJCSHash(t *testing.T) {
	ach, engine, limits, workspace, artifacts, env, channel := gitlabPRReviewerAgent()
	env = append(env,
		corev1.EnvVar{Name: "GITLAB_REPO_BASEURL", Value: "https://git.ackstorm.com"},
		corev1.EnvVar{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "ach-agent-gitlab-ro"}, Key: "GITLAB_TOKEN"}}},
	)

	profile := achv1alpha1.AgentProfile{
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{
				Image: "ghcr.io/ackstorm/ach-agent:latest",
				Model: &achv1alpha1.ModelSpec{Name: "openai.gpt-5", Type: "openai"},
			},
			Execution: *fixtureExecutionSpec(),
		},
	}
	agent := achv1alpha1.ACHAgent{
		Spec: achv1alpha1.ACHAgentSpec{
			AgentDefaults: achv1alpha1.AgentDefaults{
				Ach: ach, Engine: engine, Limits: limits, Workspace: workspace, Artifacts: artifacts,
			},
			Env:      env,
			Channels: []achv1alpha1.ChannelSpec{channel},
		},
	}
	agent.Name, agent.Namespace, agent.UID = "gitlab-reviewer", "ach", fixtureAgentUID

	cfg, err := Render2(profile, agent, "")
	if err != nil {
		t.Fatalf("Render2: %v", err)
	}
	if cfg.ConfigVersion == "" {
		t.Fatal("Render2 did not populate configVersion")
	}

	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal WSConfig: %v", err)
	}
	var v any
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	schema := compileSchema(t, vendoredWorkspaceV2Schema)
	if err := schema.Validate(v); err != nil {
		t.Fatalf("Render2's full output violates ach-workspace-config-v2:\n%v", err)
	}

	recomputed, err := ComputeConfigVersion(cfg)
	if err != nil {
		t.Fatalf("recompute configVersion: %v", err)
	}
	if recomputed != cfg.ConfigVersion {
		t.Fatalf("configVersion self-consistency broken: Render2 set %q, recomputing over its own output gives %q", cfg.ConfigVersion, recomputed)
	}
}

// TestRender2_RequiresEffectiveModelName: a profile/agent pair whose merged model is
// missing name must fail closed — ModelSpec.Name has no CRD Required marker any more
// (it may legitimately come from the sibling), so Render2 is the one place completeness
// of the EFFECTIVE value is still enforced.
func TestRender2_RequiresEffectiveModelName(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	profile.Spec.Achagent.Model = &achv1alpha1.ModelSpec{Type: "openai"}
	_, err := Render2(profile, agent, "")
	if err == nil || !strings.Contains(err.Error(), "no effective model.name") {
		t.Fatalf("err = %v, want containing %q", err, "no effective model.name")
	}
}

// TestRender2_DefaultsEffectiveModelType: an unset effective type renders as openai
// without writing it back into the profile.
func TestRender2_DefaultsEffectiveModelType(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	profile.Spec.Achagent.Model = &achv1alpha1.ModelSpec{Name: "m"}
	cfg, err := Render2(profile, agent, "")
	if err != nil {
		t.Fatalf("Render2: %v", err)
	}
	if cfg.Model.Type != DefaultModelType {
		t.Fatalf("model.type = %q, want %q", cfg.Model.Type, DefaultModelType)
	}
	if profile.Spec.Achagent.Model.Type != "" {
		t.Fatalf("profile model.type mutated to %q", profile.Spec.Achagent.Model.Type)
	}
}

// TestRender2_ControlServiceAccountName: unset keeps the per-agent ach-harness-<uid>; set
// resolves to the profile's stable ServiceAccount. (No longer on the wire in workspace-v2.)
func TestRender2_ControlServiceAccountName(t *testing.T) {
	profile, _ := minimalRender2Fixture()
	if got, want := ControlServiceAccountName(fixtureAgentUID, &profile), "ach-harness-"+fixtureAgentUID; got != want {
		t.Fatalf("unset control SA = %q, want %q", got, want)
	}

	profile.Spec.ControlServiceAccountName = "ach-sandboxed-agent"
	if got := ControlServiceAccountName(fixtureAgentUID, &profile); got != "ach-sandboxed-agent" {
		t.Fatalf("set control SA = %q, want ach-sandboxed-agent", got)
	}
}

// TestControlName: agent-<agent name>, trimmed + hashed past 46 chars (or on '.') so the pod
// name and the controller-revision-hash label value ("<sts>-<10 chars>") fit in 63.
func TestControlName(t *testing.T) {
	long := strings.Repeat("a", 37) + "-reviewer-for-gitlab-merge-requests"
	trail := strings.Repeat("c", 36) + "-" + strings.Repeat("d", 20)
	cases := []struct{ name, in, want string }{
		{"short", "gitlab-reviewer", "agent-gitlab-reviewer"},
		{"exactly 46", strings.Repeat("b", 46), "agent-" + strings.Repeat("b", 46)},
		{"over 46", long, "agent-" + strings.Repeat("a", 37) + "-" + sha8(long)},
		{"trailing dash after trim", trail, "agent-" + strings.Repeat("c", 36) + "-" + sha8(trail)},
		{"dot in name", "team.reviewer", "agent-team-reviewer-" + sha8("team.reviewer")},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := ControlName(tc.in)
			if got != tc.want {
				t.Fatalf("ControlName(%q) = %q, want %q", tc.in, got, tc.want)
			}
			if len(got) > 52 {
				t.Fatalf("ControlName(%q) = %q (%d chars): pod or revision-hash label would exceed 63", tc.in, got, len(got))
			}
			if errs := validation.IsDNS1035Label(got); len(errs) > 0 {
				t.Fatalf("ControlName(%q) = %q is not a valid Service name: %v", tc.in, got, errs)
			}
			if ControlName(tc.in) != got {
				t.Fatal("ControlName must be deterministic")
			}
		})
	}
	// Two long names sharing their first 37 chars must not collide.
	a, b := strings.Repeat("x", 37)+"-alpha-agent-long-name", strings.Repeat("x", 37)+"-bravo-agent-long-name"
	if ControlName(a) == ControlName(b) {
		t.Fatalf("long names sharing a prefix collided: %q", ControlName(a))
	}
	// Never collides with the operator's other per-agent Service (achagent-<name>) or the
	// ach-* platform names, for any pair of agent names: different first bytes.
	for _, n := range []string{"foo", "achagent-foo", "ach-ws-x", strings.Repeat("z", 60)} {
		if got := ControlName(n); !strings.HasPrefix(got, "agent-") || strings.HasPrefix(got, "ach") {
			t.Fatalf("ControlName(%q) = %q escapes the agent- namespace", n, got)
		}
	}
}

func sha8(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:8]
}

// TestRender2_AlwaysEmitsAgentName: the runtime names workspace pods agent-<name>-<ref>, so
// agent.name is required on the wire and always rendered from metadata.name.
func TestRender2_AlwaysEmitsAgentName(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	cfg, err := Render2(profile, agent, "")
	if err != nil {
		t.Fatalf("Render2: %v", err)
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Agent map[string]any `json:"agent"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if doc.Agent["name"] != agent.Name || agent.Name == "" {
		t.Fatalf("agent.name = %v, want %q", doc.Agent["name"], agent.Name)
	}
}

// TestRender2_WorkspaceHooks: workspaceStart/workspaceStop render as {script,
// timeoutSeconds} and validate against the vendored schema; unset, the keys are ABSENT
// (runtimes <= 0.1.12 forbid unknown keys, so an emitted null would crash-loop them) while
// the three session keys stay present-null.
func TestRender2_WorkspaceHooks(t *testing.T) {
	schema := compileSchema(t, vendoredWorkspaceV2Schema)
	render := func(h *achv1alpha1.HooksSpec) map[string]any {
		t.Helper()
		profile, agent := minimalRender2Fixture()
		agent.Spec.Hooks = h
		cfg, err := Render2(profile, agent, "")
		if err != nil {
			t.Fatalf("Render2: %v", err)
		}
		raw, err := json.Marshal(cfg)
		if err != nil {
			t.Fatal(err)
		}
		var v map[string]any
		if err := json.Unmarshal(raw, &v); err != nil {
			t.Fatal(err)
		}
		if err := schema.Validate(any(v)); err != nil {
			t.Fatalf("output violates ach-workspace-config-v2:\n%v", err)
		}
		return v["hooks"].(map[string]any)
	}

	timeout := int64(30)
	hooks := render(&achv1alpha1.HooksSpec{
		WorkspaceStart: &achv1alpha1.HookSpec{Script: "./start.sh", TimeoutSeconds: &timeout},
		WorkspaceStop:  &achv1alpha1.HookSpec{Script: "./stop.sh"},
	})
	if got := hooks["workspaceStart"]; !reflect.DeepEqual(got, map[string]any{"script": "./start.sh", "timeoutSeconds": float64(30)}) {
		t.Errorf("workspaceStart = %v", got)
	}
	if got := hooks["workspaceStop"]; !reflect.DeepEqual(got, map[string]any{"script": "./stop.sh"}) {
		t.Errorf("workspaceStop = %v", got)
	}

	for _, h := range []*achv1alpha1.HooksSpec{nil, {SessionStart: &achv1alpha1.HookSpec{Script: "x"}}} {
		hooks = render(h)
		for _, k := range []string{"workspaceStart", "workspaceStop"} {
			if _, present := hooks[k]; present {
				t.Errorf("hooks.%s present for unset hook, want absent: %v", k, hooks)
			}
		}
		for _, k := range []string{"sessionRestore", "sessionSuspend"} {
			if v, present := hooks[k]; !present || v != nil {
				t.Errorf("hooks.%s = %v (present=%v), want null", k, v, present)
			}
		}
	}
}

func TestRenderInfrastructureV2_FQDNFacadeEndpoint(t *testing.T) {
	for _, name := range []string{"gitlab-reviewer", "my.dotted.agent", strings.Repeat("z", 60)} {
		got := RenderInfrastructureV2(name, "ach")
		want := "http://" + ControlName(name) + ".ach.svc.cluster.local:8081/facades"
		if got.FacadeEndpoint != want {
			t.Errorf("facadeEndpoint(%q) = %q, want %q", name, got.FacadeEndpoint, want)
		}
	}
}

func TestRender2_WorkspaceV2_InfrastructureIsFacadeEndpointOnly(t *testing.T) {
	profile, agent := minimalRender2Fixture()
	cfg, err := Render2(profile, agent, "")
	if err != nil {
		t.Fatalf("Render2: %v", err)
	}
	if cfg.SchemaVersion != "workspace-v2" {
		t.Fatalf("schemaVersion = %q, want workspace-v2", cfg.SchemaVersion)
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Infrastructure map[string]any `json:"infrastructure"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Infrastructure) != 1 || doc.Infrastructure["facadeEndpoint"] == nil {
		t.Fatalf("infrastructure = %v, want exactly {facadeEndpoint} (no control, no execution)", doc.Infrastructure)
	}
}

func TestRender2_ReadyTimeoutComesFromTheProfileExecutionOrIsOmitted(t *testing.T) {
	p, a := minimalRender2Fixture()
	cfg, err := Render2(p, a, "http://ach.test")
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	if strings.Contains(string(raw), "readyTimeoutSeconds") {
		t.Fatalf("readyTimeoutSeconds rendered although spec.execution does not set it: %s", raw)
	}
	n := int64(90)
	p.Spec.Execution.ReadyTimeoutSeconds = &n
	cfg, err = Render2(p, a, "http://ach.test")
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Workspace.ReadyTimeoutSeconds == nil || *cfg.Workspace.ReadyTimeoutSeconds != 90 {
		t.Fatalf("workspace.readyTimeoutSeconds = %v, want 90", cfg.Workspace.ReadyTimeoutSeconds)
	}
}
