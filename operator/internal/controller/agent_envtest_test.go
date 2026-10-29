// SPDX-License-Identifier: MIT

package controller

// Agent reconcile envtest cases. Reuses the package envtest harness (suite_test.go: TestMain
// manager bootstrap, k8sClient direct client, Eventually poll helper). Every object is
// created in WatchNamespace with a unique name.

import (
	"context"
	"encoding/json"
	"testing"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// testLimitsSpec/testWorkspaceSpec/testArtifactsSpec/testEngineSpec satisfy the AgentProfile
// CRD's required defaults blocks AND Render's stricter "every field required on the resolved
// value" check — admission (CEL) only checks block presence, but a reconcile that must reach
// WorkloadApplied=True needs the full value. Values are irrelevant to the behavior most of
// these tests exercise.
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
func testSandboxSpec() achv1alpha1.ProfileSandboxSpec {
	return achv1alpha1.ProfileSandboxSpec{OpenShell: achv1alpha1.OpenShellSpec{Image: "registry.test/exec:0.1.0"}}
}

// testStandalone is a standalone capability source without any Secret.
func testStandalone() *achv1alpha1.StandaloneSpec {
	return &achv1alpha1.StandaloneSpec{Model: achv1alpha1.StandaloneModelSpec{
		Name: "m", API: "openai", BaseURL: "https://model.example.com/v1",
	}}
}

// testHeaderStandalone has one model header and one MCP header, both from Secrets.
func testHeaderStandalone() *achv1alpha1.StandaloneSpec {
	return &achv1alpha1.StandaloneSpec{
		Model: achv1alpha1.StandaloneModelSpec{
			Name: "m", API: "openai", BaseURL: "https://model.example.com/v1",
			Headers: map[string]achv1alpha1.HeaderValue{"x-litellm-api-key": {SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "litellm", Key: "apiKey"}}},
		},
		McpServers: []achv1alpha1.StandaloneMcpServerSpec{{
			Name: "serper", URL: "https://mcp.serper.dev/mcp",
			Headers: map[string]achv1alpha1.HeaderValue{"X-API-KEY": {SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "serper", Key: "apiKey"}}},
		}},
	}
}

func testProfile(name string) *achv1alpha1.AgentProfile {
	return &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentProfileSpec{
			Control: achv1alpha1.ControlSpec{Image: "registry.test/control:0.1.0", TerminationGracePeriodSeconds: ptrInt64(330)},
			Sandbox: testSandboxSpec(),
			Defaults: achv1alpha1.AgentDefaults{
				Limits: testLimitsSpec(30), Workspace: testWorkspaceSpec(), Artifacts: testArtifactsSpec(), Engine: testEngineSpec(),
			},
		},
	}
}

func testAgentObject(name, profile string) *achv1alpha1.Agent {
	return &achv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace},
		Spec: achv1alpha1.AgentSpec{
			ProfileRef:       achv1alpha1.LocalObjectRef{Name: profile},
			CapabilityServer: "standalone",
			Standalone:       testStandalone(),
			Channels:         []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	}
}

func boolPtr(v bool) *bool { return &v }

func mustApply(t *testing.T, ctx context.Context, obj client.Object) {
	t.Helper()
	if err := k8sClient.Create(ctx, obj); err != nil {
		t.Fatalf("create %T %q: %v", obj, obj.GetName(), err)
	}
}

func secret(name string, data map[string]string) *corev1.Secret {
	s := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: WatchNamespace}, Data: map[string][]byte{}}
	for k, v := range data {
		s.Data[k] = []byte(v)
	}
	return s
}

func getAgent(t *testing.T, ctx context.Context, name string) achv1alpha1.Agent {
	t.Helper()
	var a achv1alpha1.Agent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: name}, &a); err != nil {
		t.Fatalf("get agent %q: %v", name, err)
	}
	return a
}

// getControlStatefulSet gets the control StatefulSet by its name (agent-<agent name>).
func getControlStatefulSet(t *testing.T, ctx context.Context, agentName string) appsv1.StatefulSet {
	t.Helper()
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(agentName)}, &sts); err != nil {
		t.Fatalf("get control statefulset for %q: %v", agentName, err)
	}
	return sts
}

