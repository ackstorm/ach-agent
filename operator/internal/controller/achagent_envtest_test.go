// SPDX-License-Identifier: MIT

package controller

// ACHAgent reconcile envtest cases. Reuses the package envtest harness
// (suite_test.go: TestMain manager bootstrap, shared WatchNamespace-scoped
// cache, k8sClient direct client, Eventually poll helper). No per-test
// namespace — the manager cache is scoped to WatchNamespace, so every object
// is created there with a unique name.

import (
	"context"
	"encoding/json"
	"strings"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	rbacv1 "k8s.io/api/rbac/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/apis/meta/v1/unstructured"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// testExecutionSpec/testWorkspaceSpec/testArtifactsSpec/testEngineSpec/testLimitsSpec
// satisfy the workspace-v1 CRD's required blocks (AgentProfileSpec.Execution,
// achagent.workspace, achagent.artifacts, achagent.engine.compaction, achagent.limits) AND
// Render2's stricter "every field required on the resolved value" check (RenderLimitsV1/
// RenderWorkspaceV1/RenderArtifactsV1/RenderEngineV1) — admission (CEL) only checks block
// presence, but a reconcile that must reach WorkloadApplied=True needs the full value.
// Values are irrelevant to the behavior most of these tests exercise.
func testExecutionSpec() achv1alpha1.ExecutionInfraSpec {
	return achv1alpha1.ExecutionInfraSpec{Image: "registry.test/exec:0.1.0", EphemeralStorage: "1Gi"}
}
func testLimitsSpec(maxSteps int64) *achv1alpha1.LimitsSpec {
	return &achv1alpha1.LimitsSpec{
		MaxActiveWorkspaces: ptrInt64(4), MaxConcurrentInvocations: ptrInt64(4),
		MaxInvocationSeconds: ptrInt64(900), MaxQueuedTotal: ptrInt64(100), IdempotencyWindowSeconds: ptrInt64(3600),
		MaxSteps: ptrInt64(maxSteps),
	}
}
func testWorkspaceSpec() *achv1alpha1.WorkspaceSpec {
	return &achv1alpha1.WorkspaceSpec{
		IdleTimeoutSeconds: ptrInt64(600), ShutdownTimeoutSeconds: ptrInt64(300), MaxConcurrentSessions: ptrInt64(1),
		Persistence: &achv1alpha1.WorkspacePersistenceSpec{Enabled: boolPtr(false), RetentionDays: ptrInt64(90)},
		Session: &achv1alpha1.WorkspaceSessionSpec{
			IdleTimeoutSeconds: ptrInt64(300),
			Persistence:        &achv1alpha1.WorkspacePersistenceSpec{Enabled: boolPtr(false), RetentionDays: ptrInt64(90)},
		},
	}
}
func testArtifactsSpec() *achv1alpha1.ArtifactsSpec {
	return &achv1alpha1.ArtifactsSpec{Enabled: boolPtr(false), MaxArtifactBytes: ptrInt64(104857600), RetentionDays: ptrInt64(90)}
}
func testEngineSpec() *achv1alpha1.EngineSpec {
	return &achv1alpha1.EngineSpec{
		Compaction: &achv1alpha1.CompactionSpec{Auto: boolPtr(true), Keep: &achv1alpha1.CompactionKeepSpec{Tokens: ptrInt64(8000)}, Buffer: ptrInt64(20000)},
	}
}

func boolPtr(v bool) *bool { return &v }

// envtestIdentity is the test-fixture ach block: required identity, optional environment.
func envtestIdentity(secretName, environment string) *achv1alpha1.AchSpec {
	return &achv1alpha1.AchSpec{
		Environment: &environment,
		Identity:    &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: secretName, Key: "ek"}},
	}
}

func mustApply(t *testing.T, ctx context.Context, obj client.Object) {
	t.Helper()
	if err := k8sClient.Create(ctx, obj); err != nil {
		t.Fatalf("create %T %q: %v", obj, obj.GetName(), err)
	}
}

// getControlStatefulSet fetches the named ACHAgent to learn its apiserver-assigned UID, then
// gets the control StatefulSet by its contract name (agent-<agent name>) — NOT
// agentResourceName(name): the control StatefulSet is UID-named per §11, unlike the
// ConfigMap/Service/NetworkPolicy children, which still use the legacy name-based scheme.
func getControlStatefulSet(t *testing.T, ctx context.Context, agentName string) appsv1.StatefulSet {
	t.Helper()
	var a achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: agentName}, &a); err != nil {
		t.Fatalf("get achagent %q: %v", agentName, err)
	}
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(a.Name)}, &sts); err != nil {
		t.Fatalf("get control statefulset for %q: %v", agentName, err)
	}
	return sts
}

// waitAgentCond polls the named ACHAgent until condType reaches want (or ~10s).
func waitAgentCond(t *testing.T, ctx context.Context, name, condType string, want metav1.ConditionStatus) {
	t.Helper()
	var last *metav1.Condition
	ok := Eventually(func() bool {
		var a achv1alpha1.ACHAgent
		if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: name}, &a); err != nil {
			return false
		}
		c := apimeta.FindStatusCondition(a.Status.Conditions, condType)
		if c == nil {
			return false
		}
		last = c
		return c.Status == want
	}, 10*time.Second, 200*time.Millisecond)
	if !ok {
		if last == nil {
			t.Fatalf("ACHAgent %q condition %q = <none>, want %v", name, condType, want)
		}
		t.Fatalf("ACHAgent %q condition %q = %v (reason=%s msg=%q), want %v", name, condType, last.Status, last.Reason, last.Message, want)
	}
}

