// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"encoding/hex"
	"errors"
	"strings"
	"testing"

	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func TestEnsureWorkspaceKey_CreateOnce(t *testing.T) {
	ctx := context.Background()
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}

	if err := r.ensureWorkspaceKey(ctx, a); err != nil {
		t.Fatalf("first ensureWorkspaceKey: %v", err)
	}
	var got corev1.Secret
	name := types.NamespacedName{Namespace: a.Namespace, Name: workspaceKeySecretName(string(a.UID))}
	if err := c.Get(ctx, name, &got); err != nil {
		t.Fatalf("get created Secret: %v", err)
	}
	value, ok := got.Data[workspaceKeyDataKey]
	if !ok || len(value) != 64 {
		t.Fatalf("Secret data[%q] = %q (present=%v), want 64 hexadecimal bytes", workspaceKeyDataKey, value, ok)
	}
	if _, err := hex.DecodeString(string(value)); err != nil {
		t.Fatalf("generated key is not hexadecimal: %v", err)
	}
	if got.Type != corev1.SecretTypeOpaque {
		t.Errorf("Secret type = %q, want Opaque", got.Type)
	}
	if !metav1.IsControlledBy(&got, a) || len(got.OwnerReferences) != 1 {
		t.Errorf("Secret owner references = %+v, want the Agent controller owner", got.OwnerReferences)
	}
	if got.Labels[agentLabelKey] != a.Name {
		t.Errorf("Secret labels = %v, want agent label %q", got.Labels, a.Name)
	}
	firstValue, firstRV := string(value), got.ResourceVersion

	if err := r.ensureWorkspaceKey(ctx, a); err != nil {
		t.Fatalf("second ensureWorkspaceKey: %v", err)
	}
	var second corev1.Secret
	if err := c.Get(ctx, name, &second); err != nil {
		t.Fatalf("get retained Secret: %v", err)
	}
	if string(second.Data[workspaceKeyDataKey]) != firstValue {
		t.Fatal("second ensure changed key bytes")
	}
	if second.ResourceVersion != firstRV {
		t.Errorf("second ensure changed resourceVersion from %q to %q", firstRV, second.ResourceVersion)
	}
}

func TestEnsureWorkspaceKey_AcceptsExistingOwnedNonemptyLiteral(t *testing.T) {
	ctx := context.Background()
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	const supplied = "operator-supplied-not-format-checked"
	s := workspaceKeyTestSecret(t, a, supplied)
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(s).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}

	if err := r.ensureWorkspaceKey(ctx, a); err != nil {
		t.Fatalf("ensureWorkspaceKey: %v", err)
	}
	var got corev1.Secret
	if err := c.Get(ctx, client.ObjectKeyFromObject(s), &got); err != nil {
		t.Fatalf("get Secret: %v", err)
	}
	if string(got.Data[workspaceKeyDataKey]) != supplied || got.ResourceVersion != s.ResourceVersion {
		t.Fatalf("existing key was changed: data=%q rv=%q", got.Data[workspaceKeyDataKey], got.ResourceVersion)
	}
}

