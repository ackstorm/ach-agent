// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

func volumePersistence() *achv1alpha1.WorkspacePersistenceSpec {
	return &achv1alpha1.WorkspacePersistenceSpec{Type: "volume", RetentionDays: ptrInt64(30),
		Volume: &achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("1Gi")}}
}

// applyVolumeAgent creates profile p (persistence type volume) and Agent a in ns.
func applyVolumeAgent(t *testing.T, ns string) {
	t.Helper()
	ctx := context.Background()
	mustApply(t, ctx, &corev1.Secret{ObjectMeta: metav1.ObjectMeta{Name: "ek", Namespace: ns}, Data: map[string][]byte{"ek": []byte("v")}})
	spec := minimalProfileSpec(false)
	spec.Defaults.Workspace.Persistence = volumePersistence()
	mustApply(t, ctx, &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "p", Namespace: ns}, Spec: spec})
	mustApply(t, ctx, &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "a", Namespace: ns}, Spec: achv1alpha1.AgentSpec{
		ProfileRef: achv1alpha1.LocalObjectRef{Name: "p"}, CapabilityServer: "standalone", Standalone: testStandalone(),
		Channels: []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}},
	}})
}

// TestAgent_VolumeNeedsGatewayTLS: type volume renders nothing unless the operator talks to the
// gateway with mTLS, the only setup where the operator alone can send driver_config.
func TestAgent_VolumeNeedsGatewayTLS(t *testing.T) {
	const ns = "aa-volume-tls"
	isolatedStorageNamespace(t, ns)
	applyVolumeAgent(t, ns)
	ctx := context.Background()

	a := reconcileOnce(t, isolatedAgentReconciler(t, render.RuntimeStorageOptions{}), ns, "a")
	c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied)
	if c == nil || c.Status != metav1.ConditionFalse || c.Reason != "VolumeRequiresGatewayTLS" {
		t.Fatalf("WorkloadApplied = %+v, want False/VolumeRequiresGatewayTLS", c)
	}
	var sts appsv1.StatefulSet
	if err := k8sClient.Get(ctx, types.NamespacedName{Namespace: ns, Name: render.ControlName("a")}, &sts); !apierrors.IsNotFound(err) {
		t.Fatalf("control StatefulSet of a refused volume Agent: err = %v, want NotFound", err)
	}

	withTLS := isolatedAgentReconciler(t, render.RuntimeStorageOptions{})
	withTLS.OpenShellCA = []byte("test CA")
	a = reconcileOnce(t, withTLS, ns, "a")
	if c := apimeta.FindStatusCondition(a.Status.Conditions, condWorkloadApplied); c == nil || c.Status != metav1.ConditionTrue {
		t.Fatalf("with gateway TLS: WorkloadApplied = %+v, want True", c)
	}
}
