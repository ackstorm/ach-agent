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
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/validation"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

const fixtureAgentUID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"

// minimalFixture builds the smallest profile+agent pair Render accepts (every wire-required
// block fully resolved, a standalone model without headers), for tests that only care about
// one dimension.
func minimalFixture() (v1.AgentProfile, v1.Agent) {
	profile := v1.AgentProfile{
		Spec: v1.AgentProfileSpec{
			Control: v1.ControlSpec{Image: "img"},
			Defaults: v1.AgentDefaults{
				Engine: &v1.EngineSpec{Compaction: &v1.CompactionSpec{Auto: ptr(true), Keep: &v1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))}},
				Limits: &v1.LimitsSpec{
					MaxActiveWorkspaces: ptr(int64(4)), MaxConcurrentInvocations: ptr(int64(4)), MaxInvocationSeconds: ptr(int64(900)),
					MaxQueuedTotal: ptr(int64(100)), IdempotencyWindowSeconds: ptr(int64(3600)), MaxSteps: ptr(int64(30)),
				},
				Workspace: &v1.WorkspaceSpec{
					IdleTimeoutSeconds: ptr(int64(600)), ShutdownTimeoutSeconds: ptr(int64(300)), MaxConcurrentSessions: ptr(int64(1)),
					Persistence: &v1.WorkspacePersistenceSpec{Enabled: ptr(false), RetentionDays: ptr(int64(90))},
					Session: &v1.WorkspaceSessionSpec{
						IdleTimeoutSeconds: ptr(int64(300)),
						Persistence:        &v1.WorkspacePersistenceSpec{Enabled: ptr(false), RetentionDays: ptr(int64(90))},
					},
				},
				Artifacts: &v1.ArtifactsSpec{Enabled: ptr(false), MaxArtifactBytes: ptr(int64(1000)), RetentionDays: ptr(int64(90))},
			},
			Sandbox: v1.ProfileSandboxSpec{OpenShell: v1.OpenShellSpec{Image: "registry.example.com/ach-agent-execution:0.1.0"}},
		},
	}
	agent := v1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: "ach"},
		Spec: v1.AgentSpec{
			ProfileRef:       v1.LocalObjectRef{Name: "p"},
			CapabilityServer: "standalone",
			Standalone: &v1.StandaloneSpec{Model: v1.StandaloneModelSpec{
				Name: "m", API: "openai", BaseURL: "https://model.example.com/v1",
			}},
			Channels: []v1.ChannelSpec{{Name: "c", Type: "cron", Cron: &v1.CronSpec{Schedule: "* * * * *"}}},
		},
	}
	agent.UID = fixtureAgentUID
	return profile, agent
}

// TestRender_RejectsUnsupportedMemoryType is the Important review finding 4 regression:
// workspace-v3's memory union is ach-memory or absent — "codemem" (a retired
// agent-config-v1-only variant) must fail closed through Render, not be silently delegated
// to the legacy renderMemory mapping.
func TestRender_RejectsUnsupportedMemoryType(t *testing.T) {
	profile, agent := minimalFixture()
	agent.Spec.Memory = &v1.MemorySpec{Type: "codemem"}
	if _, _, err := Render(profile, agent); err == nil {
		t.Fatal("expected rejection of memory.type=codemem under workspace-v3")
	}
}