func TestEnsureWorkspaceKey_RejectsUnownedForeignAndEmptySecrets(t *testing.T) {
	ctx := context.Background()
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	foreign := workspaceKeyTestAgent("ns", "other", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
	wrongUID := workspaceKeyTestAgent("ns", "agent", "99999999-2222-4333-8444-555555555555")
	cases := []struct {
		name string
		make func() *corev1.Secret
	}{
		{name: "unowned", make: func() *corev1.Secret {
			return &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: workspaceKeySecretName(string(a.UID)), Namespace: a.Namespace}, Data: map[string][]byte{workspaceKeyDataKey: []byte("private-value")}}
		}},
		{name: "foreign owner", make: func() *corev1.Secret {
			s := workspaceKeyTestSecret(t, foreign, "private-value")
			s.Name = workspaceKeySecretName(string(a.UID))
			return s
		}},
		{name: "same name wrong uid", make: func() *corev1.Secret {
			s := workspaceKeyTestSecret(t, wrongUID, "private-value")
			s.Name = workspaceKeySecretName(string(a.UID))
			return s
		}},
		{name: "missing key", make: func() *corev1.Secret { return workspaceKeyTestSecret(t, a, "") }},
		{name: "missing key field", make: func() *corev1.Secret {
			s := workspaceKeyTestSecret(t, a, "private-value")
			delete(s.Data, workspaceKeyDataKey)
			return s
		}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			s := tc.make()
			scheme := workspaceKeyTestScheme(t)
			c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(s).Build()
			r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}
			err := r.ensureWorkspaceKey(ctx, a)
			if err == nil {
				t.Fatal("ensureWorkspaceKey succeeded; want validation error")
			}
			if !errors.Is(err, errKeySecretConflict) {
				t.Fatalf("err = %v, want errKeySecretConflict", err)
			}
			if strings.Contains(err.Error(), "private-value") {
				t.Fatalf("error exposed Secret data: %v", err)
			}
			var after corev1.Secret
			if getErr := c.Get(ctx, client.ObjectKeyFromObject(s), &after); getErr != nil {
				t.Fatalf("validation mutated/deleted the existing Secret: %v", getErr)
			}
			if after.ResourceVersion != s.ResourceVersion || string(after.Data[workspaceKeyDataKey]) != string(s.Data[workspaceKeyDataKey]) {
				t.Fatal("validation changed the existing Secret")
			}
		})
	}
}

func TestWorkspaceKeySecretChangeRollsPrivateHash(t *testing.T) {
	ctx := context.Background()
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	keyName := workspaceKeySecretName(string(a.UID))
	identity := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "identity", Namespace: a.Namespace}, Data: map[string][]byte{"ek": []byte("ek_test")}}
	key := workspaceKeyTestSecret(t, a, "first-key")
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(identity, key).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}
	secrets := map[string][]string{keyName: {workspaceKeyDataKey}}
	first, err := r.hashSecrets(ctx, a, secrets)
	if err != nil {
		t.Fatalf("first hashSecrets: %v", err)
	}
	key.Data[workspaceKeyDataKey] = []byte("second-key")
	if err := c.Update(ctx, key); err != nil {
		t.Fatalf("update key Secret: %v", err)
	}
	second, err := r.hashSecrets(ctx, a, secrets)
	if err != nil {
		t.Fatalf("second hashSecrets: %v", err)
	}
	if first == second {
		t.Fatal("workspace key change did not change the salted secret hash")
	}
}

func TestAgentsForWorkspaceKeySecretEnqueuesOnlyMatchingNamespaceAndUID(t *testing.T) {
	target := workspaceKeyTestAgent("ns-one", "target", "11111111-2222-4333-8444-555555555555")
	otherUID := workspaceKeyTestAgent("ns-one", "other", "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee")
	otherNamespace := workspaceKeyTestAgent("ns-two", "same-uid", string(target.UID))
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(target, otherUID, otherNamespace).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}
	secret := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Namespace: target.Namespace, Name: workspaceKeySecretName(string(target.UID))}}
	got := r.agentsForSecret(context.Background(), secret)
	want := types.NamespacedName{Namespace: target.Namespace, Name: target.Name}
	if len(got) != 1 || got[0].NamespacedName != want {
		t.Fatalf("agentsForSecret = %v, want only %v", got, want)
	}
}