// waitAgentCond polls the named Agent until condType reaches want (or ~10s) and returns it.
func waitAgentCond(t *testing.T, ctx context.Context, name, condType string, want metav1.ConditionStatus) *metav1.Condition {
	t.Helper()
	var last *metav1.Condition
	ok := Eventually(func() bool {
		var a achv1alpha1.Agent
		if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: name}, &a); err != nil {
			return false
		}
		c := apimeta.FindStatusCondition(a.Status.Conditions, condType)
		if c == nil {
			return false
		}
		last = c.DeepCopy()
		return c.Status == want
	}, 10*time.Second, 200*time.Millisecond)
	if !ok {
		if last == nil {
			t.Fatalf("Agent %q condition %q = <none>, want %v", name, condType, want)
		}
		t.Fatalf("Agent %q condition %q = %v (reason=%s msg=%q), want %v", name, condType, last.Status, last.Reason, last.Message, want)
	}
	return last
}

func TestAgent_HappyPath_Standalone(t *testing.T) {
	ctx := context.Background()
	const name = "aa-happy"
	mustApply(t, ctx, secret("litellm", map[string]string{"apiKey": "k1"}))
	mustApply(t, ctx, secret("serper", map[string]string{"apiKey": "k2"}))
	mustApply(t, ctx, testProfile("aa-happy-p"))
	a := testAgentObject(name, "aa-happy-p")
	a.Spec.Standalone = testHeaderStandalone()
	mustApply(t, ctx, a)

	waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionTrue)
	agent := getAgent(t, ctx, name)

	var cm corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(name) + "-config"}, &cm); err != nil {
		t.Fatalf("get config map: %v", err)
	}
	var cfg map[string]any
	if err := json.Unmarshal([]byte(cm.Data["config.json"]), &cfg); err != nil {
		t.Fatalf("config.json is not JSON: %v", err)
	}
	if cfg["schemaVersion"] != "workspace-v3" {
		t.Errorf("schemaVersion = %v, want workspace-v3", cfg["schemaVersion"])
	}

	var headless corev1.Service
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(name)}, &headless); err != nil {
		t.Fatalf("get headless service: %v", err)
	}
	if !headless.Spec.PublishNotReadyAddresses || headless.Spec.ClusterIP != corev1.ClusterIPNone ||
		len(headless.Spec.Ports) != 1 || headless.Spec.Ports[0].Port != 8081 {
		t.Errorf("headless service = %+v, want None/publishNotReady on 8081", headless.Spec)
	}

	var key corev1.Secret
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: WatchNamespace, Name: "ach-agent-key-" + string(agent.UID)}, &key); err != nil {
		t.Fatalf("get key secret: %v", err)
	}
	if len(key.Data["key"]) != 64 {
		t.Errorf("key length = %d, want 64 hex chars", len(key.Data["key"]))
	}
	if owner := metav1.GetControllerOf(&key); owner == nil || owner.UID != agent.UID {
		t.Errorf("key secret controller = %+v, want the Agent", owner)
	}

	hasFinalizer := false
	for _, f := range agent.Finalizers {
		hasFinalizer = hasFinalizer || f == "ach-agent.ackstorm.ai/sandboxes"
	}
	if !hasFinalizer {
		t.Errorf("finalizers = %v, want ach-agent.ackstorm.ai/sandboxes", agent.Finalizers)
	}

	sts := getControlStatefulSet(t, ctx, name)
	if sts.Spec.Template.Labels["istio.io/dataplane-mode"] != "ambient" {
		t.Errorf("pod labels = %v, want ambient", sts.Spec.Template.Labels)
	}
}

