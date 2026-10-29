// SPDX-License-Identifier: MIT

package render

import (
	"encoding/json"
	"os"
	"reflect"
	"slices"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func ptr[T any](v T) *T                      { return &v }
func rawJSON(s string) *apiextensionsv1.JSON { return &apiextensionsv1.JSON{Raw: []byte(s)} }

func TestReferencedSecrets_NameToKeys(t *testing.T) {
	a := v1.Agent{Spec: v1.AgentSpec{Channels: []v1.ChannelSpec{
		{Name: "w1", Type: "webhook", Webhook: &v1.WebhookSpec{Auth: v1.WebhookAuthSpec{Type: "hmac", SecretRef: &v1.SecretKeyRef{Name: "s1", Key: "kb"}}}},
		{Name: "w2", Type: "webhook", Webhook: &v1.WebhookSpec{Auth: v1.WebhookAuthSpec{Type: "hmac", SecretRef: &v1.SecretKeyRef{Name: "s1", Key: "ka"}}}},
		{Name: "cr", Type: "cron", Cron: &v1.CronSpec{Schedule: "* * * * *"}},
	}}}
	got := ReferencedSecrets(v1.AgentProfile{}, a)
	if len(got) != 1 || len(got["s1"]) != 2 || got["s1"][0] != "ka" || got["s1"][1] != "kb" {
		t.Errorf("ReferencedSecrets = %v, want {s1:[ka kb]}", got)
	}
}

func TestChannelSecretEnv_NamesAndRefs(t *testing.T) {
	a := v1.Agent{Spec: v1.AgentSpec{Channels: []v1.ChannelSpec{
		{Name: "gitlab-mr-review", Type: "webhook", Webhook: &v1.WebhookSpec{Auth: v1.WebhookAuthSpec{Type: "gitlab_token", SecretRef: &v1.SecretKeyRef{Name: "gl", Key: "secret"}}}},
		{Name: "peer-intake", Type: "a2a", A2A: &v1.A2ASpec{Auth: v1.A2AAuthSpec{SecretRef: v1.SecretKeyRef{Name: "peer", Key: "apikey"}}}},
		{Name: "daily", Type: "cron", Cron: &v1.CronSpec{Schedule: "* * * * *"}}, // no secret
	}}}
	got := ChannelSecretEnv(v1.AgentProfile{}, a)
	if len(got) != 2 {
		t.Fatalf("ChannelSecretEnv len = %d, want 2", len(got))
	}
	if got[0].EnvName != "ACH_SECRET_GITLAB_MR_REVIEW_WEBHOOK" || got[0].SecretName != "gl" || got[0].Key != "secret" {
		t.Errorf("webhook ref = %+v", got[0])
	}
	if got[1].EnvName != "ACH_SECRET_PEER_INTAKE_A2A" || got[1].SecretName != "peer" || got[1].Key != "apikey" {
		t.Errorf("a2a ref = %+v", got[1])
	}
}

func TestMemorySecretEnv_AchMemoryAuth(t *testing.T) {
	withAuth := v1.Agent{Spec: v1.AgentSpec{Memory: &v1.MemorySpec{
		Type: "ach-memory", AchMemory: &v1.AchMemorySpec{Endpoint: "http://m/mcp/", Auth: &v1.AchMemoryAuthSpec{Type: memoryAuthTypeBearer, SecretRef: &v1.SecretKeyRef{Name: "am", Key: "token"}}},
	}}}
	ref := MemorySecretEnv(withAuth)
	if ref == nil || ref.EnvName != "ACH_SECRET_MEMORY_AUTH" || ref.SecretName != "am" || ref.Key != "token" {
		t.Errorf("MemorySecretEnv = %+v, want ACH_SECRET_MEMORY_AUTH → am/token", ref)
	}
	// It must join ReferencedSecrets so the reconciler key-check + hash + watch cover it.
	if keys := ReferencedSecrets(v1.AgentProfile{}, withAuth)["am"]; len(keys) != 1 || keys[0] != "token" {
		t.Errorf("ReferencedSecrets missing memory secret: %v", ReferencedSecrets(v1.AgentProfile{}, withAuth))
	}
	// No auth → no ref (internal/no-auth ach-memory URL).
	noAuth := v1.Agent{Spec: v1.AgentSpec{Memory: &v1.MemorySpec{
		Type: "ach-memory", AchMemory: &v1.AchMemorySpec{Endpoint: "http://m/mcp/"},
	}}}
	if ref := MemorySecretEnv(noAuth); ref != nil {
		t.Errorf("MemorySecretEnv(no auth) = %+v, want nil", ref)
	}
	// ach arm → no ref: the harness sends its own ek_, there is no second secret.
	achArm := v1.Agent{Spec: v1.AgentSpec{Memory: &v1.MemorySpec{
		Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
			Endpoint: "https://api.ackstorm.ai/mcp/ach-memory", Auth: &v1.AchMemoryAuthSpec{Type: memoryAuthTypeAch},
		},
	}}}
	if ref := MemorySecretEnv(achArm); ref != nil {
		t.Errorf("MemorySecretEnv(ach arm) = %+v, want nil", ref)
	}
	// Non-ach-memory memory → no ref.
	if ref := MemorySecretEnv(v1.Agent{Spec: v1.AgentSpec{Memory: &v1.MemorySpec{Type: "codemem"}}}); ref != nil {
		t.Errorf("MemorySecretEnv(codemem) = %+v, want nil", ref)
	}
}