func TestWorkspaceKeyPrivateControlEnvAndConfigMapBoundary(t *testing.T) {
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "agent:test"
	keyName := workspaceKeySecretName(string(a.UID))
	env := buildAgentEnv(a, p, nil)
	env = overrideEnv(env, []corev1.EnvVar{{Name: "ACH_SANDBOX_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{LocalObjectReference: corev1.LocalObjectReference{Name: keyName}, Key: workspaceKeyDataKey}}}})
	sts := buildStatefulSet(a, p, "hash", env)
	var found int
	for _, e := range sts.Spec.Template.Spec.Containers[0].Env {
		if e.Name != "ACH_SANDBOX_KEY" {
			continue
		}
		found++
		if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil || e.ValueFrom.SecretKeyRef.Name != keyName || e.ValueFrom.SecretKeyRef.Key != workspaceKeyDataKey {
			t.Fatalf("ACH_SANDBOX_KEY = %+v, want SecretKeyRef %s/%s only", e, keyName, workspaceKeyDataKey)
		}
	}
	if found != 1 {
		t.Fatalf("ACH_SANDBOX_KEY entries = %d, want exactly one", found)
	}
	configMap := buildConfigMap(a, []byte(`{"schemaVersion":"workspace-v1"}`))
	if strings.Contains(configMap.Data[configFileName], "ACH_SANDBOX_KEY") || strings.Contains(configMap.Data[configFileName], "private-value") {
		t.Fatal("private key material or env name appeared in workspace config ConfigMap")
	}
}

func workspaceKeyTestAgent(namespace, name, uid string) *achv1alpha1.Agent {
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Namespace: namespace, Name: name, UID: types.UID(uid)}}
	a.APIVersion, a.Kind = achv1alpha1.GroupVersion.String(), "Agent"
	return a
}

func workspaceKeyTestSecret(t *testing.T, owner *achv1alpha1.Agent, value string) *corev1.Secret {
	t.Helper()
	s := &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: workspaceKeySecretName(string(owner.UID)), Namespace: owner.Namespace, Labels: agentLabels(owner)}, Type: corev1.SecretTypeOpaque, Data: map[string][]byte{workspaceKeyDataKey: []byte(value)}}
	if err := controllerutil.SetControllerReference(owner, s, workspaceKeyTestScheme(t)); err != nil {
		t.Fatalf("set owner reference: %v", err)
	}
	return s
}

func workspaceKeyTestScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatalf("add core scheme: %v", err)
	}
	if err := achv1alpha1.AddToScheme(s); err != nil {
		t.Fatalf("add ACH scheme: %v", err)
	}
	return s
}

type failingGetReader struct{ client.Reader }

func (failingGetReader) Get(context.Context, client.ObjectKey, client.Object, ...client.GetOption) error {
	return errors.New("apiserver timeout")
}

func TestEnsureWorkspaceKey_TransientErrorIsNotAConflict(t *testing.T) {
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).Build()
	r := &AgentReconciler{Client: c, APIReader: failingGetReader{c}, Scheme: scheme}
	err := r.ensureWorkspaceKey(context.Background(), a)
	if err == nil || errors.Is(err, errKeySecretConflict) {
		t.Fatalf("err = %v, want a non-conflict error", err)
	}
}

func TestPruneAndApply_LeaveObjectsTheAgentDoesNotControl(t *testing.T) {
	ctx := context.Background()
	a := workspaceKeyTestAgent("ns", "agent", "11111111-2222-4333-8444-555555555555")
	scheme := workspaceKeyTestScheme(t)
	mine := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "agent-http", Namespace: "ns"}}
	if err := controllerutil.SetControllerReference(a, mine, scheme); err != nil {
		t.Fatal(err)
	}
	theirs := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "theirs", Namespace: "ns"}}
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(mine, theirs).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}

	if err := r.prune(ctx, a, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "theirs", Namespace: "ns"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Get(ctx, client.ObjectKey{Namespace: "ns", Name: "theirs"}, &corev1.Service{}); err != nil {
		t.Fatalf("a Service the Agent does not control was deleted: %v", err)
	}
	if err := r.prune(ctx, a, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "agent-http", Namespace: "ns"}}); err != nil {
		t.Fatal(err)
	}
	if err := c.Get(ctx, client.ObjectKey{Namespace: "ns", Name: "agent-http"}, &corev1.Service{}); err == nil {
		t.Fatal("the controlled Service was not pruned")
	}
	if err := r.apply(ctx, a, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: "theirs", Namespace: "ns"}}); err == nil {
		t.Fatal("apply adopted an unowned Service")
	}
}
