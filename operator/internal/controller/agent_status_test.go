// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"testing"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func finishWith(t *testing.T, a *achv1alpha1.Agent) metav1.Condition {
	t.Helper()
	scheme := workspaceKeyTestScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(a).WithStatusSubresource(&achv1alpha1.Agent{}).Build()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: scheme}
	var conds []metav1.Condition
	for _, ct := range requiredConds {
		setCond(&conds, ct, metav1.ConditionTrue, "Ok", "", a.Generation)
	}
	if _, err := r.finish(context.Background(), a, conds); err != nil {
		t.Fatal(err)
	}
	var got achv1alpha1.Agent
	if err := c.Get(context.Background(), types.NamespacedName{Namespace: a.Namespace, Name: a.Name}, &got); err != nil {
		t.Fatal(err)
	}
	ready := apimeta.FindStatusCondition(got.Status.Conditions, condReady)
	if ready == nil {
		t.Fatal("Ready missing")
	}
	return *ready
}

func refusedAgent(reason string, observed int64) *achv1alpha1.Agent {
	a := workspaceKeyTestAgent("ns", "a", "11111111-2222-4333-8444-555555555555")
	a.Generation = 3
	a.Status.Conditions = []metav1.Condition{{
		Type: condReady, Status: metav1.ConditionFalse, Reason: reason, Message: "refused", ObservedGeneration: observed,
		LastTransitionTime: metav1.Now(),
	}}
	return a
}

func TestFinish_KeepsASandboxRefusalForTheSameGeneration(t *testing.T) {
	for _, reason := range []string{"SandboxPolicyRejected", "SandboxResourcesUnsupported"} {
		got := finishWith(t, refusedAgent(reason, 3))
		if got.Status != metav1.ConditionFalse || got.Reason != reason || got.Message != "refused" {
			t.Errorf("%s: Ready = %+v, want the refusal kept", reason, got)
		}
	}
}

func TestFinish_ReplacesASandboxRefusalAfterASpecChange(t *testing.T) {
	got := finishWith(t, refusedAgent("SandboxPolicyRejected", 2))
	if got.Status != metav1.ConditionTrue {
		t.Errorf("Ready = %+v, want True once the generation moved on", got)
	}
}

func TestFinish_OtherFalseReadyReasonsAreRecomputed(t *testing.T) {
	got := finishWith(t, refusedAgent("PodNotReady", 3))
	if got.Status != metav1.ConditionTrue {
		t.Errorf("Ready = %+v, want recomputed True", got)
	}
}