func TestRenderMemory_AchMemoryBlock(t *testing.T) {
	out := renderMemory(&v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
		Endpoint: "http://m/mcp/", Project: "team-reviewer",
		Auth: &v1.AchMemoryAuthSpec{Type: memoryAuthTypeBearer, Header: "x-litellm-api-key", SecretRef: &v1.SecretKeyRef{Name: "am", Key: "token"}},
	}})
	b, err := json.Marshal(out)
	if err != nil {
		t.Fatalf("marshal: %v", err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	am := m["achMemory"].(map[string]any)
	// auth renders ONLY the operator-generated env name, never the secretRef.
	// header rides through verbatim: it selects WHICH ach-memory identity provider
	// the token is for, so a rewritten one silently 401s under fail-open memory.
	if auth := am["auth"].(map[string]any); auth["type"] != "bearer" || auth["env"] != "ACH_SECRET_MEMORY_AUTH" || auth["header"] != "x-litellm-api-key" {
		t.Errorf("auth = %v, want {type: bearer, env: ACH_SECRET_MEMORY_AUTH, header: x-litellm-api-key}", auth)
	}
	// endpoint is rendered VERBATIM — the harness appends nothing.
	if am["endpoint"] != "http://m/mcp/" || am["project"] != "team-reviewer" {
		t.Errorf("endpoint/project = %v/%v", am["endpoint"], am["project"])
	}
	// project omitted when unset — the harness derives {POD_NAMESPACE}-{agent.name}.
	// ach arm renders {type: ach} with NO env — there is no second credential.
	achArm := renderMemory(&v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
		Endpoint: "https://api.ackstorm.ai/mcp/ach-memory", Auth: &v1.AchMemoryAuthSpec{Type: memoryAuthTypeAch},
	}})
	ab, _ := json.Marshal(achArm)
	var aM map[string]any
	_ = json.Unmarshal(ab, &aM)
	aAuth := aM["achMemory"].(map[string]any)["auth"].(map[string]any)
	if aAuth["type"] != "ach" {
		t.Errorf("ach arm type = %v, want ach", aAuth["type"])
	}
	if _, present := aAuth["env"]; present {
		t.Errorf("ach arm must not render env: %s", ab)
	}
	if _, present := aAuth["header"]; present {
		t.Errorf("ach arm must not render header: %s", ab)
	}

	// bearer with header unset omits it, so the harness applies its own
	// "Authorization" default rather than ACH restating a value it does not own.
	bareBearer := renderMemory(&v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
		Endpoint: "http://m/mcp/",
		Auth:     &v1.AchMemoryAuthSpec{Type: memoryAuthTypeBearer, SecretRef: &v1.SecretKeyRef{Name: "am", Key: "token"}},
	}})
	bb, _ := json.Marshal(bareBearer)
	var bm map[string]any
	_ = json.Unmarshal(bb, &bm)
	bAuth := bm["achMemory"].(map[string]any)["auth"].(map[string]any)
	if _, present := bAuth["header"]; present {
		t.Errorf("unset header emitted: %s", bb)
	}

	// mcpServerId is the OTHER arm of the where-is-ach-memory choice: it renders
	// instead of endpoint, and its presence is what tells the harness to drop that
	// server from the MCP proxy so the facade is the only path to the bank.
	byID := renderMemory(&v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
		McpServerID: "ach-memory",
		Auth:        &v1.AchMemoryAuthSpec{Type: memoryAuthTypeAch},
	}})
	ib, _ := json.Marshal(byID)
	var im map[string]any
	_ = json.Unmarshal(ib, &im)
	iam := im["achMemory"].(map[string]any)
	if iam["mcpServerId"] != "ach-memory" {
		t.Errorf("mcpServerId = %v, want ach-memory", iam["mcpServerId"])
	}
	if _, present := iam["endpoint"]; present {
		t.Errorf("mcpServerId arm must not render endpoint: %s", ib)
	}

	noAuth := renderMemory(&v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{Endpoint: "http://m/mcp/"}})
	nb, _ := json.Marshal(noAuth)
	var nm map[string]any
	_ = json.Unmarshal(nb, &nm)
	nam := nm["achMemory"].(map[string]any)
	if _, present := nam["auth"]; present {
		t.Errorf("no-auth ach-memory emitted an auth block: %s", nb)
	}
	if _, present := nam["project"]; present {
		t.Errorf("unset project emitted: %s", nb)
	}
}

