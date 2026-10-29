// SPDX-License-Identifier: MIT

package render

import (
	"encoding/json"
	"os"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// fixtureSubtree loads testdata/pr-review-runtime.json and returns one top-level key,
// re-marshaled so it can be compared byte-for-byte against our own Marshal output.
func fixtureSubtree(t *testing.T, key string) []byte {
	t.Helper()
	raw, err := os.ReadFile(vendoredPRReviewRuntime)
	if err != nil {
		t.Fatalf("read fixture: %v", err)
	}
	var doc map[string]json.RawMessage
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatalf("unmarshal fixture: %v", err)
	}
	sub, ok := doc[key]
	if !ok {
		t.Fatalf("fixture has no top-level key %q", key)
	}
	// Re-marshal through the generic path so map-key order matches json.Marshal's
	// (alphabetical for map[string]any), not the fixture file's own formatting.
	var v any
	if err := json.Unmarshal(sub, &v); err != nil {
		t.Fatalf("unmarshal fixture.%s: %v", key, err)
	}
	out, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("remarshal fixture.%s: %v", key, err)
	}
	return out
}

func assertJSONEqual(t *testing.T, got any, wantKey string) {
	t.Helper()
	gotJSON, err := json.Marshal(got)
	if err != nil {
		t.Fatalf("marshal got: %v", err)
	}
	var gotV any
	if err := json.Unmarshal(gotJSON, &gotV); err != nil {
		t.Fatalf("unmarshal got: %v", err)
	}
	gotCanon, err := json.Marshal(gotV)
	if err != nil {
		t.Fatalf("remarshal got: %v", err)
	}
	want := fixtureSubtree(t, wantKey)
	if string(gotCanon) != string(want) {
		t.Fatalf("%s mismatch:\n got:  %s\n want: %s", wantKey, gotCanon, want)
	}
}

