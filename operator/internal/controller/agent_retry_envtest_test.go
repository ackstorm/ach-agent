// SPDX-License-Identifier: Apache-2.0

package controller

import (
	"context"
	"errors"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/interceptor"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// secretFailingReader fails every Secret read, as an apiserver that times out does.
type secretFailingReader struct{ client.Reader }

func (r secretFailingReader) Get(ctx context.Context, key client.ObjectKey, obj client.Object, opts ...client.GetOption) error {
	if _, ok := obj.(*corev1.Secret); ok {
		return errors.New("apiserver timeout")
	}
	return r.Reader.Get(ctx, key, obj, opts...)
}

// retryTestAgent creates a profile and a standalone Agent in ns.
func retryTestAgent(t *testing.T, ns, name string, standalone *achv1alpha1.StandaloneSpec) {
	t.Helper()
	ctx := context.Background()
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Spec: minimalProfileSpec(false)})
	mustApply(t, ctx, &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Spec: achv1alpha1.AgentSpec{
		ProfileRef:       achv1alpha1.LocalObjectRef{Name: name},
		CapabilityServer: "standalone", Standalone: standalone,
		Channels: []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
	}})
}

// reconcileFailing runs one Reconcile that must fail, and returns the condition it wrote.
func reconcileFailing(t *testing.T, r *AgentReconciler, ns, name, condType string) *metav1.Condition {
	t.Helper()
	ctx := context.Background()
	key := types.NamespacedName{Namespace: ns, Name: name}
	if _, err := r.Reconcile(ctx, ctrl.Request{NamespacedName: key}); err == nil {
		t.Fatalf("Reconcile returned no error: the controller would not retry")
	}
	var a achv1alpha1.Agent
	if err := k8sClient.Get(ctx, key, &a); err != nil {
		t.Fatalf("get agent: %v", err)
	}
	return apimeta.FindStatusCondition(a.Status.Conditions, condType)
}

// A failed apply writes ApplyFailed and returns the error. A second identical failure
// changes no status and so raises no watch event: only the returned error retries it.
func TestAgent_ApplyFailure_IsRetried(t *testing.T) {
	const ns = "aa-apply-retry"
	isolatedStorageNamespace(t, ns)
	retryTestAgent(t, ns, "a", testStandalone())

	r := isolatedAgentReconciler(t, render.RuntimeStorageOptions{})
	base, err := client.NewWithWatch(cfg, client.Options{Scheme: k8sClient.Scheme()})
	if err != nil {
		t.Fatalf("watch client: %v", err)
	}
	r.Client = interceptor.NewClient(base, interceptor.Funcs{
		Create: func(ctx context.Context, c client.WithWatch, obj client.Object, opts ...client.CreateOption) error {
			if _, ok := obj.(*appsv1.StatefulSet); ok {
				return errors.New("admission webhook unavailable")
			}
			return c.Create(ctx, obj, opts...)
		},
	})

	for attempt := range 2 {
		c := reconcileFailing(t, r, ns, "a", condWorkloadApplied)
		if c == nil || c.Status != metav1.ConditionFalse || c.Reason != "ApplyFailed" {
			t.Fatalf("attempt %d: WorkloadApplied = %+v, want False/ApplyFailed", attempt, c)
		}
	}
}

// A Secret read that fails (not a missing Secret) writes SecretReadFailed and returns the
// error: no watch reports that the apiserver answers again.
func TestAgent_SecretReadFailure_IsRetried(t *testing.T) {
	const ns = "aa-secret-retry"
	isolatedStorageNamespace(t, ns)
	ctx := context.Background()
	for _, name := range []string{"litellm", "serper"} {
		mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns}, Data: map[string][]byte{"apiKey": []byte("v")}})
	}
	retryTestAgent(t, ns, "a", testHeaderStandalone())

	r := isolatedAgentReconciler(t, render.RuntimeStorageOptions{})
	r.APIReader = secretFailingReader{r.APIReader}

	c := reconcileFailing(t, r, ns, "a", condSecretsResolved)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != "SecretReadFailed" {
		t.Fatalf("SecretsResolved = %+v, want False/SecretReadFailed", c)
	}
}