func assertConfigMapValid(t *testing.T, ctx context.Context, cmName string) {
	t.Helper()
	var cm corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: cmName}, &cm); err != nil {
		t.Fatalf("get configmap %q: %v", cmName, err)
	}
	raw, ok := cm.Data["config.json"]
	if !ok {
		t.Fatalf("configmap %q has no config.json", cmName)
	}
	var m map[string]any
	if err := json.Unmarshal([]byte(raw), &m); err != nil {
		t.Fatalf("config.json is not valid JSON: %v", err)
	}
	if m["schemaVersion"] != "workspace-v2" {
		t.Errorf("config.json schemaVersion = %v, want \"workspace-v2\"", m["schemaVersion"])
	}
	achBlock, _ := m["ach"].(map[string]any)
	identity, _ := achBlock["identity"].(map[string]any)
	if identity["env"] == "" || identity["env"] == nil {
		t.Errorf("config.json ach.identity.env missing: %v", achBlock)
	}
}

func TestACHAgent_MissingProfile_ProfileResolvedFalse(t *testing.T) {
	ctx := context.Background()
	agent := &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-no-profile", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-ghost"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek", "e")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	}
	if err := k8sClient.Create(ctx, agent); err != nil {
		t.Fatalf("create agent: %v", err)
	}
	waitAgentCond(t, ctx, "aa-no-profile", condProfileResolved, metav1.ConditionFalse)
	waitAgentCond(t, ctx, "aa-no-profile", condReady, metav1.ConditionFalse)
}

func TestACHAgent_HappyPath_AppliesConfigMapAndDeployment(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-happy", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-happy", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-happy", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-happy"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-happy", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})
	// No kubelet in envtest → WorkloadReady stays False; WorkloadApplied must go True.
	waitAgentCond(t, ctx, "aa-happy", condWorkloadApplied, metav1.ConditionTrue)
	assertConfigMapValid(t, ctx, agentResourceName("aa-happy"))
	var agent achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-happy"}, &agent); err != nil {
		t.Fatal(err)
	}
	var keySecret corev1.Secret
	keyName := workspaceKeySecretName(string(agent.UID))
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: keyName}, &keySecret); err != nil {
		t.Fatalf("get created workspace key Secret: %v", err)
	}
	if !metav1.IsControlledBy(&keySecret, &agent) || len(keySecret.Data[workspaceKeyDataKey]) != 64 {
		t.Fatalf("workspace key Secret owner/data invalid: owners=%+v key length=%d", keySecret.OwnerReferences, len(keySecret.Data[workspaceKeyDataKey]))
	}
	sts := getControlStatefulSet(t, ctx, "aa-happy")
	oldPodHash := sts.Spec.Template.Annotations[configHashAnnotation]
	var keyEnvCount int
	for _, e := range sts.Spec.Template.Spec.Containers[0].Env {
		if e.Name == "ACH_SANDBOX_KEY" {
			keyEnvCount++
			if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil || e.ValueFrom.SecretKeyRef.Name != keyName || e.ValueFrom.SecretKeyRef.Key != workspaceKeyDataKey {
				t.Fatalf("control ACH_SANDBOX_KEY = %+v, want SecretKeyRef %s/key", e, keyName)
			}
		}
		if strings.HasPrefix(e.Name, "ACH_STORAGE_") {
			t.Errorf("disabled backend unexpectedly added %s to the control env", e.Name)
		}
	}
	if keyEnvCount != 1 {
		t.Fatalf("control ACH_SANDBOX_KEY entries = %d, want exactly one", keyEnvCount)
	}
	var config corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: agentResourceName("aa-happy")}, &config); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(config.Data[configFileName], string(keySecret.Data[workspaceKeyDataKey])) {
		t.Fatal("private workspace key leaked into config.json")
	}
	var before map[string]any
	if err := json.Unmarshal([]byte(config.Data[configFileName]), &before); err != nil {
		t.Fatal(err)
	}
	oldConfigVersion := before["configVersion"]
	var bootstrap corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ExecutionServiceAccountName(string(agent.UID))}, &bootstrap); !apierrors.IsNotFound(err) {
		t.Fatalf("execution bootstrap ConfigMap must not exist (the operator creates no bootstrap), Get err: %v", err)
	}
	assertExecutionSAUnbound(t, ctx, &agent)
	keySecret.Data[workspaceKeyDataKey] = []byte("operator-supplied-key-rotation")
	if err := k8sClient.Update(ctx, &keySecret); err != nil {
		t.Fatalf("update workspace key Secret: %v", err)
	}
	if !Eventually(func() bool {
		updated := getControlStatefulSet(t, ctx, "aa-happy")
		return updated.Spec.Template.Annotations[configHashAnnotation] != oldPodHash
	}, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("workspace key Secret change did not roll the control pod")
	}
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: agentResourceName("aa-happy")}, &config); err != nil {
		t.Fatal(err)
	}
	var after map[string]any
	if err := json.Unmarshal([]byte(config.Data[configFileName]), &after); err != nil {
		t.Fatal(err)
	}
	if after["configVersion"] != oldConfigVersion {
		t.Errorf("configVersion changed with private key: before=%v after=%v", oldConfigVersion, after["configVersion"])
	}
}