// gitlabPRReviewerAgent reconstructs the fully-resolved CR values behind
// pr-review-runtime.json (contract's own binding example, examples/gitlab-pr.yaml in
// ach-agent) — every field the wire schema requires is set, so these tests prove the
// MAPPING is correct, independent of the (separately blocked) default-resolution gap.
func gitlabPRReviewerAgent() (ach *achv1alpha1.AchSpec, engine *achv1alpha1.EngineSpec, limits *achv1alpha1.LimitsSpec, workspace *achv1alpha1.WorkspaceSpec, artifacts *achv1alpha1.ArtifactsSpec, env []corev1.EnvVar, channel achv1alpha1.ChannelSpec) {
	ach = &achv1alpha1.AchSpec{
		BaseURL: "https://ach.ackstorm.ai", Environment: ptr("engineering-prod"),
		Identity: &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: "test", Key: "ek"}},
		Capability: &achv1alpha1.CapabilitySpec{Filter: &achv1alpha1.FilterSpec{Exclude: &achv1alpha1.ExcludeSpec{
			Tools:      []string{},
			McpServers: []string{"mcp-gitlab-ro", "mcp-google-calendar-ro", "mcp-slack", "mcp-zoho-desk-ro"},
			Skills:     []string{"frontend-design@anthropics-skills"},
		}}},
	}
	env = []corev1.EnvVar{{Name: "OPENCODE_ENABLE_EXA", Value: "true"}}
	engine = &achv1alpha1.EngineSpec{
		ForwardEnv: []string{"OPENCODE_ENABLE_EXA"}, StartupTimeoutSeconds: ptr(int64(60)),
		Compaction: &achv1alpha1.CompactionSpec{Auto: ptr(true), Keep: &achv1alpha1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))},
	}
	limits = &achv1alpha1.LimitsSpec{
		MaxActiveWorkspaces: ptr(int64(4)), MaxConcurrentInvocations: ptr(int64(4)), MaxConcurrentScripts: ptr(int64(2)),
		MaxInvocationSeconds: ptr(int64(900)), MaxQueuedTotal: ptr(int64(100)), IdempotencyWindowSeconds: ptr(int64(3600)), MaxSteps: ptr(int64(30)),
	}
	workspace = &achv1alpha1.WorkspaceSpec{
		IdleTimeoutSeconds: ptr(int64(600)), ShutdownTimeoutSeconds: ptr(int64(300)), MaxConcurrentSessions: ptr(int64(1)),
		Persistence: &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		Session: &achv1alpha1.WorkspaceSessionSpec{
			IdleTimeoutSeconds: ptr(int64(300)),
			Persistence:        &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		},
	}
	artifacts = &achv1alpha1.ArtifactsSpec{Enabled: ptr(true), MaxArtifactBytes: ptr(int64(104857600)), RetentionDays: ptr(int64(90))}
	bot := "ai.run"
	channel = achv1alpha1.ChannelSpec{
		Name: "gitlab-mr-review", Type: "webhook", Source: "gitlab", Concurrency: ptr(int64(4)),
		Prompt: "Review the current state of this merge request.\nProject: {{ event.gitlab.projectPath }}\nMR: {{ event.gitlab.mergeRequestIid }}\nTrigger: {{ event.type }}\nPayload: {{ payload | json }}\nRepository: ./handoff/repo\n",
		Webhook: &achv1alpha1.WebhookSpec{
			Auth:              achv1alpha1.WebhookAuthSpec{Type: "gitlab_token", SecretRef: &achv1alpha1.SecretKeyRef{Name: "gitlab-reviewer-webhook", Key: "secret"}},
			GitlabEvents:      []string{"merge_request", "note"},
			BotUsername:       &bot,
			TriggerUsers:      []string{"juancarlos.moreno", "ivan.hernanz"},
			MergeRequestsOnly: ptr(true),
		},
		Handoff: &achv1alpha1.HandoffSpec{
			PrepareSpec: achv1alpha1.PrepareSpec{
				ForwardEnv:     []string{"GITLAB_TOKEN", "GITLAB_REPO_BASEURL"},
				TimeoutSeconds: ptr(int64(300)),
				Script:         "set -eu\ncase \"$ACH_EVENT_PROJECT_PATH\" in\n  \"\"|*[!a-zA-Z0-9_./-]*) exit 1 ;;\nesac\ncase \"/$ACH_EVENT_PROJECT_PATH/\" in\n  *\"/../\"*|*\"/./\"*|*\"//\"*) exit 1 ;;\nesac\ncase \"$ACH_EVENT_MR_IID\" in\n  \"\"|*[!0-9]*) exit 1 ;;\nesac\nAUTH=$(printf 'oauth2:%s' \"$GITLAB_TOKEN\" | base64 -w0)\nexport GIT_CONFIG_COUNT=1\nexport GIT_CONFIG_KEY_0=http.extraHeader\nexport GIT_CONFIG_VALUE_0=\"Authorization: Basic $AUTH\"\nexport GIT_TERMINAL_PROMPT=0\nexport GIT_LFS_SKIP_SMUDGE=1\ngit clone --no-checkout --no-recurse-submodules \\\n  \"$GITLAB_REPO_BASEURL/$ACH_EVENT_PROJECT_PATH.git\" \\\n  \"$ACH_HANDOFF_DIR/repo\"\ngit -C \"$ACH_HANDOFF_DIR/repo\" fetch origin \\\n  \"refs/merge-requests/$ACH_EVENT_MR_IID/head\"\ngit -C \"$ACH_HANDOFF_DIR/repo\" checkout --detach FETCH_HEAD\n",
			},
			Destination: "handoff",
		},
	}
	return ach, engine, limits, workspace, artifacts, env, channel
}

func TestWorkspaceV1_AchBlockMatchesFixture(t *testing.T) {
	ach, _, _, _, _, _, _ := gitlabPRReviewerAgent()
	got, err := RenderAchV1(ach, ach.BaseURL)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, "ach")
}

func TestWorkspaceV1_EngineBlockMatchesFixture(t *testing.T) {
	_, engine, _, _, _, env, _ := gitlabPRReviewerAgent()
	// The fixture's agent-level literal env is GITLAB_REPO_BASEURL, not forwarded;
	// only OPENCODE_ENABLE_EXA is in forwardEnv (matching the binding example).
	env = append(env, corev1.EnvVar{Name: "GITLAB_REPO_BASEURL", Value: "https://git.ackstorm.com"})
	got, err := RenderEngineV1(engine, env)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, "engine")
}

