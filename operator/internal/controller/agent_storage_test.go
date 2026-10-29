// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"sort"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	clientfake "sigs.k8s.io/controller-runtime/pkg/client/fake"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// TestRuntimeStorageControlEnv compares the merged control env against the reconciler's
// actual override wiring (buildAgentEnv then overrideEnv(render.RuntimeStorageEnv)):
// the four backend names appear exactly once, the global options win over any conflicting
// entry already present at those exact names, unrelated entries survive untouched, and
// building without the override (the all-disabled path) appends nothing.
func TestRuntimeStorageControlEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	a.Spec.Env = []corev1.EnvVar{{Name: "MY_CUSTOM_VAR", Value: "keep-me"}}
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"

	base := buildAgentEnv(a, p, nil)
	// Simulate a stale/conflicting entry already at a storage env name (the reconciler
	// overrides at these exact names, regardless of how they got there).
	base = append(base, corev1.EnvVar{Name: "ACH_STORAGE_S3_BUCKET", Value: "stale-should-be-overridden"})

	options := render.RuntimeStorageOptions{Bucket: "runtime-bucket", Region: "us-east-1"}
	merged := overrideEnv(base, render.RuntimeStorageEnv(options))

	counts := map[string]int{}
	for _, e := range merged {
		counts[e.Name]++
	}
	for _, name := range []string{"ACH_STORAGE_S3_BUCKET", "ACH_STORAGE_S3_REGION", "ACH_STORAGE_S3_ENDPOINT_URL", "ACH_STORAGE_S3_PREFIX"} {
		if counts[name] != 1 {
			t.Errorf("env %q appears %d times, want exactly 1", name, counts[name])
		}
	}
	for _, e := range merged {
		switch e.Name {
		case "ACH_STORAGE_S3_BUCKET":
			if e.Value != "runtime-bucket" {
				t.Errorf("ACH_STORAGE_S3_BUCKET = %q, want global override %q (global authority)", e.Value, "runtime-bucket")
			}
		case "MY_CUSTOM_VAR":
			if e.Value != "keep-me" {
				t.Errorf("unrelated entry MY_CUSTOM_VAR = %q, want untouched %q", e.Value, "keep-me")
			}
		}
	}
	hasCustom := false
	for _, e := range merged {
		if e.Name == "MY_CUSTOM_VAR" {
			hasCustom = true
		}
	}
	if !hasCustom {
		t.Error("unrelated env entry MY_CUSTOM_VAR dropped by override")
	}

	// All-disabled: the reconciler never calls overrideEnv at all, so buildAgentEnv's own
	// output alone must carry none of the four names.
	for _, e := range buildAgentEnv(a, p, nil) {
		if strings.HasPrefix(e.Name, "ACH_STORAGE_S3_") {
			t.Errorf("buildAgentEnv alone (all-disabled path) must carry no ACH_STORAGE_S3_* names, got %q", e.Name)
		}
	}
}

// minimalProfileSpec builds a complete AgentProfileSpec (every Render-required block
// resolved, reusing the package's existing envtest fixtures) with workspace persistence set
// by persist — the single dimension TestRuntimeStorageSecretHashAndEnqueue/agentsForSecret
// coverage below needs to flip RuntimeRequiresStorage.
func minimalProfileSpec(persist bool) achv1alpha1.AgentProfileSpec {
	ws := testWorkspaceSpec()
	ws.Persistence.Enabled = boolPtr(persist)
	return achv1alpha1.AgentProfileSpec{
		Control: achv1alpha1.ControlSpec{Image: "img:test", TerminationGracePeriodSeconds: ptrInt64(330)},
		Defaults: achv1alpha1.AgentDefaults{
			Limits: testLimitsSpec(10), Workspace: ws, Artifacts: testArtifactsSpec(), Engine: testEngineSpec(),
		},
		Sandbox: testSandboxSpec(),
	}
}

func newFakeStorageReconciler(t *testing.T, options render.RuntimeStorageOptions, objs ...client.Object) *AgentReconciler {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := achv1alpha1.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	if err := clientgoscheme.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	c := clientfake.NewClientBuilder().WithScheme(scheme).WithObjects(objs...).Build()
	return &AgentReconciler{Client: c, APIReader: c, Scheme: scheme, Storage: options}
}

