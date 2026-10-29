// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// isolatedStorageNamespace creates a private namespace the shared suite manager's cache
// never watches (its informer cache is scoped to WatchNamespace only, suite_test.go) — so a
// test here can drive its own ACHAgentReconciler with its own RuntimeStorageOptions by
// calling Reconcile() directly against the real envtest apiserver, with zero risk of racing
// the shared manager's goroutine reconciling the same object with the suite's fixed options
// (Task2 D1 amendment: "do not mutate a running shared manager").
func isolatedStorageNamespace(t *testing.T, name string) {
	t.Helper()
	ctx := context.Background()
	if err := k8sClient.Create(ctx, &corev1.Namespace{ObjectMeta: metav1.ObjectMeta{Name: name}}); err != nil && !apierrors.IsAlreadyExists(err) {
		t.Fatalf("create namespace %q: %v", name, err)
	}
}

// isolatedACHAgentReconciler builds a fresh, one-off reconciler against the shared envtest
// apiserver (same cfg/scheme as the suite manager) with its own RuntimeStorageOptions.
func isolatedACHAgentReconciler(t *testing.T, options render.RuntimeStorageOptions) *ACHAgentReconciler {
	t.Helper()
	c, err := client.New(cfg, client.Options{Scheme: k8sClient.Scheme()})
	if err != nil {
		t.Fatalf("isolated client: %v", err)
	}
	return &ACHAgentReconciler{
		Client: c, APIReader: c, Scheme: k8sClient.Scheme(),
		DefaultAchBaseURL:     "https://ach",
		RuntimeStorageOptions: options,
	}
}

// reconcileOnce drives one synchronous Reconcile() call (never the shared manager's async
// watch loop) and returns the resulting ACHAgent.
func reconcileOnce(t *testing.T, r *ACHAgentReconciler, ns, name string) achv1alpha1.ACHAgent {
	t.Helper()
	ctx := context.Background()
	if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: types.NamespacedName{Namespace: ns, Name: name}}); err != nil {
		t.Fatalf("Reconcile: %v", err)
	}
	var a achv1alpha1.ACHAgent
	if err := r.Client.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &a); err != nil {
		t.Fatalf("get achagent after reconcile: %v", err)
	}
	return a
}

// getControlStatefulSetIn is getControlStatefulSet for an arbitrary namespace — the package
// helper hardcodes WatchNamespace, which the isolated-namespace tests here never use.
func getControlStatefulSetIn(t *testing.T, ns, name string) appsv1.StatefulSet {
	t.Helper()
	ctx := context.Background()
	var a achv1alpha1.ACHAgent
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &a); err != nil {
		t.Fatalf("get achagent %q: %v", name, err)
	}
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: ns, Name: render.ControlName(a.Name)}, &sts); err != nil {
		t.Fatalf("get control statefulset for %q: %v", name, err)
	}
	return sts
}