func TestACHAgent_ProfileDeletedAfterApplied_ReadyFlipsFalse(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-regress", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	prof := &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-regress", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}}
	mustApply(t, ctx, prof)
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-regress", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-regress"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-regress", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})
	waitAgentCond(t, ctx, "aa-regress", condWorkloadApplied, metav1.ConditionTrue)
	// Delete the profile → next reconcile must flip ProfileResolved AND Ready to False (R-B1).
	if err := k8sClient.Delete(ctx, prof); err != nil {
		t.Fatalf("delete profile: %v", err)
	}
	waitAgentCond(t, ctx, "aa-regress", condProfileResolved, metav1.ConditionFalse)
	waitAgentCond(t, ctx, "aa-regress", condReady, metav1.ConditionFalse)
}

// TestACHAgent_RoutingRequiresNonEmptyKeys: routing.workspaceKey/sessionKey are optional
// and independent, but a PRESENT one must be non-empty (contract §2).
func TestACHAgent_RoutingRequiresNonEmptyKeys(t *testing.T) {
	ctx := context.Background()
	base := func(name string, routing *achv1alpha1.RoutingSpec) *achv1alpha1.ACHAgent {
		return &achv1alpha1.ACHAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace},
			Spec: achv1alpha1.ACHAgentSpec{
				ProfileRef:    achv1alpha1.LocalObjectRef{Name: "p"},
				AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("ek", "e")},
				Channels: []achv1alpha1.ChannelSpec{{
					Name: "c", Type: "cron", Routing: routing,
					Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"},
				}},
			},
		}
	}
	empty := ""
	key := "{{ payload.thread }}"
	// present but empty workspaceKey → rejected
	if err := k8sClient.Create(ctx, base("aa-routing-empty", &achv1alpha1.RoutingSpec{WorkspaceKey: &empty})); err == nil {
		t.Fatal("expected rejection: routing.workspaceKey must be non-empty when set")
	}
	// omitted routing → accepted
	if err := k8sClient.Create(ctx, base("aa-routing-omitted", nil)); err != nil {
		t.Fatalf("omitted routing must be accepted: %v", err)
	}
	// present non-empty sessionKey only → accepted (independent overrides)
	if err := k8sClient.Create(ctx, base("aa-routing-sessiononly", &achv1alpha1.RoutingSpec{SessionKey: &key})); err != nil {
		t.Fatalf("sessionKey-only override must be accepted: %v", err)
	}
}

// Disabling expose.service (true→false) prunes the now-orphaned Service. Owner-ref
// GC only fires on ACHAgent delete, so without this the Service would leak — the
// same convergence that cleans agents predating the expose feature.
func TestACHAgent_ExposeServiceDisabled_PrunesService(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-prune", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-prune", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}})

	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prune", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-prune"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-prune", "prod")},
			Expose:        &achv1alpha1.ExposeSpec{Service: true},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "gh", Type: "webhook", Source: "github", Webhook: &achv1alpha1.WebhookSpec{Auth: achv1alpha1.WebhookAuthSpec{Type: "none"}}}},
		},
	})
	waitAgentCond(t, ctx, "aa-prune", condWorkloadApplied, metav1.ConditionTrue)

	svcKey := types.NamespacedName{Namespace: WatchNamespace, Name: agentResourceName("aa-prune")}
	if err := k8sClient.Get(ctx, svcKey, &corev1.Service{}); err != nil {
		t.Fatalf("exposed agent must have a Service before disable: %v", err)
	}

	// Flip expose off — mirrors a pre-feat agent: Service on disk, spec no longer wants it.
	var a achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-prune"}, &a); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	a.Spec.Expose = nil
	if err := k8sClient.Update(ctx, &a); err != nil {
		t.Fatalf("disable expose: %v", err)
	}

	if !Eventually(func() bool {
		return apierrors.IsNotFound(k8sClient.Get(ctx, svcKey, &corev1.Service{}))
	}, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("Service must be pruned after expose.service disabled")
	}
}