func TestValidateEngineForwardEnv(t *testing.T) {
	env := []corev1.EnvVar{
		{Name: "LITERAL", Value: "x"},
		{Name: "SECRET_REF", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "s"}, Key: "k"}}},
		{Name: "OPENCODE_ENABLE_EXA", Value: "true"},
		{Name: "SSL_CERT_FILE", Value: "/x"},
		{Name: "LD_PRELOAD", Value: "/x"},
		{Name: "AWS_ACCESS_KEY_ID", Value: "x"},
		{Name: "HTTP_PROXY", Value: "http://p"},
		{Name: "HTTPS_PROXY", Value: "http://p"},
		{Name: "XDG_CONFIG_HOME", Value: "/x"},
		{Name: "OPENCODE_CONFIG_PATH", Value: "/x"},
		{Name: "OPENCODE_BIN", Value: "/x"},
	}
	cases := []struct {
		name    string
		forward []string
		wantErr string
	}{
		{"literal ok", []string{"LITERAL"}, ""},
		{"nil ok", nil, ""},
		{"missing name", []string{"NOPE"}, "not set"},
		{"secret-sourced rejected", []string{"SECRET_REF"}, "secretKeyRef"},
		{"ACH_ prefix reserved", []string{"ACH_TOKEN"}, "reserved"},
		{"HOME reserved", []string{"HOME"}, "reserved"},
		{"not reserved: OPENCODE_ENABLE_EXA", []string{"OPENCODE_ENABLE_EXA"}, ""},
		// Final corrected policy (ach-agent 3da2c6e/ceee54d, re-vendored
		// testdata/ach-workspace-config-v2.schema.json): generic cloud-credential, TLS,
		// loader, and proxy variables are explicitly ALLOWED via forwardEnv — an operator's
		// deliberate choice. Only names the runtime concretely owns stay reserved: HOME, the
		// five pinned OPENCODE_* bootstrap vars, and the ACH_/XDG_/OPENCODE_CONFIG prefixes.
		{"TLS: SSL_CERT_FILE allowed", []string{"SSL_CERT_FILE"}, ""},
		{"loader: LD_PRELOAD allowed", []string{"LD_PRELOAD"}, ""},
		{"cloud: AWS_ACCESS_KEY_ID allowed", []string{"AWS_ACCESS_KEY_ID"}, ""},
		{"proxy: HTTP_PROXY allowed", []string{"HTTP_PROXY"}, ""},
		{"proxy: HTTPS_PROXY allowed", []string{"HTTPS_PROXY"}, ""},
		{"prefix: XDG_ reserved", []string{"XDG_CONFIG_HOME"}, "reserved"},
		{"prefix: OPENCODE_CONFIG reserved", []string{"OPENCODE_CONFIG_PATH"}, "reserved"},
		{"pinned bootstrap: OPENCODE_BIN reserved", []string{"OPENCODE_BIN"}, "reserved"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			err := validateEngineForwardEnv(&v1.EngineSpec{ForwardEnv: c.forward}, env)
			if c.wantErr == "" {
				if err != nil {
					t.Fatalf("unexpected error: %v", err)
				}
				return
			}
			if err == nil || !strings.Contains(err.Error(), c.wantErr) {
				t.Fatalf("error = %v, want containing %q", err, c.wantErr)
			}
		})
	}
}