// TestRender_FullOutputConformsToSchemaAndJCSHash runs the real orchestrator end to end for
// a standalone and an ach Agent and checks (1) the complete marshaled document satisfies the
// vendored ach-workspace-config-v3 JSON Schema, and (2) ComputeConfigVersion recomputed over
// Render's own output reproduces the configVersion Render set.
func TestRender_FullOutputConformsToSchemaAndJCSHash(t *testing.T) {
	filter, engine, limits, workspace, artifacts, env, channel := gitlabPRReviewerAgent()
	env = append(env,
		corev1.EnvVar{Name: "GITLAB_REPO_BASEURL", Value: "https://git.example.com"},
		corev1.EnvVar{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "ach-agent-gitlab-ro"}, Key: "GITLAB_TOKEN"}}},
	)
	full := func() (v1.AgentProfile, v1.Agent) {
		profile, agent := minimalFixture()
		agent.Name, agent.Namespace = "gitlab-reviewer", "ach"
		agent.Spec.AgentDefaults = v1.AgentDefaults{Engine: engine, Limits: limits, Workspace: workspace, Artifacts: artifacts}
		agent.Spec.Env, agent.Spec.Channels, agent.Spec.Filter = env, []v1.ChannelSpec{channel}, filter
		return profile, agent
	}
	fixtures := map[string]func() (v1.AgentProfile, v1.Agent){
		"minimal standalone": minimalFixture,
		"standalone": func() (v1.AgentProfile, v1.Agent) {
			p, a := standaloneFixture()
			return p, a
		},
		"full ach": func() (v1.AgentProfile, v1.Agent) {
			p, a := full()
			a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
			a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://ach.example.com", Environment: "engineering-prod",
				KeyFrom: v1.AchKeyFrom{SecretKeyRef: &v1.SecretKeyRef{Name: "test", Key: "ek"}}, Model: v1.AchModelSpec{Name: "gemini.gemini-flash-latest", API: "gemini"}}
			return p, a
		},
		"full ach token exchange": func() (v1.AgentProfile, v1.Agent) {
			p, a := full()
			a.Spec.CapabilityServer, a.Spec.Standalone = "ach", nil
			a.Spec.Ach = &v1.AchServerSpec{BaseURL: "https://ach.example.com", Environment: "engineering-prod",
				KeyFrom: v1.AchKeyFrom{TokenExchange: &v1.AchTokenExchange{Identity: "ai.run@example.com"}},
				Model:   v1.AchModelSpec{Name: "gemini.gemini-flash-latest", API: "gemini"}}
			return p, a
		},
		"full standalone": func() (v1.AgentProfile, v1.Agent) {
			p, a := full()
			a.Spec.Filter = &v1.FilterSpec{Exclude: &v1.ExcludeSpec{McpServers: []string{"serper"}}}
			return p, a
		},
	}
	schema := compileSchema(t, vendoredWorkspaceSchema)
	for name, build := range fixtures {
		t.Run(name, func(t *testing.T) {
			profile, agent := build()
			cfg, _, err := Render(profile, agent)
			if err != nil {
				t.Fatalf("Render: %v", err)
			}
			if cfg.ConfigVersion == "" {
				t.Fatal("Render did not populate configVersion")
			}
			raw, err := json.Marshal(cfg)
			if err != nil {
				t.Fatalf("marshal WSConfig: %v", err)
			}
			var v any
			if err := json.Unmarshal(raw, &v); err != nil {
				t.Fatal(err)
			}
			if err := schema.Validate(v); err != nil {
				t.Fatalf("Render's output violates ach-workspace-config-v3:\n%v", err)
			}
			recomputed, err := ComputeConfigVersion(cfg)
			if err != nil {
				t.Fatalf("recompute configVersion: %v", err)
			}
			if recomputed != cfg.ConfigVersion {
				t.Fatalf("configVersion self-consistency broken: Render set %q, recomputing gives %q", cfg.ConfigVersion, recomputed)
			}
		})
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
	// Never collides with the operator's other per-agent names or the
	// ach-* platform names, for any pair of agent names: different first bytes.
	for _, n := range []string{"foo", "ach-agent-foo", "ach-ws-x", strings.Repeat("z", 60)} {
		if got := ControlName(n); !strings.HasPrefix(got, "agent-") || strings.HasPrefix(got, "ach") {
			t.Fatalf("ControlName(%q) = %q escapes the agent- namespace", n, got)
		}
	}
}

func sha8(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])[:8]
}

// TestRender_AlwaysEmitsAgentName: the runtime names workspace pods agent-<name>-<ref>, so
// agent.name is required on the wire and always rendered from metadata.name.
func TestRender_AlwaysEmitsAgentName(t *testing.T) {
	profile, agent := minimalFixture()
	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
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

// TestRender_WorkspaceHooks: workspaceStart/workspaceStop render as {script,
// timeoutSeconds} and validate against the vendored schema; unset, the keys are ABSENT
// (runtimes <= 0.1.12 forbid unknown keys, so an emitted null would crash-loop them) while
// the three session keys stay present-null.
func TestRender_WorkspaceHooks(t *testing.T) {
	schema := compileSchema(t, vendoredWorkspaceSchema)
	render := func(h *v1.HooksSpec) map[string]any {
		t.Helper()
		profile, agent := minimalFixture()
		agent.Spec.Hooks = h
		cfg, _, err := Render(profile, agent)
		if err != nil {
			t.Fatalf("Render: %v", err)
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
	hooks := render(&v1.HooksSpec{
		WorkspaceStart: &v1.HookSpec{Script: "./start.sh", TimeoutSeconds: &timeout},
		WorkspaceStop:  &v1.HookSpec{Script: "./stop.sh"},
	})
	if got := hooks["workspaceStart"]; !reflect.DeepEqual(got, map[string]any{"script": "./start.sh", "timeoutSeconds": float64(30)}) {
		t.Errorf("workspaceStart = %v", got)
	}
	if got := hooks["workspaceStop"]; !reflect.DeepEqual(got, map[string]any{"script": "./stop.sh"}) {
		t.Errorf("workspaceStop = %v", got)
	}

	for _, h := range []*v1.HooksSpec{nil, {SessionStart: &v1.HookSpec{Script: "x"}}} {
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

func TestRender_InfrastructureIsFacadeEndpointOnly(t *testing.T) {
	profile, agent := minimalFixture()
	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if cfg.SchemaVersion != "workspace-v3" {
		t.Fatalf("schemaVersion = %q, want workspace-v3", cfg.SchemaVersion)
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

func TestRender_ReadyTimeoutComesFromTheProfileExecutionOrIsOmitted(t *testing.T) {
	p, a := minimalFixture()
	cfg, _, err := Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := json.Marshal(cfg)
	if strings.Contains(string(raw), "readyTimeoutSeconds") {
		t.Fatalf("readyTimeoutSeconds rendered although spec.execution does not set it: %s", raw)
	}
	n := int64(90)
	p.Spec.Sandbox.OpenShell.ReadyTimeoutSeconds = &n
	cfg, _, err = Render(p, a)
	if err != nil {
		t.Fatal(err)
	}
	if cfg.Workspace.ReadyTimeoutSeconds == nil || *cfg.Workspace.ReadyTimeoutSeconds != 90 {
		t.Fatalf("workspace.readyTimeoutSeconds = %v, want 90", cfg.Workspace.ReadyTimeoutSeconds)
	}
}