// TestACHAgent_MemoryAuth_WiresConfigAndSecretKeyRef proves the ach-memory user-key
// secret round-trips: the ConfigMap renders auth.env = the operator-generated
// name, and the Deployment carries a matching secretKeyRef env var (never inline,
// never a file). A missing referenced key drives ChannelSecretsResolved=False via
// the shared ReferencedSecrets/checkChannelSecrets path.
func TestACHAgent_MemoryAuth_WiresConfigAndSecretKeyRef(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-mem", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "hs-admin", Namespace: WatchNamespace}, Data: map[string][]byte{"token": []byte("bearer")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-mem", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}})

	memAgent := func(name, secretName string) *achv1alpha1.ACHAgent {
		return &achv1alpha1.ACHAgent{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace},
			Spec: achv1alpha1.ACHAgentSpec{
				ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-mem"},
				AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-mem", "prod")},
				Memory: &achv1alpha1.MemorySpec{Type: "ach-memory", AchMemory: &achv1alpha1.AchMemorySpec{
					Endpoint: "http://ach-memory.ach.svc:8000/mcp/",
					Auth:     &achv1alpha1.AchMemoryAuthSpec{Type: "bearer", SecretRef: &achv1alpha1.SecretKeyRef{Name: secretName, Key: "token"}},
				}},
				Channels: []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
			},
		}
	}

	// Auth secret present → applied, config + Deployment wired.
	mustApply(t, ctx, memAgent("aa-mem", "hs-admin"))
	waitAgentCond(t, ctx, "aa-mem", condWorkloadApplied, metav1.ConditionTrue)
	waitAgentCond(t, ctx, "aa-mem", condChannelSecretsResolved, metav1.ConditionTrue)

	var cm corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: agentResourceName("aa-mem")}, &cm); err != nil {
		t.Fatalf("get configmap: %v", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal([]byte(cm.Data["config.json"]), &cfg); err != nil {
		t.Fatalf("config.json invalid: %v", err)
	}
	auth := cfg["memory"].(map[string]any)["achMemory"].(map[string]any)["auth"].(map[string]any)
	if auth["env"] != "ACH_SECRET_MEMORY_AUTH" {
		t.Errorf("config memory.achMemory.auth.env = %v, want ACH_SECRET_MEMORY_AUTH", auth["env"])
	}

	dep := getControlStatefulSet(t, ctx, "aa-mem")
	var found bool
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		if e.Name != "ACH_SECRET_MEMORY_AUTH" {
			continue
		}
		found = true
		if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil ||
			e.ValueFrom.SecretKeyRef.Name != "hs-admin" || e.ValueFrom.SecretKeyRef.Key != "token" {
			t.Errorf("deployment env ACH_SECRET_MEMORY_AUTH must be secretKeyRef hs-admin/token, got %+v", e)
		}
	}
	if !found {
		t.Error("deployment missing ACH_SECRET_MEMORY_AUTH env var")
	}

	// Referenced key missing from the secret → ChannelSecretsResolved=False.
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "hs-nokey", Namespace: WatchNamespace}, Data: map[string][]byte{"other": []byte("x")}})
	mustApply(t, ctx, memAgent("aa-mem-nokey", "hs-nokey"))
	waitAgentCond(t, ctx, "aa-mem-nokey", condChannelSecretsResolved, metav1.ConditionFalse)
}

func TestACHAgent_EnvInheritanceHandoffAndSecretRotation(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-env", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-clone-env", Namespace: WatchNamespace}, Data: map[string][]byte{"token": []byte("one")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-env", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent:  achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()},
			Execution: testExecutionSpec(),
			Env: []corev1.EnvVar{
				{Name: "GITLAB_BASE_URL", Value: "https://git.example.com"},
				{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "aa-clone-env"}, Key: "token"}}},
				{Name: "SHARED", Value: "profile"},
			},
		},
	})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-env", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-env"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-env", "")},
			Env:           []corev1.EnvVar{{Name: "SHARED", Value: "agent"}},
			Channels: []achv1alpha1.ChannelSpec{{
				Name: "review", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"},
				Handoff: &achv1alpha1.HandoffSpec{PrepareSpec: achv1alpha1.PrepareSpec{Script: "true", ForwardEnv: []string{"GITLAB_BASE_URL", "GITLAB_TOKEN", "MISSING"}}, Destination: "handoff"},
			}},
		},
	})
	// Negative control: MISSING is not in the merged environment, so the channel's
	// forwardEnv rejects it and the controller must fail closed with RenderFailed.
	var lastCond *metav1.Condition
	if !Eventually(func() bool {
		var a achv1alpha1.ACHAgent
		if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-env"}, &a); err != nil {
			return false
		}
		c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied)
		if c == nil {
			return false
		}
		lastCond = c
		return c.Status == metav1.ConditionFalse
	}, 10*time.Second, 200*time.Millisecond) {
		t.Fatalf("ACHAgent %q condition %q = <none>, want False/RenderFailed", "aa-env", condWorkloadApplied)
	}
	if lastCond.Reason != "RenderFailed" || !strings.Contains(lastCond.Message, "MISSING") {
		t.Fatalf("ACHAgent %q condition %q = %+v, want reason RenderFailed identifying MISSING", "aa-env", condWorkloadApplied, lastCond)
	}

	var toFix achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-env"}, &toFix); err != nil {
		t.Fatal(err)
	}
	toFix.Spec.Channels[0].Handoff.ForwardEnv = []string{"GITLAB_BASE_URL", "GITLAB_TOKEN"}
	if err := k8sClient.Update(ctx, &toFix); err != nil {
		t.Fatal(err)
	}
	waitAgentCond(t, ctx, "aa-env", condWorkloadApplied, metav1.ConditionTrue)

	dep := getControlStatefulSet(t, ctx, "aa-env")
	wantEnv := map[string]string{"GITLAB_BASE_URL": "https://git.example.com", "SHARED": "agent"}
	wantSecrets := map[string]bool{"GITLAB_TOKEN": false, "ACH_SECRET_REVIEW_HANDOFF_GITLAB_TOKEN": false}
	for _, e := range dep.Spec.Template.Spec.Containers[0].Env {
		if want, ok := wantEnv[e.Name]; ok {
			if e.Value != want {
				t.Errorf("Pod env %s = %q, want %q", e.Name, e.Value, want)
			}
			delete(wantEnv, e.Name)
		}
		if _, ok := wantSecrets[e.Name]; ok && e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Name == "aa-clone-env" && e.ValueFrom.SecretKeyRef.Key == "token" {
			wantSecrets[e.Name] = true
		}
	}
	if len(wantEnv) > 0 {
		t.Errorf("Pod env missing literals: %v", wantEnv)
	}
	for name, found := range wantSecrets {
		if !found {
			t.Errorf("Pod env missing secretKeyRef %s", name)
		}
	}

	var cm corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: agentResourceName("aa-env")}, &cm); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(cm.Data[configFileName], `"one"`) {
		t.Fatal("secret plaintext leaked into ConfigMap")
	}
	var cfg map[string]any
	if err := json.Unmarshal([]byte(cm.Data[configFileName]), &cfg); err != nil {
		t.Fatal(err)
	}
	handoff := cfg["channels"].([]any)[0].(map[string]any)["handoff"].(map[string]any)
	if handoff["env"].(map[string]any)["GITLAB_BASE_URL"] != "https://git.example.com" {
		t.Fatalf("handoff literals = %v", handoff["env"])
	}
	if handoff["secretEnv"].(map[string]any)["GITLAB_TOKEN"].(map[string]any)["env"] != "ACH_SECRET_REVIEW_HANDOFF_GITLAB_TOKEN" {
		t.Fatalf("handoff secret aliases = %v", handoff["secretEnv"])
	}

	oldHash := dep.Spec.Template.Annotations[configHashAnnotation]
	stsKey := types.NamespacedName{Namespace: dep.Namespace, Name: dep.Name}
	var secret corev1.Secret
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-clone-env"}, &secret); err != nil {
		t.Fatal(err)
	}
	secret.Data["token"] = []byte("two")
	if err := k8sClient.Update(ctx, &secret); err != nil {
		t.Fatal(err)
	}
	if !Eventually(func() bool {
		if err := k8sClient.Get(ctx, stsKey, &dep); err != nil {
			return false
		}
		return dep.Spec.Template.Annotations[configHashAnnotation] != oldHash
	}, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("profile env Secret rotation did not roll the StatefulSet hash")
	}
}