func TestResolveEngine(t *testing.T) {
	profile := &v1.EngineSpec{ForwardEnv: []string{"HTTPS_PROXY"}, StartupTimeoutSeconds: ptr(int64(600))}
	t.Run("nil agent inherits profile", func(t *testing.T) {
		if got := ResolveEngine(nil, profile); got != profile {
			t.Errorf("ResolveEngine(nil, p) = %+v, want profile", got)
		}
	})
	t.Run("nil profile returns agent", func(t *testing.T) {
		agent := &v1.EngineSpec{StartupTimeoutSeconds: ptr(int64(10))}
		if got := ResolveEngine(agent, nil); got != agent {
			t.Errorf("ResolveEngine(a, nil) = %+v, want agent", got)
		}
	})
	t.Run("agent denyTools replaces profile denyTools as a whole", func(t *testing.T) {
		p := &v1.EngineSpec{DenyTools: []string{"question"}}
		a := &v1.EngineSpec{DenyTools: []string{"webfetch"}}
		if got := ResolveEngine(a, p); !reflect.DeepEqual(got.DenyTools, []string{"webfetch"}) {
			t.Errorf("DenyTools = %v, want [webfetch]", got.DenyTools)
		}
	})
	t.Run("agent without denyTools inherits profile denyTools", func(t *testing.T) {
		p := &v1.EngineSpec{DenyTools: []string{"question"}}
		if got := ResolveEngine(&v1.EngineSpec{}, p); !reflect.DeepEqual(got.DenyTools, []string{"question"}) {
			t.Errorf("DenyTools = %v, want profile's", got.DenyTools)
		}
	})
	t.Run("nil engine on both sides", func(t *testing.T) {
		if got := ResolveEngine(nil, nil); got != nil {
			t.Errorf("ResolveEngine(nil, nil) = %+v, want nil", got)
		}
	})
	t.Run("agent startupTimeoutSeconds wins, forwardEnv inherits", func(t *testing.T) {
		agent := &v1.EngineSpec{StartupTimeoutSeconds: ptr(int64(10))}
		got := ResolveEngine(agent, profile)
		if got.StartupTimeoutSeconds == nil || *got.StartupTimeoutSeconds != 10 {
			t.Errorf("agent startupTimeoutSeconds must win: %+v", got)
		}
		if len(got.ForwardEnv) != 1 || got.ForwardEnv[0] != "HTTPS_PROXY" {
			t.Errorf("unset agent forwardEnv must inherit profile: %+v", got)
		}
	})
	t.Run("agent forwardEnv replaces atomically", func(t *testing.T) {
		agent := &v1.EngineSpec{ForwardEnv: []string{"NO_PROXY"}}
		got := ResolveEngine(agent, profile)
		if len(got.ForwardEnv) != 1 || got.ForwardEnv[0] != "NO_PROXY" {
			t.Errorf("forwardEnv must replace as a whole: %v", got.ForwardEnv)
		}
	})
	t.Run("agent compaction replaces atomically", func(t *testing.T) {
		agent := &v1.EngineSpec{Compaction: &v1.CompactionSpec{Auto: ptr(true)}}
		got := ResolveEngine(agent, profile)
		if got.Compaction == nil || got.Compaction.Auto == nil || !*got.Compaction.Auto {
			t.Errorf("agent compaction must win: %+v", got.Compaction)
		}
	})
}

// TestResolveLimits_FieldMerge: an agent may override one limit (e.g. maxConcurrentScripts,
// or just maxSteps, or everything EXCEPT maxSteps) without restating the rest — per-field
// merge, not an atomic whole-block replace. maxSteps is a pointer like every other field
// here (no longer the non-pointer exception), so it merges the same way.
func TestResolveLimits_FieldMerge(t *testing.T) {
	profile := &v1.LimitsSpec{MaxConcurrentInvocations: ptr(int64(8)), MaxSteps: ptr(int64(50))}
	agent := &v1.LimitsSpec{MaxConcurrentScripts: ptr(int64(2))}
	got := ResolveLimits(agent, profile)
	if got.MaxConcurrentScripts == nil || *got.MaxConcurrentScripts != 2 {
		t.Errorf("agent field must win: %+v", got)
	}
	if got.MaxConcurrentInvocations == nil || *got.MaxConcurrentInvocations != 8 {
		t.Errorf("unset agent field must inherit profile: %+v", got)
	}
	if got.MaxSteps == nil || *got.MaxSteps != 50 {
		t.Errorf("unset agent maxSteps must inherit profile: %+v", got)
	}
	agentOverridesSteps := &v1.LimitsSpec{MaxSteps: ptr(int64(10))}
	if got := ResolveLimits(agentOverridesSteps, profile); got.MaxSteps == nil || *got.MaxSteps != 10 {
		t.Errorf("agent maxSteps must win when set: %+v", got)
	}
	if got := ResolveLimits(nil, profile); got != profile {
		t.Errorf("nil agent must inherit profile")
	}
	if got := ResolveLimits(agent, nil); got != agent {
		t.Errorf("nil profile must return agent")
	}
}

func TestResolveEnv_AgentWinsByName(t *testing.T) {
	profile := []corev1.EnvVar{{Name: "PROFILE", Value: "p"}, {Name: "SHARED", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "profile-secret"}, Key: "value"}}}}
	agent := []corev1.EnvVar{{Name: "SHARED", Value: "agent"}, {Name: "AGENT", Value: "a"}}
	got := ResolveEnv(agent, profile)
	want := []corev1.EnvVar{{Name: "PROFILE", Value: "p"}, {Name: "SHARED", Value: "agent"}, {Name: "AGENT", Value: "a"}}
	if !slices.Equal(got, want) {
		t.Fatalf("ResolveEnv = %+v, want %+v", got, want)
	}
}