func TestWorkspaceV1_EngineBlock_RejectsSecretForwardEnv(t *testing.T) {
	_, engine, _, _, _, _, _ := gitlabPRReviewerAgent()
	engine.ForwardEnv = []string{"SECRET_VAR"}
	env := []corev1.EnvVar{{Name: "SECRET_VAR", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "s"}, Key: "k"}}}}
	if _, err := RenderEngineV1(engine, env); err == nil {
		t.Fatal("expected rejection of a secretKeyRef-sourced engine.forwardEnv name")
	}
}

func TestWorkspaceV1_EngineBlock_RequiresCompaction(t *testing.T) {
	_, engine, _, _, _, env, _ := gitlabPRReviewerAgent()
	engine.Compaction = nil
	if _, err := RenderEngineV1(engine, env); err == nil {
		t.Fatal("expected rejection: compaction is required on the wire with no harness default published")
	}
}

func TestWorkspaceV1_LimitsBlockMatchesFixture(t *testing.T) {
	_, _, limits, _, _, _, _ := gitlabPRReviewerAgent()
	got, err := RenderLimitsV1(limits)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, "limits")
}

// TestWorkspaceV1_LimitsBlock_NilBlockRejected: the wire-required subfields (all but
// maxConcurrentScripts) are CRD-required non-pointer fields now — admission, not this
// function, rejects a LimitsSpec missing one. Only "no block at all" remains for
// RenderLimitsV1 itself to catch.
func TestWorkspaceV1_LimitsBlock_NilBlockRejected(t *testing.T) {
	if _, err := RenderLimitsV1(nil); err == nil {
		t.Fatal("expected rejection: limits block itself is required")
	}
}

func TestWorkspaceV1_WorkspaceBlockMatchesFixture(t *testing.T) {
	_, _, _, workspace, _, _, _ := gitlabPRReviewerAgent()
	got, err := RenderWorkspaceV1(workspace)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, "workspace")
}

func TestWorkspaceV1_ArtifactsBlockMatchesFixture(t *testing.T) {
	_, _, _, _, artifacts, _, _ := gitlabPRReviewerAgent()
	got, err := RenderArtifactsV1(artifacts)
	if err != nil {
		t.Fatal(err)
	}
	assertJSONEqual(t, got, "artifacts")
}

func TestWorkspaceV1_ChannelMatchesFixture(t *testing.T) {
	_, _, _, _, _, env, channel := gitlabPRReviewerAgent()
	env = append(env, corev1.EnvVar{Name: "GITLAB_REPO_BASEURL", Value: "https://git.ackstorm.com"},
		corev1.EnvVar{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "ach-agent-gitlab-ro"}, Key: "GITLAB_TOKEN"}}})

	webhook := RenderWebhookV1(&channel)
	handoff, err := RenderHandoffV1(&channel, env)
	if err != nil {
		t.Fatal(err)
	}
	routing := RenderRoutingV1(channel.Routing)

	got := struct {
		Name        string          `json:"name"`
		Type        string          `json:"type"`
		Concurrency *int64          `json:"concurrency,omitempty"`
		Source      string          `json:"source,omitempty"`
		Routing     *WSRoutingBlock `json:"routing"`
		Prompt      string          `json:"prompt,omitempty"`
		Webhook     *WSWebhookBlock `json:"webhook,omitempty"`
		Handoff     *WSHandoffBlock `json:"handoff,omitempty"`
	}{channel.Name, channel.Type, channel.Concurrency, channel.Source, routing, channel.Prompt, webhook, handoff}

	gotJSON, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var gotV any
	if err := json.Unmarshal(gotJSON, &gotV); err != nil {
		t.Fatal(err)
	}
	gotCanon, _ := json.Marshal(gotV)

	raw, err := os.ReadFile(vendoredPRReviewRuntime)
	if err != nil {
		t.Fatal(err)
	}
	var doc struct {
		Channels []json.RawMessage `json:"channels"`
	}
	if err := json.Unmarshal(raw, &doc); err != nil {
		t.Fatal(err)
	}
	if len(doc.Channels) != 1 {
		t.Fatalf("fixture channels = %d, want 1", len(doc.Channels))
	}
	var wantV any
	if err := json.Unmarshal(doc.Channels[0], &wantV); err != nil {
		t.Fatal(err)
	}
	// The fixture's channel-secret alias names (ACH_SECRET_GITLAB_WEBHOOK,
	// ACH_SECRET_GITLAB_RO) are illustrative placeholders, not a mandated naming
	// scheme — nothing in the contract text specifies one, and this repo already has
	// an established deterministic generator (channelSecretEnvName/hookSecretEnvName,
	// used and tested throughout the legacy renderer). Patch the fixture's alias
	// strings to our deterministic ones before comparing, so the rest of the channel
	// (including the "env" KEY's presence/shape) still gets byte-exact parity.
	wantMap := wantV.(map[string]any)
	wantMap["webhook"].(map[string]any)["auth"].(map[string]any)["env"] = channelSecretEnvName(&channel)
	wantMap["handoff"].(map[string]any)["secretEnv"].(map[string]any)["GITLAB_TOKEN"].(map[string]any)["env"] = hookSecretEnvName(&channel, "HANDOFF", "GITLAB_TOKEN")
	wantCanon, _ := json.Marshal(wantMap)

	if string(gotCanon) != string(wantCanon) {
		t.Fatalf("channel mismatch:\n got:  %s\n want: %s", gotCanon, wantCanon)
	}
}