func TestACHAgent_WebhookScriptRejectsHandoff(t *testing.T) {
	ctx := context.Background()
	agent := &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "webhook-script-with-handoff", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "unused"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("unused", "")},
			Channels: []achv1alpha1.ChannelSpec{{
				Name:    "register",
				Type:    "webhook-script",
				Webhook: &achv1alpha1.WebhookSpec{Auth: achv1alpha1.WebhookAuthSpec{Type: "none"}},
				Script:  &achv1alpha1.PrepareSpec{Script: "true"},
				Handoff: &achv1alpha1.HandoffSpec{PrepareSpec: achv1alpha1.PrepareSpec{Script: "true"}, Destination: "handoff"},
			}},
		},
	}

	err := k8sClient.Create(ctx, agent)
	if err == nil || !strings.Contains(err.Error(), "webhook-script forbids prompt and handoff") {
		t.Fatalf("Create error = %v", err)
	}
}

func TestACHAgent_EnvAdmissionRejectsReservedAndUnsupportedSources(t *testing.T) {
	ctx := context.Background()
	profile := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-env-invalid", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Limits: &achv1alpha1.LimitsSpec{MaxSteps: ptrInt64(10)}},
			Env:      []corev1.EnvVar{{Name: "FROM_CONFIGMAP", ValueFrom: &corev1.EnvVarSource{ConfigMapKeyRef: &corev1.ConfigMapKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "cm"}, Key: "key"}}}},
		},
	}
	if err := k8sClient.Create(ctx, profile); err == nil {
		t.Fatal("configMapKeyRef must be rejected; only secretKeyRef is supported")
	}
	profile.Name = "aa-prof-env-both"
	profile.Spec.Env = []corev1.EnvVar{{Name: "BOTH", Value: "literal", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: "secret"}, Key: "key"}}}}
	if err := k8sClient.Create(ctx, profile); err == nil {
		t.Fatal("value and valueFrom must be rejected together")
	}

	agent := &unstructured.Unstructured{Object: map[string]any{"spec": map[string]any{
		"profileRef": map[string]any{"name": "p"},
		"ach":        map[string]any{"identity": map[string]any{"secretRef": map[string]any{"name": "ek", "key": "ek"}}},
		"env":        []any{map[string]any{"name": "ACH_TOKEN", "value": "leak"}},
		"channels":   []any{map[string]any{"name": "c", "type": "cron", "cron": map[string]any{"schedule": "* * * * *"}}},
	}}}
	agent.SetGroupVersionKind(achv1alpha1.GroupVersion.WithKind("ACHAgent"))
	agent.SetNamespace(WatchNamespace)
	agent.SetName("aa-env-reserved")
	if err := k8sClient.Create(ctx, agent); err == nil {
		t.Fatal("reserved ACH_* env must be rejected")
	}
}