// TestRender_RejectsCollidingChannelSecretAliases migrates the legacy
// TestRender_RejectsCollidingChannelSecretAliases off the retired Render() root: two
// channel hooks whose generated aliases collide must fail Render closed, without leaking
// the underlying secret data into the error.
func TestRender_RejectsCollidingChannelSecretAliases(t *testing.T) {
	secret := func(name, secretName, key string) corev1.EnvVar {
		return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: secretName}, Key: key,
		}}}
	}
	profile, agent := minimalFixture()
	profile.Spec.Control.Env = []corev1.EnvVar{
		secret("TOKEN_SCRIPT_X", "handoff-credential", "handoff-key"),
		secret("X", "script-credential", "script-key"),
	}
	agent.Spec.Channels = []v1.ChannelSpec{
		{
			Name: "c", Type: "cron", Cron: &v1.CronSpec{Schedule: "* * * * *"},
			Handoff: &v1.HandoffSpec{PrepareSpec: v1.PrepareSpec{Script: "true", ForwardEnv: []string{"TOKEN_SCRIPT_X"}}, Destination: "handoff"},
		},
		{
			Name: "c-handoff-token", Type: "webhook-script", Webhook: &v1.WebhookSpec{Auth: v1.WebhookAuthSpec{Type: "hmac"}},
			Script: &v1.PrepareSpec{Script: "true", ForwardEnv: []string{"X"}},
		},
	}

	_, _, err := Render(profile, agent)
	if err == nil || !strings.Contains(err.Error(), "duplicate generated channel secret env alias") {
		t.Fatalf("Render error = %v", err)
	}
	for _, secretData := range []string{"handoff-credential", "handoff-key", "script-credential", "script-key"} {
		if strings.Contains(err.Error(), secretData) {
			t.Fatalf("Render error leaked secret reference data: %v", err)
		}
	}
}

// TestRender_SecretRefNeverLeaks migrates the legacy TestRender_FullGolden secret-leak
// assertions off Render()/AgentConfig: a webhook's secretRef must never reach the rendered
// WSConfig, and the wire shape must carry only the operator-generated env alias.
func TestRender_SecretRefNeverLeaks(t *testing.T) {
	profile, agent := minimalFixture()
	agent.Spec.Channels = []v1.ChannelSpec{{
		Name: "gitlab-mr-review", Type: "webhook", Source: "gitlab",
		Webhook: &v1.WebhookSpec{Auth: v1.WebhookAuthSpec{Type: "gitlab_token", SecretRef: &v1.SecretKeyRef{Name: "gitlab-webhook", Key: "secret"}}},
	}}
	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	raw, err := json.Marshal(cfg)
	if err != nil {
		t.Fatalf("marshal WSConfig: %v", err)
	}
	if strings.Contains(string(raw), "secretRef") || strings.Contains(string(raw), "secretPath") {
		t.Fatalf("secretRef/secretPath leaked into rendered config: %s", raw)
	}
	if len(cfg.Channels) != 1 || cfg.Channels[0].Webhook == nil || cfg.Channels[0].Webhook.Auth.Env != "ACH_SECRET_GITLAB_MR_REVIEW_WEBHOOK" {
		t.Fatalf("webhook.auth = %+v, want env ACH_SECRET_GITLAB_MR_REVIEW_WEBHOOK", cfg.Channels[0].Webhook)
	}
}

// TestRender_MemoryAchMemoryAuthRendered closes a coverage gap no existing Render/
// workspace-v1 test filled: memory.achMemory bearer auth wired end to end through the real
// Render entrypoint (TestRenderMemory_AchMemoryBlock below only unit-tests the shared
// renderMemory helper directly).
func TestRender_MemoryAchMemoryAuthRendered(t *testing.T) {
	profile, agent := minimalFixture()
	agent.Spec.Memory = &v1.MemorySpec{Type: "ach-memory", AchMemory: &v1.AchMemorySpec{
		Endpoint: "http://m/mcp/", Project: "team-reviewer",
		Auth: &v1.AchMemoryAuthSpec{Type: memoryAuthTypeBearer, Header: "x-litellm-api-key", SecretRef: &v1.SecretKeyRef{Name: "am", Key: "token"}},
	}}
	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if cfg.Memory == nil || cfg.Memory.AchMemory == nil || cfg.Memory.AchMemory.Auth == nil {
		t.Fatalf("Memory = %+v, want a rendered ach-memory block", cfg.Memory)
	}
	auth := cfg.Memory.AchMemory.Auth
	if auth.Type != "bearer" || auth.Env != "ACH_SECRET_MEMORY_AUTH" || auth.Header != "x-litellm-api-key" {
		t.Errorf("auth = %+v, want {bearer, ACH_SECRET_MEMORY_AUTH, x-litellm-api-key}", auth)
	}
	if keys := ReferencedSecrets(profile, agent)["am"]; len(keys) != 1 || keys[0] != "token" {
		t.Errorf("ReferencedSecrets missing memory secret: %v", ReferencedSecrets(profile, agent))
	}
}