// TestACHAgent_GlobalStorageConditional covers each storage-requiring domain missing the
// operator bucket (WorkloadApplied=False/reason StorageUnavailable, before control is ever
// launched), the all-disabled/no-bucket path (applied), the configured-bucket path (applied,
// private env including optional endpoint/prefix — never cloud reachability), and an agent
// explicitly overriding an inherited profile-true persistence back to false.
func TestACHAgent_GlobalStorageConditional(t *testing.T) {
	const ns = "aa-storage-cond"
	isolatedStorageNamespace(t, ns)
	ctx := context.Background()

	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "ek", Namespace: ns}, Data: map[string][]byte{"ek": []byte("v")}})

	newProfile := func(name string, mutate func(*achv1alpha1.AgentProfileSpec)) {
		spec := minimalProfileSpec(false)
		if mutate != nil {
			mutate(&spec)
		}
		mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Spec: spec})
	}
	newAgent := func(name, profile string, mutate func(*achv1alpha1.ACHAgentSpec)) {
		spec := achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: profile},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("ek", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		}
		if mutate != nil {
			mutate(&spec)
		}
		mustApply(t, ctx, &achv1alpha1.ACHAgent{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Spec: spec})
	}

	// Each enabled domain, missing bucket -> StorageUnavailable, before control launches.
	newProfile("p-ws", func(s *achv1alpha1.AgentProfileSpec) { s.Achagent.Workspace.Persistence.Enabled = boolPtr(true) })
	newAgent("a-ws", "p-ws", nil)
	newProfile("p-sess", func(s *achv1alpha1.AgentProfileSpec) {
		s.Achagent.Workspace.Session.Persistence.Enabled = boolPtr(true)
	})
	newAgent("a-sess", "p-sess", nil)
	newProfile("p-art", func(s *achv1alpha1.AgentProfileSpec) { s.Achagent.Artifacts.Enabled = boolPtr(true) })
	newAgent("a-art", "p-art", nil)

	emptyBucket := isolatedACHAgentReconciler(t, render.RuntimeStorageOptions{})
	for _, name := range []string{"a-ws", "a-sess", "a-art"} {
		a := reconcileOnce(t, emptyBucket, ns, name)
		c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied)
		if c == nil || c.Status != metav1.ConditionFalse || c.Reason != "StorageUnavailable" {
			t.Errorf("%s: WorkloadApplied = %+v, want False/StorageUnavailable", name, c)
		}
		if ready := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadReady); ready != nil && ready.Status == metav1.ConditionTrue {
			t.Errorf("%s: control must never launch when storage is unavailable", name)
		}
	}

	// All-disabled, no bucket -> applied.
	newProfile("p-none", nil)
	newAgent("a-none", "p-none", nil)
	a := reconcileOnce(t, emptyBucket, ns, "a-none")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied); c == nil || c.Status != metav1.ConditionTrue {
		t.Errorf("a-none: WorkloadApplied = %+v, want True (no domain requires storage)", c)
	}

	// Configured bucket -> applied, private env on the control pod including optional
	// endpoint/prefix — never cloud reachability tested here.
	configured := isolatedACHAgentReconciler(t, render.RuntimeStorageOptions{
		Bucket: "runtime-bucket", Region: "us-east-1", EndpointURL: "http://seaweedfs:8333", Prefix: "tenant/runtime",
	})
	a = reconcileOnce(t, configured, ns, "a-ws")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("a-ws with configured bucket: WorkloadApplied = %+v, want True", c)
	}
	sts := getControlStatefulSetIn(t, ns, "a-ws")
	wantEnv := map[string]string{
		"ACH_STORAGE_S3_BUCKET": "runtime-bucket", "ACH_STORAGE_S3_REGION": "us-east-1",
		"ACH_STORAGE_S3_ENDPOINT_URL": "http://seaweedfs:8333", "ACH_STORAGE_S3_PREFIX": "tenant/runtime",
	}
	for k, want := range wantEnv {
		found := false
		for _, e := range sts.Spec.Template.Spec.Containers[0].Env {
			if e.Name == k {
				found = true
				if e.Value != want {
					t.Errorf("control env %s = %q, want %q", k, e.Value, want)
				}
			}
		}
		if !found {
			t.Errorf("control env missing %s", k)
		}
	}

	// Inherited profile true, explicit agent false -> storage no longer required.
	newAgent("a-ws-override", "p-ws", func(s *achv1alpha1.ACHAgentSpec) {
		s.Workspace = &achv1alpha1.WorkspaceSpec{Persistence: &achv1alpha1.WorkspacePersistenceSpec{Enabled: boolPtr(false)}}
	})
	a = reconcileOnce(t, emptyBucket, ns, "a-ws-override")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied); c == nil || c.Status != metav1.ConditionTrue {
		t.Errorf("a-ws-override: WorkloadApplied = %+v, want True (agent override clears the inherited requirement)", c)
	}
}