// TestACHAgent_PartialEngineForwardEnv_EmptyOverrideClearsInheritance is the real CR
// admission counterpart of TestWorkspaceV1_PartialOverrides's "empty forwardEnv selection
// clears inherited exposure" case: a typed Go struct would DROP an explicit
// `ForwardEnv: []string{}` on send (the json tag carries `omitempty`, which treats an empty
// slice the same as absent), silently losing the override's presence. Unstructured input
// sends the literal `"forwardEnv": []` the apiserver stores, proving the LIVE CR — not just
// the in-memory Go value — retains the distinction that engine.go's ResolveEngine (unit-
// tested in TestWorkspaceV1_PartialOverrides) relies on.
//
// Object-only limitation: this cannot yet assert the merge result reaches a rendered
// ConfigMap — every full reconcile currently fails WorkloadApplied at RenderInfrastructureV1
// (the pre-existing, pre-Task-3 broker/TLS wire shape vs. the already-reset vendored schema;
// confirmed against the baseline TestACHAgent_HappyPath_AppliesConfigMapAndDeployment, which
// fails the same way with no CR involved in this task's changes). That cutover is Task 4's
// scope (plan: "full-root fixtures await Task 4") — the merge behavior itself is proven at
// the resolve/render-function boundary by TestWorkspaceV1_PartialOverrides.
func TestACHAgent_PartialEngineForwardEnv_EmptyOverrideClearsInheritance(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-fe", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-fe", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{
				Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"},
				Model:     &achv1alpha1.ModelSpec{Name: "m", Type: "openai"},
				Engine:    &achv1alpha1.EngineSpec{ForwardEnv: []string{"HTTPS_PROXY"}, Compaction: testEngineSpec().Compaction},
				Limits:    testLimitsSpec(10),
				Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(),
			},
			Execution: testExecutionSpec(),
			Env:       []corev1.EnvVar{{Name: "HTTPS_PROXY", Value: "http://proxy:8080"}},
		},
	})

	agent := &unstructured.Unstructured{Object: map[string]any{"spec": map[string]any{
		"profileRef": map[string]any{"name": "aa-prof-fe"},
		"ach":        map[string]any{"identity": map[string]any{"secretRef": map[string]any{"name": "aa-ek-fe", "key": "ek"}}},
		"engine":     map[string]any{"forwardEnv": []any{}},
		"channels":   []any{map[string]any{"name": "c", "type": "cron", "cron": map[string]any{"schedule": "* * * * *"}}},
	}}}
	agent.SetGroupVersionKind(achv1alpha1.GroupVersion.WithKind("ACHAgent"))
	agent.SetNamespace(WatchNamespace)
	agent.SetName("aa-fe-empty")
	if err := k8sClient.Create(ctx, agent); err != nil {
		t.Fatalf("create agent with explicit empty forwardEnv: %v", err)
	}

	var stored achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-fe-empty"}, &stored); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	if stored.Spec.Engine == nil || stored.Spec.Engine.ForwardEnv == nil || len(stored.Spec.Engine.ForwardEnv) != 0 {
		t.Fatalf("spec.engine.forwardEnv = %#v, want a present (non-nil) empty slice, not omitted/inherited", stored.Spec.Engine)
	}
}

// TestACHAgent_PartialLimitsOverride_MissingEffectiveMaxStepsBlocksWorkload: admission only
// checks that spec.achagent.limits is PRESENT (object-level CEL), not that every field is
// set — maxSteps stays a real requirement, enforced on the EFFECTIVE (resolved) value at
// reconcile time (RenderLimitsV1), same as the other wire-required policy blocks. Asserts
// the specific failure reason names the limits gap, not the unrelated pre-existing
// infrastructure RenderFailed every reconcile currently also hits (see the note on
// TestACHAgent_PartialEngineForwardEnv_EmptyOverrideClearsInheritance) — RenderLimitsV1 runs
// and fails BEFORE Render2 ever reaches RenderInfrastructureV1, so this is not masked by it.
func TestACHAgent_PartialLimitsOverride_MissingEffectiveMaxStepsBlocksWorkload(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-ms", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	incompleteLimits := testLimitsSpec(1)
	incompleteLimits.MaxSteps = nil
	mustApply(t, ctx, &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-ms", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{
				Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"},
				Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: incompleteLimits,
				Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec(),
			},
			Execution: testExecutionSpec(),
		},
	})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-ms", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-ms"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-ms", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})
	waitAgentCond(t, ctx, "aa-ms", condWorkloadApplied, metav1.ConditionFalse)

	var a achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-ms"}, &a); err != nil {
		t.Fatalf("get achagent: %v", err)
	}
	c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied)
	if c == nil || !strings.Contains(c.Message, "maxSteps") {
		t.Fatalf("WorkloadApplied = %+v, want a message naming the missing effective limits.maxSteps (not masked by an unrelated failure)", c)
	}
}

// TestACHAgent_PartialLimits_NonpositiveInvocationSecondsRejectedAtAdmission: bounded
// invocation time is a real requirement (contract §3) enforced structurally — a nonpositive
// maxInvocationSeconds is rejected by the apiserver's OpenAPI schema (Minimum=1), before CEL
// or the controller ever see it.
func TestACHAgent_PartialLimits_NonpositiveInvocationSecondsRejectedAtAdmission(t *testing.T) {
	ctx := context.Background()
	zeroSeconds := testLimitsSpec(10)
	zeroSeconds.MaxInvocationSeconds = ptrInt64(0)
	profile := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-zerosec", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent: achv1alpha1.AgentDefaults{
				Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"},
				Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: zeroSeconds,
				Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec(),
			},
			Execution: testExecutionSpec(),
		},
	}
	if err := k8sClient.Create(ctx, profile); err == nil {
		t.Fatal("expected rejection: limits.maxInvocationSeconds must be >= 1")
	}
}

// TestACHAgent_CapabilityOptional proves spec.ach.capability is optional at the API
// server: a manifest that omits the key entirely still applies and reconciles.
func TestACHAgent_CapabilityOptional(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-nocap", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-nocap", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}})

	agent := func(name string, ach map[string]any) *unstructured.Unstructured {
		ach["identity"] = map[string]any{"secretRef": map[string]any{"name": "aa-ek-nocap", "key": "ek"}}
		spec := map[string]any{
			"profileRef": map[string]any{"name": "aa-prof-nocap"},
			"ach":        ach,
			"channels":   []any{map[string]any{"name": "c", "type": "cron", "cron": map[string]any{"schedule": "* * * * *"}}},
		}
		u := &unstructured.Unstructured{Object: map[string]any{"spec": spec}}
		u.SetGroupVersionKind(achv1alpha1.GroupVersion.WithKind("ACHAgent"))
		u.SetNamespace(WatchNamespace)
		u.SetName(name)
		return u
	}

	// Key absent → accepted, and render still emits a capability block.
	mustApply(t, ctx, agent("aa-nocap", map[string]any{}))
	waitAgentCond(t, ctx, "aa-nocap", condWorkloadApplied, metav1.ConditionTrue)
	assertConfigMapValid(t, ctx, agentResourceName("aa-nocap"))

	// Bare `capability:` in YAML → explicit null. The API server treats null as
	// unset for an optional field, so this is the same as omitting the key.
	mustApply(t, ctx, agent("aa-nullcap", map[string]any{"capability": nil}))
	waitAgentCond(t, ctx, "aa-nullcap", condWorkloadApplied, metav1.ConditionTrue)
}