func TestRender_PromptRendered(t *testing.T) {
	profile, agent := minimalFixture()
	agent.Spec.Prompt = &v1.AgentPromptSpec{System: v1.PromptSystemSpec{Type: "text", Text: "You are a reviewer."}, Compose: "append"}
	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if cfg.Prompt == nil || cfg.Prompt.System.Type != "text" || cfg.Prompt.System.Text != "You are a reviewer." || cfg.Prompt.Compose != "append" {
		t.Fatalf("Prompt = %+v, want rendered text prompt", cfg.Prompt)
	}
}

// TestRender_PromptVariants restores task-5-review.md Important #2: prompt.system's file
// and ach branches (including the ach arm's optional AchFile subpath) are still live
// renderPrompt/Render code paths that lost their schema coverage with renderMatrix's
// removal; this exercises both through the real Render entrypoint.
func TestRender_PromptVariants(t *testing.T) {
	cases := []struct {
		name   string
		prompt v1.AgentPromptSpec
		want   PromptSystemBlock
	}{
		{
			name:   "file",
			prompt: v1.AgentPromptSpec{System: v1.PromptSystemSpec{Type: "file", File: "prompts/x/y.md"}, Compose: "replace"},
			want:   PromptSystemBlock{Type: "file", File: "prompts/x/y.md"},
		},
		{
			name:   "ach",
			prompt: v1.AgentPromptSpec{System: v1.PromptSystemSpec{Type: "ach", Ach: "persona", AchFile: "main.md"}, Compose: "append"},
			want:   PromptSystemBlock{Type: "ach", Ach: "persona", File: "main.md"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			profile, agent := minimalFixture()
			agent.Spec.Prompt = &c.prompt
			cfg, _, err := Render(profile, agent)
			if err != nil {
				t.Fatalf("Render: %v", err)
			}
			if cfg.Prompt == nil || cfg.Prompt.System != c.want {
				t.Fatalf("Prompt.System = %+v, want %+v", cfg.Prompt, c.want)
			}
			if cfg.Prompt.Compose != c.prompt.Compose {
				t.Errorf("Prompt.Compose = %q, want unchanged %q", cfg.Prompt.Compose, c.prompt.Compose)
			}
		})
	}
}

// TestRender_WebhookScriptAuthScriptAndForwardEnv restores task-5-review.md Important #3:
// the deleted TestWebhookScript_RendersAuthScriptAndForwardEnv's successful-case assertions
// (webhook auth alias, rendered script literal/secret env, generated secret alias count),
// adapted from Render()/AgentConfig onto Render/minimalFixture. Unlike the retired
// fixture, forwardEnv only selects names actually present in the merged env — the old
// "MISSING" name that silently vanished from the rendered script is deliberately not
// reproduced here; TestRenderScriptV1_MissingForwardEnvNameErrors (workspacev1_test.go)
// already covers that rejection path.
func TestRender_WebhookScriptAuthScriptAndForwardEnv(t *testing.T) {
	profile, agent := minimalFixture()
	profile.Spec.Control.Env = []corev1.EnvVar{{Name: "GITLAB_BASE_URL", Value: "https://git.example.com"}}
	agent.Spec.Env = []corev1.EnvVar{{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: "gitlab-api"}, Key: "token",
	}}}}
	agent.Spec.Channels = []v1.ChannelSpec{{
		Name: "gitlab-register", Type: "webhook-script", Source: "gitlab",
		Webhook: &v1.WebhookSpec{
			Auth:         v1.WebhookAuthSpec{Type: "gitlab_token", SecretRef: &v1.SecretKeyRef{Name: "system-hook", Key: "secret"}},
			GitlabEvents: []string{"project_create", "push", "merge_request"},
		},
		Script: &v1.PrepareSpec{Script: "payload=$(cat)", ForwardEnv: []string{"GITLAB_BASE_URL", "GITLAB_TOKEN"}},
	}}

	cfg, _, err := Render(profile, agent)
	if err != nil {
		t.Fatalf("Render: %v", err)
	}
	if len(cfg.Channels) != 1 {
		t.Fatalf("channels = %+v, want exactly 1", cfg.Channels)
	}
	got := cfg.Channels[0]
	if got.Webhook == nil || got.Webhook.Auth.Env != "ACH_SECRET_GITLAB_REGISTER_WEBHOOK_SCRIPT" {
		t.Fatalf("webhook auth alias = %+v, want env ACH_SECRET_GITLAB_REGISTER_WEBHOOK_SCRIPT", got.Webhook)
	}
	if got.Script == nil || got.Script.Script != "payload=$(cat)" || got.Script.Env["GITLAB_BASE_URL"] != "https://git.example.com" {
		t.Fatalf("script did not render: %+v", got.Script)
	}
	if got.Script.SecretEnv["GITLAB_TOKEN"].Env != "ACH_SECRET_GITLAB_REGISTER_SCRIPT_GITLAB_TOKEN" {
		t.Fatalf("script secret alias = %#v, want ACH_SECRET_GITLAB_REGISTER_SCRIPT_GITLAB_TOKEN", got.Script.SecretEnv)
	}
	refs := ChannelSecretEnv(profile, agent)
	if len(refs) != 2 {
		t.Fatalf("webhook-script auth + script refs = %+v, want 2", refs)
	}
}

