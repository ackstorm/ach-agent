// SPDX-License-Identifier: MIT

package controller

import (
	"testing"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
)

func TestSandboxRefusalStands_ProfileGeneration(t *testing.T) {
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Generation: 2}}
	a.Status.Conditions = []metav1.Condition{{
		Type: condReady, Status: metav1.ConditionFalse, Reason: host.ReasonSandboxPolicyRejected,
		Message: "rejected (AgentProfile generation 3)", ObservedGeneration: 2,
	}}
	for gen, want := range map[int64]bool{3: true, 4: false, -1: true} {
		if got := sandboxRefusalStands(a, gen); got != want {
			t.Errorf("profile generation %d: stands = %v, want %v", gen, got, want)
		}
	}
	a.Generation = 3
	if sandboxRefusalStands(a, 3) {
		t.Error("a refusal for an older Agent generation must not stand")
	}
}