// CEL: spec.achagent.image is required (nonempty) on AgentProfile. Two invalid
// shapes: `achagent: {}` present-but-empty (any typed client), and spec.achagent
// omitted entirely (only unstructured can produce it — exercises the
// has(self.spec.achagent) branch + the Required marker).
func TestAgentProfile_CEL_AchagentImageRequired(t *testing.T) {
	ctx := context.Background()
	bad := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-noimg", Namespace: WatchNamespace},
		Spec:       achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: &achv1alpha1.LimitsSpec{MaxSteps: ptrInt64(10)}}},
	}
	if err := k8sClient.Create(ctx, bad); err == nil {
		t.Fatal("expected rejection: empty spec.achagent.image (achagent: {})")
	}
	u := &unstructured.Unstructured{Object: map[string]any{"spec": map[string]any{}}}
	u.SetAPIVersion("ach.ackstorm.ai/v1alpha1")
	u.SetKind("AgentProfile")
	u.SetName("aa-prof-noachagent")
	u.SetNamespace(WatchNamespace)
	if err := k8sClient.Create(ctx, u); err == nil {
		t.Fatal("expected rejection: spec.achagent omitted entirely")
	}
	good := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-img-ok", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent:  achv1alpha1.AgentDefaults{Image: "img:test", Limits: &achv1alpha1.LimitsSpec{MaxSteps: ptrInt64(10)}, Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()},
			Execution: testExecutionSpec(),
		},
	}
	if err := k8sClient.Create(ctx, good); err != nil {
		t.Fatalf("valid profile rejected: %v", err)
	}
}

// CEL: spec.achagent.limits is required on AgentProfile — the only place a profile
// is guaranteed to carry a positive limits.maxSteps (contract §3).
func TestAgentProfile_CEL_AchagentLimitsRequired(t *testing.T) {
	ctx := context.Background()
	bad := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-nolimits", Namespace: WatchNamespace},
		Spec:       achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test"}},
	}
	if err := k8sClient.Create(ctx, bad); err == nil {
		t.Fatal("expected rejection: spec.achagent.limits omitted")
	}
	zero := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-zerosteps", Namespace: WatchNamespace},
		Spec:       achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Limits: &achv1alpha1.LimitsSpec{}}},
	}
	if err := k8sClient.Create(ctx, zero); err == nil {
		t.Fatal("expected rejection: limits.maxSteps must be positive (Minimum=1), got the zero value")
	}
	good := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-limits-ok", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Achagent:  achv1alpha1.AgentDefaults{Image: "img:test", Limits: &achv1alpha1.LimitsSpec{MaxSteps: ptrInt64(30)}, Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()},
			Execution: testExecutionSpec(),
		},
	}
	if err := k8sClient.Create(ctx, good); err != nil {
		t.Fatalf("valid profile rejected: %v", err)
	}
}

// CEL: spec.achagent.ach.identity is forbidden on a profile — a credential is never
// a shared implicit default (contract §5).
func TestAgentProfile_CEL_AchIdentityForbidden(t *testing.T) {
	ctx := context.Background()
	bad := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-identityleak", Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{Achagent: achv1alpha1.AgentDefaults{
			Image: "img:test", Limits: &achv1alpha1.LimitsSpec{MaxSteps: ptrInt64(10)},
			Ach: &achv1alpha1.AchSpec{BaseURL: "u", Identity: &achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{Name: "ek", Key: "ek"}}},
		}},
	}
	if err := k8sClient.Create(ctx, bad); err == nil {
		t.Fatal("expected rejection: spec.achagent.ach.identity is forbidden on a profile")
	}
}

// CEL: spec.ach.identity is required on an ACHAgent — the credential is the agent's
// own, never inherited.
func TestACHAgent_CEL_AchIdentityRequired(t *testing.T) {
	ctx := context.Background()
	bad := &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-noidentity", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef: achv1alpha1.LocalObjectRef{Name: "p"},
			Channels:   []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	}
	if err := k8sClient.Create(ctx, bad); err == nil {
		t.Fatal("expected rejection: spec.ach.identity is required")
	}
}