func TestAgent_MissingHeaderSecret_SecretMissing(t *testing.T) {
	ctx := context.Background()
	const name = "aa-missing-secret"
	mustApply(t, ctx, secret("litellm-ms", map[string]string{"apiKey": "k1"}))
	mustApply(t, ctx, testProfile("aa-ms-p"))
	a := testAgentObject(name, "aa-ms-p")
	a.Spec.Standalone = testHeaderStandalone()
	a.Spec.Standalone.Model.Headers["x-litellm-api-key"] = achv1alpha1.HeaderValue{SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "litellm-ms", Key: "apiKey"}}
	a.Spec.Standalone.McpServers[0].Headers["X-API-KEY"] = achv1alpha1.HeaderValue{SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "serper-ms", Key: "apiKey"}}
	mustApply(t, ctx, a)

	c := waitAgentCond(t, ctx, name, condSecretsResolved, metav1.ConditionFalse)
	if c.Reason != "SecretMissing" || c.Message != "secret serper-ms not found" {
		t.Errorf("SecretsResolved = %s/%q, want SecretMissing / secret serper-ms not found", c.Reason, c.Message)
	}
	ready := waitAgentCond(t, ctx, name, condReady, metav1.ConditionFalse)
	if ready.Reason != "SecretMissing" {
		t.Errorf("Ready reason = %s, want SecretMissing", ready.Reason)
	}
	// Creating the Secret re-enqueues the Agent through the Secret watch.
	mustApply(t, ctx, secret("serper-ms", map[string]string{"apiKey": "k2"}))
	waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionTrue)
}

func TestAgent_ForeignKeySecret_SecretConflict(t *testing.T) {
	ctx := context.Background()
	const name = "aa-foreign-key"
	// The Agent's UID is only known after create; the profile does not exist yet, so the
	// reconciler stops before it would create the key Secret.
	mustApply(t, ctx, testAgentObject(name, "aa-fk-p"))
	agent := getAgent(t, ctx, name)
	mustApply(t, ctx, secret("ach-agent-key-"+string(agent.UID), map[string]string{"key": "foreign"}))
	mustApply(t, ctx, testProfile("aa-fk-p"))

	c := waitAgentCond(t, ctx, name, condSecretsResolved, metav1.ConditionFalse)
	if c.Reason != "SecretConflict" {
		t.Errorf("SecretsResolved reason = %s, want SecretConflict", c.Reason)
	}
}

func TestAgent_SecretChangeRollsPodTemplate(t *testing.T) {
	ctx := context.Background()
	const name = "aa-roll"
	litellm := secret("litellm-roll", map[string]string{"apiKey": "k1"})
	mustApply(t, ctx, litellm)
	mustApply(t, ctx, testProfile("aa-roll-p"))
	a := testAgentObject(name, "aa-roll-p")
	a.Spec.Standalone = testStandalone()
	a.Spec.Standalone.Model.Headers = map[string]achv1alpha1.HeaderValue{"x-key": {SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "litellm-roll", Key: "apiKey"}}}
	mustApply(t, ctx, a)
	waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionTrue)
	before := getControlStatefulSet(t, ctx, name).Spec.Template.Annotations["ach-agent.ackstorm.ai/config-hash"]
	if before == "" {
		t.Fatal("config-hash annotation missing")
	}

	litellm.Data["apiKey"] = []byte("k2")
	if err := k8sClient.Update(ctx, litellm); err != nil {
		t.Fatal(err)
	}
	if !Eventually(func() bool {
		return getControlStatefulSet(t, ctx, name).Spec.Template.Annotations["ach-agent.ackstorm.ai/config-hash"] != before
	}, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("config-hash did not change after the header Secret changed")
	}
}

func TestAgent_InvalidGracePeriod(t *testing.T) {
	ctx := context.Background()
	const name = "aa-grace"
	p := testProfile("aa-grace-p")
	p.Spec.Control.TerminationGracePeriodSeconds = ptrInt64(30) // workspace shutdown is 300
	mustApply(t, ctx, p)
	mustApply(t, ctx, testAgentObject(name, "aa-grace-p"))
	c := waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionFalse)
	if c.Reason != "InvalidGracePeriod" {
		t.Errorf("reason = %s, want InvalidGracePeriod", c.Reason)
	}
	if ready := waitAgentCond(t, ctx, name, condReady, metav1.ConditionFalse); ready.Reason != "InvalidGracePeriod" {
		t.Errorf("Ready reason = %s, want InvalidGracePeriod", ready.Reason)
	}
}

func TestAgent_MissingProfile_ProfileResolvedFalse(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, testAgentObject("aa-noprofile", "does-not-exist"))
	c := waitAgentCond(t, ctx, "aa-noprofile", condProfileResolved, metav1.ConditionFalse)
	if c.Reason != "ProfileNotFound" {
		t.Errorf("reason = %s, want ProfileNotFound", c.Reason)
	}
}