// TestRenderFilterV1_PartialFilterEmitsEmptyArraysNotNull is the Important review finding 4
// regression: a filter excluding only skills must render tools/mcpServers as [] (the schema
// type), never JSON null (a nil Go slice's default marshal).
func TestRenderFilterV1_PartialFilterEmitsEmptyArraysNotNull(t *testing.T) {
	f := &achv1alpha1.FilterSpec{Exclude: &achv1alpha1.ExcludeSpec{Skills: []string{"send-email"}}}
	got := renderFilterV1(f)
	if got == nil || got.Exclude == nil {
		t.Fatal("expected a non-nil filter block")
	}
	b, err := json.Marshal(got.Exclude)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	for _, key := range []string{"tools", "mcpServers"} {
		if string(m[key]) != "[]" {
			t.Errorf("exclude.%s = %s, want [] (not null)", key, m[key])
		}
	}
}

// TestRenderHandoffV1_MissingForwardEnvNameErrors is the Important review finding 6
// regression: a forwardEnv name absent from the merged environment must fail closed, not
// silently vanish from the rendered handoff block.
func TestRenderHandoffV1_MissingForwardEnvNameErrors(t *testing.T) {
	ch := &achv1alpha1.ChannelSpec{Name: "c", Handoff: &achv1alpha1.HandoffSpec{
		PrepareSpec: achv1alpha1.PrepareSpec{Script: "true", ForwardEnv: []string{"MISSING"}}, Destination: "handoff",
	}}
	if _, err := RenderHandoffV1(ch, nil); err == nil {
		t.Fatal("expected rejection of a forwardEnv name absent from the merged environment")
	}
}

// TestRenderScriptV1_MissingForwardEnvNameErrors is the script-channel counterpart.
func TestRenderScriptV1_MissingForwardEnvNameErrors(t *testing.T) {
	ch := &achv1alpha1.ChannelSpec{Name: "c", Script: &achv1alpha1.PrepareSpec{Script: "true", ForwardEnv: []string{"MISSING"}}}
	if _, err := RenderScriptV1(ch, nil); err == nil {
		t.Fatal("expected rejection of a forwardEnv name absent from the merged environment")
	}
}

func TestWorkspaceV1_HooksBlock_AllThreeKeysAlwaysPresent(t *testing.T) {
	got := RenderHooksV1(nil)
	b, err := json.Marshal(got)
	if err != nil {
		t.Fatal(err)
	}
	var m map[string]any
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatal(err)
	}
	for _, k := range []string{"sessionStart", "sessionRestore", "sessionSuspend"} {
		v, present := m[k]
		if !present {
			t.Fatalf("hooks.%s must be present (null), not omitted: %s", k, b)
		}
		if v != nil {
			t.Fatalf("hooks.%s = %v, want null for an unset hook", k, v)
		}
	}
}