// A profile-named control ServiceAccount is used verbatim: the operator never creates
// ach-harness-<uid>, no Harness RoleBinding exists, and a non-DNS-1123 name
// is rejected at admission.
func TestACHAgent_ControlServiceAccountName_UsedAndNotCreated(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-csa", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	prof := &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-csa", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{ControlServiceAccountName: "ach-sandboxed-agent", Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}}
	mustApply(t, ctx, prof)
	mustApply(t, ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "ach-sandboxed-agent", Namespace: WatchNamespace}})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-csa", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-csa"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-csa", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})
	waitAgentCond(t, ctx, "aa-csa", condWorkloadApplied, metav1.ConditionTrue)
	var agent achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-csa"}, &agent); err != nil {
		t.Fatal(err)
	}
	if got := getControlStatefulSet(t, ctx, "aa-csa").Spec.Template.Spec.ServiceAccountName; got != "ach-sandboxed-agent" {
		t.Errorf("control StatefulSet SA = %q, want ach-sandboxed-agent", got)
	}
	perAgent := render.HarnessName(string(agent.UID))
	var sa corev1.ServiceAccount
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: perAgent}, &sa); err == nil {
		t.Errorf("per-agent SA %s must not be created when the profile sets controlServiceAccountName", perAgent)
	}
	var rb rbacv1.RoleBinding
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: perAgent}, &rb); !apierrors.IsNotFound(err) {
		t.Errorf("no RoleBinding ach-harness-<uid> may exist (control has no Kubernetes access), Get err: %v", err)
	}

	bad := prof.DeepCopy()
	bad.ObjectMeta = metav1.ObjectMeta{Name: "aa-prof-csa-bad", Namespace: WatchNamespace}
	bad.Spec.ControlServiceAccountName = "Not_A_Label"
	if err := k8sClient.Create(ctx, bad); err == nil {
		t.Fatal("expected rejection: controlServiceAccountName is not a DNS-1123 label")
	}
}

// assertExecutionSAUnbound: ach-execution-<uid> is a name only (the runtime renders the
// workspace pod with automountServiceAccountToken=false) — no RoleBinding or
// ClusterRoleBinding anywhere may reference it.
func assertExecutionSAUnbound(t *testing.T, ctx context.Context, agent *achv1alpha1.ACHAgent) {
	t.Helper()
	execSA := render.ExecutionServiceAccountName(string(agent.UID))
	var sa corev1.ServiceAccount
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: execSA}, &sa); err != nil {
		t.Fatalf("get execution ServiceAccount: %v", err)
	}
	if sa.AutomountServiceAccountToken == nil || *sa.AutomountServiceAccountToken {
		t.Errorf("execution ServiceAccount automount = %v, want false", sa.AutomountServiceAccountToken)
	}
	isExec := func(subjects []rbacv1.Subject) bool {
		for _, s := range subjects {
			if s.Kind == rbacv1.ServiceAccountKind && s.Name == execSA {
				return true
			}
		}
		return false
	}
	var rbs rbacv1.RoleBindingList
	if err := k8sClient.List(ctx, &rbs); err != nil {
		t.Fatalf("list RoleBindings: %v", err)
	}
	for _, rb := range rbs.Items {
		if isExec(rb.Subjects) {
			t.Errorf("RoleBinding %s/%s binds the execution ServiceAccount %s", rb.Namespace, rb.Name, execSA)
		}
	}
	var crbs rbacv1.ClusterRoleBindingList
	if err := k8sClient.List(ctx, &crbs); err != nil {
		t.Fatalf("list ClusterRoleBindings: %v", err)
	}
	for _, crb := range crbs.Items {
		if isExec(crb.Subjects) {
			t.Errorf("ClusterRoleBinding %s binds the execution ServiceAccount %s", crb.Name, execSA)
		}
	}
}

// A profile-named control ServiceAccount that does not exist leaves the agent
// ControlServiceAccountResolved=False (no workload applied — its pod could not start), and
// creating the SA re-enqueues the agent without any other change.
func TestACHAgent_ControlServiceAccountName_Missing(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "aa-ek-csa-missing", Namespace: WatchNamespace}, Data: map[string][]byte{"ek": []byte("ek_test")}})
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "aa-prof-csa-missing", Namespace: WatchNamespace}, Spec: achv1alpha1.AgentProfileSpec{ControlServiceAccountName: "ach-late-sa", Achagent: achv1alpha1.AgentDefaults{Image: "img:test", Ach: &achv1alpha1.AchSpec{BaseURL: "https://ach"}, Model: &achv1alpha1.ModelSpec{Name: "m", Type: "openai"}, Limits: testLimitsSpec(10), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec()}, Execution: testExecutionSpec()}})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "aa-csa-missing", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "aa-prof-csa-missing"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("aa-ek-csa-missing", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})
	waitAgentCond(t, ctx, "aa-csa-missing", condControlServiceAccountResolved, metav1.ConditionFalse)
	waitAgentCond(t, ctx, "aa-csa-missing", condReady, metav1.ConditionFalse)
	var agent achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "aa-csa-missing"}, &agent); err != nil {
		t.Fatal(err)
	}
	c := apimeta.FindStatusCondition(agent.Status.Conditions, condControlServiceAccountResolved)
	if c.Reason != "ServiceAccountNotFound" || !strings.Contains(c.Message, "ach-late-sa") {
		t.Errorf("condition = %s/%q, want ServiceAccountNotFound naming ach-late-sa", c.Reason, c.Message)
	}
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName("aa-csa-missing")}, &sts); !apierrors.IsNotFound(err) {
		t.Errorf("control StatefulSet must not be applied while its SA is missing: err=%v", err)
	}

	mustApply(t, ctx, &corev1.ServiceAccount{ObjectMeta: metav1.ObjectMeta{Name: "ach-late-sa", Namespace: WatchNamespace}})
	waitAgentCond(t, ctx, "aa-csa-missing", condControlServiceAccountResolved, metav1.ConditionTrue)
	waitAgentCond(t, ctx, "aa-csa-missing", condWorkloadApplied, metav1.ConditionTrue)
}