// TestRuntimeStorageSecretHashAndEnqueue exercises the actual hashSecrets method (never a
// reimplementation) against isolated fake-client reconciler instances: a required AWS key
// rotation, and the optional session token's addition/change/removal, must each change the
// salted rollout hash — no token is ever required for hashSecrets to succeed.
func TestRuntimeStorageSecretHashAndEnqueue(t *testing.T) {
	const ns = "ns"
	a := achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "demo", Namespace: ns}}
	a.Spec.ProfileRef = achv1alpha1.LocalObjectRef{Name: "p"}
	a.UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"

	identity := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "ek", Namespace: ns}, Data: map[string][]byte{"ek": []byte("v1")}}
	options := render.RuntimeStorageOptions{Bucket: "runtime-bucket", CredentialsSecretName: "runtime-s3"}
	hashInput := mergeSecretKeys(map[string][]string{}, options.CredentialsSecretName, allStorageCredentialKeys)

	hashWith := func(data map[string][]byte) string {
		t.Helper()
		cred := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "runtime-s3", Namespace: ns}, Data: data}
		r := newFakeStorageReconciler(t, options, identity, cred)
		h, err := r.hashSecrets(context.Background(), &a, hashInput)
		if err != nil {
			t.Fatalf("hashSecrets: %v", err)
		}
		return h
	}

	noToken := hashWith(map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak1"), "AWS_SECRET_ACCESS_KEY": []byte("sk1")})
	rotatedKey := hashWith(map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak2"), "AWS_SECRET_ACCESS_KEY": []byte("sk1")})
	withToken := hashWith(map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak1"), "AWS_SECRET_ACCESS_KEY": []byte("sk1"), "AWS_SESSION_TOKEN": []byte("tok1")})
	changedToken := hashWith(map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak1"), "AWS_SECRET_ACCESS_KEY": []byte("sk1"), "AWS_SESSION_TOKEN": []byte("tok2")})

	if rotatedKey == noToken {
		t.Error("required AWS_ACCESS_KEY_ID rotation did not change the rollout hash")
	}
	if withToken == noToken {
		t.Error("optional session-token addition did not change the rollout hash (token removal must be observable)")
	}
	if changedToken == withToken {
		t.Error("session-token change did not change the rollout hash")
	}
	if _, err := newFakeStorageReconciler(t, options, identity, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "runtime-s3", Namespace: ns}, Data: map[string][]byte{"AWS_ACCESS_KEY_ID": []byte("ak1"), "AWS_SECRET_ACCESS_KEY": []byte("sk1")}}).hashSecrets(context.Background(), &a, hashInput); err != nil {
		t.Errorf("hashSecrets must succeed with no session token present (it is optional): %v", err)
	}
}

// TestAgentsForSecret_GlobalStorageCredential covers the Task2 D1 amendment's agentsForSecret
// extension: the shared namespace-local credentials Secret only re-enqueues an agent whose
// resolved Render2 config actually requires storage — a volatile (all-disabled) agent on the
// same Secret must not become a storage consumer, and an unrelated Secret matches nothing.
func TestAgentsForSecret_GlobalStorageCredential(t *testing.T) {
	const ns = "ns"
	volatileProfile := &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "p-volatile", Namespace: ns}, Spec: minimalProfileSpec(false)}
	persistentProfile := &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "p-persist", Namespace: ns}, Spec: minimalProfileSpec(true)}
	volatileAgent := &achv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "a-volatile", Namespace: ns, UID: "11111111-1111-4111-8111-111111111111"},
		Spec:       achv1alpha1.AgentSpec{ProfileRef: achv1alpha1.LocalObjectRef{Name: "p-volatile"}, CapabilityServer: "standalone", Standalone: testStandalone()},
	}
	persistentAgent := &achv1alpha1.Agent{
		ObjectMeta: metav1.ObjectMeta{Name: "a-persist", Namespace: ns, UID: "22222222-2222-4222-8222-222222222222"},
		Spec:       achv1alpha1.AgentSpec{ProfileRef: achv1alpha1.LocalObjectRef{Name: "p-persist"}, CapabilityServer: "standalone", Standalone: testStandalone()},
	}

	options := render.RuntimeStorageOptions{Bucket: "runtime-bucket", CredentialsSecretName: "runtime-s3"}
	r := newFakeStorageReconciler(t, options, volatileProfile, persistentProfile, volatileAgent, persistentAgent)

	for _, tc := range []struct {
		profile *achv1alpha1.AgentProfile
		agent   *achv1alpha1.Agent
		want    bool
	}{
		{volatileProfile, volatileAgent, false},
		{persistentProfile, persistentAgent, true},
	} {
		cfg, _, err := render.Render(*tc.profile, *tc.agent)
		if err != nil {
			t.Fatalf("Render(%s): %v", tc.agent.Name, err)
		}
		if got := render.RuntimeRequiresStorage(cfg); got != tc.want {
			t.Errorf("RuntimeRequiresStorage(%s) = %v, want %v", tc.agent.Name, got, tc.want)
		}
	}

	credSecretMeta := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "runtime-s3", Namespace: ns}}
	reqs := r.agentsForSecret(context.Background(), credSecretMeta)
	names := make([]string, 0, len(reqs))
	for _, req := range reqs {
		names = append(names, req.Name)
	}
	sort.Strings(names)
	if len(names) != 1 || names[0] != "a-persist" {
		t.Errorf("agentsForSecret(global cred secret) = %v, want exactly [a-persist] (volatile agent must not become a storage consumer)", names)
	}

	unrelated := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "unrelated", Namespace: ns}}
	if reqs := r.agentsForSecret(context.Background(), unrelated); len(reqs) != 0 {
		t.Errorf("agentsForSecret(unrelated secret) = %v, want none", reqs)
	}
}