func TestHandoff_SecretAliasesDoNotCollapseEnvNameCase(t *testing.T) {
	secret := func(name, secret string) corev1.EnvVar {
		return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: secret}, Key: "key"}}}
	}
	p := v1.AgentProfile{Spec: v1.AgentProfileSpec{Control: v1.ControlSpec{Env: []corev1.EnvVar{secret("token", "lower"), secret("TOKEN", "upper")}}}}
	a := v1.Agent{Spec: v1.AgentSpec{Channels: []v1.ChannelSpec{{
		Name: "c", Type: "cron", Cron: &v1.CronSpec{Schedule: "* * * * *"},
		Handoff: &v1.HandoffSpec{PrepareSpec: v1.PrepareSpec{Script: "true", ForwardEnv: []string{"token", "TOKEN"}}, Destination: "handoff"},
	}}}}
	refs := ChannelSecretEnv(p, a)
	if len(refs) != 2 || refs[0].EnvName == refs[1].EnvName {
		t.Fatalf("case-distinct env names collapsed to the same alias: %+v", refs)
	}
}

// TestResolvePersistence_PreservesInheritedFieldOnPartialOverride is the Important review
// finding 3 regression: `workspace.persistence: {enabled: false}` on the agent must not lose
// the profile's retentionDays (the wholesale sub-block swap this replaces did exactly that).
func TestResolvePersistence_PreservesInheritedFieldOnPartialOverride(t *testing.T) {
	profile := &v1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))}
	agent := &v1.WorkspacePersistenceSpec{Enabled: ptr(false)}
	got := ResolvePersistence(agent, profile)
	if got.Enabled == nil || *got.Enabled != false {
		t.Errorf("enabled = %v, want agent's false", got.Enabled)
	}
	if got.RetentionDays == nil || *got.RetentionDays != 90 {
		t.Errorf("retentionDays = %v, want inherited 90 (not lost by the partial override)", got.RetentionDays)
	}
}

// TestResolveWorkspace_SessionPartialOverridePreservesPersistence is the companion case one
// level deeper: `workspace.session: {idleTimeoutSeconds: 0}` must not lose
// session.persistence (previously an atomic sub-block swap at the WorkspaceSpec level).
func TestResolveWorkspace_SessionPartialOverridePreservesPersistence(t *testing.T) {
	profile := &v1.WorkspaceSpec{
		IdleTimeoutSeconds: ptr(int64(600)), ShutdownTimeoutSeconds: ptr(int64(300)), MaxConcurrentSessions: ptr(int64(1)),
		Persistence: &v1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		Session: &v1.WorkspaceSessionSpec{
			IdleTimeoutSeconds: ptr(int64(300)),
			Persistence:        &v1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		},
	}
	agent := &v1.WorkspaceSpec{Session: &v1.WorkspaceSessionSpec{IdleTimeoutSeconds: ptr(int64(0))}}
	got := ResolveWorkspace(agent, profile)
	if got.Session.IdleTimeoutSeconds == nil || *got.Session.IdleTimeoutSeconds != 0 {
		t.Errorf("session.idleTimeoutSeconds = %v, want agent's explicit 0", got.Session.IdleTimeoutSeconds)
	}
	if got.Session.Persistence == nil || got.Session.Persistence.RetentionDays == nil || *got.Session.Persistence.RetentionDays != 90 {
		t.Errorf("session.persistence = %+v, want inherited from profile", got.Session.Persistence)
	}
	if _, err := RenderWorkspaceV1(got); err != nil {
		t.Errorf("resolved workspace must still satisfy RenderWorkspaceV1 after a partial override: %v", err)
	}
}