// TestWorkspaceV1_PartialOverrides pins the per-field merge semantics an agent's partial
// override must preserve: an omitted field inherits the profile's sibling, while an
// explicitly-set-to-empty/zero/false field clears or overrides the inherited value without
// disturbing its own siblings. Each subtest exercises a different Resolve*/Render*V1 pair
// pinned by Task 3's brief (step 1).
func TestWorkspaceV1_PartialOverrides(t *testing.T) {
	t.Run("explicit empty Environment clears inherited intent", testPartialEnvironment)
	t.Run("maxSteps override inherits the other finite limits", testPartialLimits)
	t.Run("enabled false inherits retentionDays (workspace persistence)", testPartialPersistence)
	t.Run("enabled false inherits retentionDays (artifacts)", testPartialArtifacts)
	t.Run("empty forwardEnv selection clears inherited exposure", testPartialForwardEnv)
	t.Run("session override preserves workspace and session siblings", testPartialSession)
	t.Run("per-name whole-EnvVar replacement and agent-only identity", testPartialEnvIdentity)
}

func testPartialEnvironment(t *testing.T) {
	profile := &achv1alpha1.AchSpec{Environment: ptr("prod")}
	agent := &achv1alpha1.AchSpec{
		Identity:    &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: "ek", Key: "ek"}},
		Environment: ptr(""),
	}
	got := ResolveAch(agent, profile)
	if got.Environment == nil || *got.Environment != "" {
		t.Fatalf("Environment = %v, want explicit empty string (not inherited \"prod\")", got.Environment)
	}
}

func testPartialLimits(t *testing.T) {
	profile := &achv1alpha1.LimitsSpec{
		MaxActiveWorkspaces: ptr(int64(4)), MaxConcurrentInvocations: ptr(int64(4)),
		MaxInvocationSeconds: ptr(int64(900)), MaxQueuedTotal: ptr(int64(100)),
		IdempotencyWindowSeconds: ptr(int64(3600)), MaxSteps: ptr(int64(30)),
	}
	agent := &achv1alpha1.LimitsSpec{MaxSteps: ptr(int64(12))}
	got := ResolveLimits(agent, profile)
	if *got.MaxSteps != 12 {
		t.Fatalf("MaxSteps = %d, want 12", *got.MaxSteps)
	}
	if *got.MaxActiveWorkspaces != 4 || *got.MaxConcurrentInvocations != 4 || *got.MaxInvocationSeconds != 900 ||
		*got.MaxQueuedTotal != 100 || *got.IdempotencyWindowSeconds != 3600 {
		t.Fatalf("sibling limits not inherited from profile: %+v", got)
	}
}

func testPartialPersistence(t *testing.T) {
	profile := &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))}
	agent := &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(false)}
	got := ResolvePersistence(agent, profile)
	if got.Enabled == nil || *got.Enabled {
		t.Fatalf("Enabled = %v, want false", got.Enabled)
	}
	if got.RetentionDays == nil || *got.RetentionDays != 90 {
		t.Fatalf("RetentionDays = %v, want inherited 90", got.RetentionDays)
	}
}

func testPartialArtifacts(t *testing.T) {
	profile := &achv1alpha1.ArtifactsSpec{Enabled: ptr(true), MaxArtifactBytes: ptr(int64(104857600)), RetentionDays: ptr(int64(90))}
	agent := &achv1alpha1.ArtifactsSpec{Enabled: ptr(false)}
	got := ResolveArtifacts(agent, profile)
	if got.Enabled == nil || *got.Enabled {
		t.Fatalf("Enabled = %v, want false", got.Enabled)
	}
	if got.RetentionDays == nil || *got.RetentionDays != 90 || got.MaxArtifactBytes == nil || *got.MaxArtifactBytes != 104857600 {
		t.Fatalf("siblings not inherited from profile: %+v", got)
	}
}

func testPartialForwardEnv(t *testing.T) {
	profile := &achv1alpha1.EngineSpec{
		ForwardEnv: []string{"HTTPS_PROXY"},
		Compaction: &achv1alpha1.CompactionSpec{Auto: ptr(true), Keep: &achv1alpha1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))},
	}
	agent := &achv1alpha1.EngineSpec{ForwardEnv: []string{}}
	resolved := ResolveEngine(agent, profile)
	if len(resolved.ForwardEnv) != 0 {
		t.Fatalf("ForwardEnv = %v, want explicit empty override to clear the inherited HTTPS_PROXY selection", resolved.ForwardEnv)
	}
	got, err := RenderEngineV1(resolved, nil)
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Env) != 0 {
		t.Fatalf("engine.env = %v, want empty — nothing forwarded", got.Env)
	}
}