func TestAgent_ProfileDeletedAfterApplied_ReadyFlipsFalse(t *testing.T) {
	ctx := context.Background()
	const name = "aa-profile-gone"
	p := testProfile("aa-pg-p")
	mustApply(t, ctx, p)
	mustApply(t, ctx, testAgentObject(name, "aa-pg-p"))
	waitAgentCond(t, ctx, name, condWorkloadApplied, metav1.ConditionTrue)
	if err := k8sClient.Delete(ctx, p); err != nil {
		t.Fatal(err)
	}
	waitAgentCond(t, ctx, name, condProfileResolved, metav1.ConditionFalse)
	waitAgentCond(t, ctx, name, condReady, metav1.ConditionFalse)
}

func TestAgent_ExposeServiceDisabled_PrunesService(t *testing.T) {
	ctx := context.Background()
	const name = "aa-expose"
	mustApply(t, ctx, testProfile("aa-expose-p"))
	a := testAgentObject(name, "aa-expose-p")
	a.Spec.Expose = &achv1alpha1.ExposeSpec{Service: true}
	mustApply(t, ctx, a)
	svcKey := types.NamespacedName{Namespace: WatchNamespace, Name: render.ControlName(name) + "-http"}
	if !Eventually(func() bool { return k8sClient.Get(ctx, svcKey, &corev1.Service{}) == nil }, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("-http Service not created for expose.service")
	}
	cur := getAgent(t, ctx, name)
	cur.Spec.Expose = nil
	if err := k8sClient.Update(ctx, &cur); err != nil {
		t.Fatal(err)
	}
	if !Eventually(func() bool { return apierrors.IsNotFound(k8sClient.Get(ctx, svcKey, &corev1.Service{})) }, 10*time.Second, 200*time.Millisecond) {
		t.Fatal("-http Service must be pruned once expose.service is dropped")
	}
}

func TestAgent_CEL_Rejections(t *testing.T) {
	ctx := context.Background()
	mustApply(t, ctx, testProfile("aa-cel-p"))
	for name, mutate := range map[string]func(*achv1alpha1.Agent){
		"both servers": func(a *achv1alpha1.Agent) {
			a.Spec.Ach = &achv1alpha1.AchServerSpec{BaseURL: "https://a", KeySecretRef: achv1alpha1.SecretKeyRef{Name: "k", Key: "k"}, Model: achv1alpha1.AchModelSpec{Name: "m"}}
		},
		"ach selected without ach block": func(a *achv1alpha1.Agent) {
			a.Spec.CapabilityServer = "ach"
		},
		"route without service": func(a *achv1alpha1.Agent) {
			a.Spec.Expose = &achv1alpha1.ExposeSpec{Route: &achv1alpha1.RouteSpec{Hostname: "a.example.com"}}
		},
		"skills exclusion in standalone": func(a *achv1alpha1.Agent) {
			a.Spec.Filter = &achv1alpha1.FilterSpec{Exclude: &achv1alpha1.ExcludeSpec{Skills: []string{"s"}}}
		},
		"header with both secretKeyRef and value": func(a *achv1alpha1.Agent) {
			v := "x"
			a.Spec.Standalone.Model.Headers = map[string]achv1alpha1.HeaderValue{"x-a": {Value: &v, SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "s", Key: "k"}}}
		},
		"header with neither": func(a *achv1alpha1.Agent) {
			a.Spec.Standalone.Model.Headers = map[string]achv1alpha1.HeaderValue{"x-a": {}}
		},
	} {
		t.Run(name, func(t *testing.T) {
			a := testAgentObject("aa-cel-bad", "aa-cel-p")
			mutate(a)
			if err := k8sClient.Create(ctx, a); err == nil {
				_ = k8sClient.Delete(ctx, a)
				t.Fatal("expected the apiserver to reject the Agent")
			}
		})
	}
	t.Run("route with service is accepted", func(t *testing.T) {
		a := testAgentObject("aa-cel-route-ok", "aa-cel-p")
		a.Spec.Expose = &achv1alpha1.ExposeSpec{Service: true, Route: &achv1alpha1.RouteSpec{Hostname: "a.example.com"}}
		if err := k8sClient.Create(ctx, a); err != nil {
			t.Fatalf("route + service must be accepted: %v", err)
		}
	})
}