// TestResolveArtifacts_PreservesEnabledOnPartialOverride mirrors the persistence case for
// artifacts: an agent overriding only maxArtifactBytes must not lose the profile's enabled.
func TestResolveArtifacts_PreservesEnabledOnPartialOverride(t *testing.T) {
	profile := &v1.ArtifactsSpec{Enabled: ptr(true), MaxArtifactBytes: ptr(int64(1000)), RetentionDays: ptr(int64(90))}
	agent := &v1.ArtifactsSpec{MaxArtifactBytes: ptr(int64(2000))}
	got := ResolveArtifacts(agent, profile)
	if got.Enabled == nil || !*got.Enabled {
		t.Errorf("enabled = %v, want inherited true", got.Enabled)
	}
	if got.MaxArtifactBytes == nil || *got.MaxArtifactBytes != 2000 {
		t.Errorf("maxArtifactBytes = %v, want agent's 2000", got.MaxArtifactBytes)
	}
}

// TestDecodeParams_OrdinaryJSONDecoding verifies decodeParams uses plain encoding/json object
// decoding: a duplicate key resolves to its last value (ordinary json.Unmarshal semantics —
// the handwritten duplicate-key framework was removed, contract §5 no longer claims that
// guarantee), well-formed params decode normally, and nil/empty raw bytes decode to nil.
func TestDecodeParams_OrdinaryJSONDecoding(t *testing.T) {
	got, err := decodeParams(rawJSON(`{"temperature":1,"temperature":2}`))
	if err != nil {
		t.Fatalf("duplicate key must not be rejected: %v", err)
	}
	if got["temperature"] != float64(2) {
		t.Fatalf("duplicate key must resolve to its last value, got %v", got["temperature"])
	}
	if _, err := decodeParams(rawJSON(`{"temperature":1}`)); err != nil {
		t.Fatalf("valid params must not be rejected: %v", err)
	}
	if got, err := decodeParams(nil); err != nil || got != nil {
		t.Fatalf("nil params: got %v, %v", got, err)
	}
}

// TestReservedForwardEnvDenylist_MatchesVendoredSchema cross-checks the hand-maintained Go
// denylist/prefix list against the final corrected vendored schema's engine.env
// propertyNames constraint (testdata/ach-workspace-config-v2.schema.json, ach-agent commits
// 3da2c6e/ceee54d), so the two cannot silently drift apart.
func TestReservedForwardEnvDenylist_MatchesVendoredSchema(t *testing.T) {
	raw, err := os.ReadFile(vendoredWorkspaceSchema)
	if err != nil {
		t.Fatalf("read vendored schema: %v", err)
	}
	var doc struct {
		Defs map[string]struct {
			Properties struct {
				Env struct {
					PropertyNames struct {
						AllOf []struct {
							Not struct {
								Enum    []string `json:"enum"`
								Pattern string   `json:"pattern"`
							} `json:"not"`
						} `json:"allOf"`
					} `json:"propertyNames"`
				} `json:"env"`
			} `json:"properties"`
		} `json:"$defs"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal schema: %v", err)
	}
	eb, ok := doc.Defs["EngineBlock"]
	if !ok {
		t.Fatal("schema has no $defs.EngineBlock")
	}
	var schemaNames []string
	var schemaPrefixPattern string
	for _, clause := range eb.Properties.Env.PropertyNames.AllOf {
		if len(clause.Not.Enum) > 0 {
			schemaNames = clause.Not.Enum
		}
		if clause.Not.Pattern != "" {
			schemaPrefixPattern = clause.Not.Pattern
		}
	}
	if len(schemaNames) == 0 || schemaPrefixPattern == "" {
		t.Fatal("could not locate EngineBlock.env's denylist enum/prefix pattern in the schema")
	}
	if len(schemaNames) != len(reservedForwardEnvNames) {
		t.Fatalf("reservedForwardEnvNames has %d entries, vendored schema enum has %d — re-sync the Go list", len(reservedForwardEnvNames), len(schemaNames))
	}
	for _, name := range schemaNames {
		if _, ok := reservedForwardEnvNames[name]; !ok {
			t.Errorf("schema reserves %q but reservedForwardEnvNames does not", name)
		}
	}
	// The schema's prefix pattern is ^(?:ACH_|XDG_|OPENCODE_CONFIG) — confirm our prefix
	// list matches its alternatives exactly (parsed, not hardcoded, so a schema-side prefix
	// addition fails this test instead of silently going unenforced).
	inner := strings.TrimSuffix(strings.TrimPrefix(schemaPrefixPattern, "^(?:"), ")")
	schemaPrefixes := strings.Split(inner, "|")
	if len(schemaPrefixes) != len(reservedForwardEnvPrefixes) {
		t.Fatalf("reservedForwardEnvPrefixes has %d entries, schema pattern has %d — re-sync", len(reservedForwardEnvPrefixes), len(schemaPrefixes))
	}
	for _, p := range schemaPrefixes {
		if !slices.Contains(reservedForwardEnvPrefixes, p) {
			t.Errorf("schema reserves prefix %q but reservedForwardEnvPrefixes does not", p)
		}
	}
}