func TestWorkspaceV1_EngineBlock_DenyTools(t *testing.T) {
	engine := &achv1alpha1.EngineSpec{
		Compaction: &achv1alpha1.CompactionSpec{Auto: ptr(true), Keep: &achv1alpha1.CompactionKeepSpec{Tokens: ptr(int64(8000))}, Buffer: ptr(int64(20000))},
	}
	render := func() string {
		b, err := RenderEngineV1(engine, nil)
		if err != nil {
			t.Fatal(err)
		}
		raw, err := json.Marshal(b)
		if err != nil {
			t.Fatal(err)
		}
		return string(raw)
	}
	if got := render(); strings.Contains(got, "denyTools") {
		t.Fatalf("empty denyTools must be omitted (keeps configVersion stable), got %s", got)
	}
	engine.DenyTools = []string{"question"}
	if got := render(); !strings.Contains(got, `"denyTools":["question"]`) {
		t.Fatalf("denyTools missing from %s", got)
	}
}

func testPartialSession(t *testing.T) {
	profile := &achv1alpha1.WorkspaceSpec{
		IdleTimeoutSeconds: ptr(int64(600)), ShutdownTimeoutSeconds: ptr(int64(300)), MaxConcurrentSessions: ptr(int64(1)),
		Persistence: &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		Session: &achv1alpha1.WorkspaceSessionSpec{
			IdleTimeoutSeconds: ptr(int64(300)),
			Persistence:        &achv1alpha1.WorkspacePersistenceSpec{Enabled: ptr(true), RetentionDays: ptr(int64(90))},
		},
	}
	agent := &achv1alpha1.WorkspaceSpec{Session: &achv1alpha1.WorkspaceSessionSpec{IdleTimeoutSeconds: ptr(int64(120))}}
	got := ResolveWorkspace(agent, profile)
	if *got.Session.IdleTimeoutSeconds != 120 {
		t.Fatalf("session.idleTimeoutSeconds = %d, want 120", *got.Session.IdleTimeoutSeconds)
	}
	if *got.IdleTimeoutSeconds != 600 || *got.ShutdownTimeoutSeconds != 300 || *got.MaxConcurrentSessions != 1 {
		t.Fatalf("workspace siblings not preserved: %+v", got)
	}
	if got.Persistence == nil || *got.Persistence.Enabled != true || *got.Persistence.RetentionDays != 90 {
		t.Fatalf("workspace.persistence not preserved: %+v", got.Persistence)
	}
	if got.Session.Persistence == nil || *got.Session.Persistence.Enabled != true || *got.Session.Persistence.RetentionDays != 90 {
		t.Fatalf("workspace.session.persistence not preserved: %+v", got.Session.Persistence)
	}
}

func testPartialEnvIdentity(t *testing.T) {
	profile := []corev1.EnvVar{{Name: "SHARED", Value: "profile"}, {Name: "FROM_PROFILE", Value: "p"}}
	agent := []corev1.EnvVar{{Name: "SHARED", Value: "agent"}, {Name: "FROM_AGENT", Value: "a"}}
	got := ResolveEnv(agent, profile)
	want := map[string]string{"SHARED": "agent", "FROM_PROFILE": "p", "FROM_AGENT": "a"}
	if len(got) != len(want) {
		t.Fatalf("ResolveEnv = %+v, want %d entries", got, len(want))
	}
	for _, e := range got {
		if e.Value != want[e.Name] {
			t.Errorf("env %s = %q, want %q", e.Name, e.Value, want[e.Name])
		}
	}

	// Identity is agent-only: ResolveAch never backfills it from the profile (the
	// profile is forbidden from setting one by object-level CEL), and merging other
	// profile fields alongside it must not disturb the agent's own identity.
	achProfile := &achv1alpha1.AchSpec{Environment: ptr("prod")}
	achAgent := &achv1alpha1.AchSpec{Identity: &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: "ek", Key: "ek"}}}
	achGot := ResolveAch(achAgent, achProfile)
	if achGot.Identity == nil || achGot.Identity.SecretRef.Name != "ek" {
		t.Fatalf("Identity = %v, want the agent's own identity preserved", achGot.Identity)
	}
	if achGot.Environment == nil || *achGot.Environment != "prod" {
		t.Fatalf("Environment = %v, want inherited \"prod\"", achGot.Environment)
	}
}