// TestACHAgent_GlobalStorageCredentials covers the namespace-local credentials Secret gate:
// missing Secret and missing required key both block launch; a valid two-key Secret launches
// without a token; the optional token's add/change/removal each change the rollout
// annotation; and config.json never carries private backend settings or credential values.
func TestACHAgent_GlobalStorageCredentials(t *testing.T) {
	const ns = "aa-storage-cred"
	isolatedStorageNamespace(t, ns)
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "ek", Namespace: ns}, Data: map[string][]byte{"ek": []byte("v")}})

	spec := minimalProfileSpec(true) // workspace persistence enabled -> storage required
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: ns}, Spec: spec})
	mustApply(t, ctx, &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: ns},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef:    achv1alpha1.LocalObjectRef{Name: "p"},
			AgentDefaults: achv1alpha1.AgentDefaults{Ach: envtestIdentity("ek", "prod")},
			Channels:      []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
		},
	})

	options := render.RuntimeStorageOptions{Bucket: "runtime-bucket", CredentialsSecretName: "runtime-s3"}
	r := isolatedACHAgentReconciler(t, options)

	// Missing Secret entirely -> blocked.
	a := reconcileOnce(t, r, ns, "a")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condChannelSecretsResolved); c == nil || c.Status != metav1.ConditionFalse {
		t.Fatalf("missing credentials secret: ChannelSecretsResolved = %+v, want False", c)
	}

	// Secret present but missing a required key -> still blocked.
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "runtime-s3", Namespace: ns}, Data: map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak1")}})
	a = reconcileOnce(t, r, ns, "a")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condChannelSecretsResolved); c == nil || c.Status != metav1.ConditionFalse || c.Reason != "ChannelSecretKeyMissing" {
		t.Fatalf("missing required key: ChannelSecretsResolved = %+v, want False/ChannelSecretKeyMissing", c)
	}

	credName := types.NamespacedName{Namespace: ns, Name: "runtime-s3"}
	var cred corev1.Secret
	if err := k8sClient.Get(ctx, credName, &cred); err != nil {
		t.Fatal(err)
	}

	// Valid two-key Secret, no token -> launches.
	cred.Data["AWS_SECRET_ACCESS_KEY"] = []byte("sk1")
	if err := k8sClient.Update(ctx, &cred); err != nil {
		t.Fatal(err)
	}
	a = reconcileOnce(t, r, ns, "a")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("valid two-key secret, no token: WorkloadApplied = %+v, want True", c)
	}
	sts := getControlStatefulSetIn(t, ns, "a")
	hashNoToken := sts.Spec.Template.Annotations[configHashAnnotation]
	assertControlAWSRefs(t, sts.Spec.Template.Spec.Containers[0].Env)

	// Add optional token -> rollout annotation changes.
	cred.Data["AWS_SESSION_TOKEN"] = []byte("tok1")
	if err := k8sClient.Update(ctx, &cred); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, ns, "a")
	sts = getControlStatefulSetIn(t, ns, "a")
	hashWithToken := sts.Spec.Template.Annotations[configHashAnnotation]
	if hashWithToken == hashNoToken {
		t.Error("adding the optional session token did not change the rollout hash")
	}
	assertControlAWSRefs(t, sts.Spec.Template.Spec.Containers[0].Env)

	// Change token -> rollout annotation changes again.
	cred.Data["AWS_SESSION_TOKEN"] = []byte("tok2")
	if err := k8sClient.Update(ctx, &cred); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, ns, "a")
	sts = getControlStatefulSetIn(t, ns, "a")
	hashChangedToken := sts.Spec.Template.Annotations[configHashAnnotation]
	if hashChangedToken == hashWithToken {
		t.Error("changing the session token did not change the rollout hash")
	}

	// Remove token -> rollout annotation reverts to the no-token hash.
	delete(cred.Data, "AWS_SESSION_TOKEN")
	if err := k8sClient.Update(ctx, &cred); err != nil {
		t.Fatal(err)
	}
	reconcileOnce(t, r, ns, "a")
	sts = getControlStatefulSetIn(t, ns, "a")
	hashRemovedToken := sts.Spec.Template.Annotations[configHashAnnotation]
	if hashRemovedToken != hashNoToken {
		t.Errorf("removing the session token: hash = %q, want back to the no-token hash %q", hashRemovedToken, hashNoToken)
	}
	assertControlAWSRefs(t, sts.Spec.Template.Spec.Containers[0].Env)

	// config.json carries no private backend settings or credential sentinel values.
	var cm corev1.ConfigMap
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: ns, Name: agentResourceName("a")}, &cm); err != nil {
		t.Fatal(err)
	}
	for _, forbidden := range []string{"ACH_STORAGE_S3_BUCKET", "runtime-bucket", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"} {
		if strings.Contains(cm.Data["config.json"], forbidden) {
			t.Errorf("config.json must never carry private backend settings/credential sentinels, found %q", forbidden)
		}
	}
}

func assertControlAWSRefs(t *testing.T, env []corev1.EnvVar) {
	t.Helper()
	var gotAccess, gotSecret, gotToken bool
	for _, e := range env {
		switch e.Name {
		case "AWS_ACCESS_KEY_ID":
			gotAccess = e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Name == "runtime-s3"
		case "AWS_SECRET_ACCESS_KEY":
			gotSecret = e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil && e.ValueFrom.SecretKeyRef.Name == "runtime-s3"
		case "AWS_SESSION_TOKEN":
			if e.Value != "" {
				t.Errorf("control env AWS_SESSION_TOKEN carries a literal value %q, want selector only", e.Value)
			}
			gotToken = e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil &&
				e.ValueFrom.SecretKeyRef.Name == "runtime-s3" && e.ValueFrom.SecretKeyRef.Key == "AWS_SESSION_TOKEN" &&
				e.ValueFrom.SecretKeyRef.Optional != nil && *e.ValueFrom.SecretKeyRef.Optional
		}
	}
	if !gotAccess || !gotSecret {
		t.Error("control env missing required AWS SecretKeyRefs")
	}
	if !gotToken {
		t.Error("control env missing optional AWS_SESSION_TOKEN selector (must always be present, Optional=true)")
	}
}
