// SPDX-License-Identifier: Apache-2.0

package controller

import (
	"context"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
)

// workspaceVolumes lists the Agent's Workspace PVCs (persistence type volume, created by the
// host loop in the sandbox namespace), uncached.
func (r *AgentReconciler) workspaceVolumes(ctx context.Context, a *achv1alpha1.Agent) ([]corev1.PersistentVolumeClaim, error) {
	var pvcs corev1.PersistentVolumeClaimList
	if err := r.APIReader.List(ctx, &pvcs, client.InNamespace(r.SandboxNamespace),
		client.MatchingLabels{openshell.AgentUIDLabel: string(a.UID)}); err != nil {
		return nil, fmt.Errorf("list Workspace PVCs of %s: %w", a.Name, err)
	}
	return pvcs.Items, nil
}

// removeWorkspaceVolumes deletes the Agent's Workspace PVCs, which live in the sandbox namespace
// and so carry no owner reference, and reports whether any is left (a PVC stays until the last
// pod using it is gone).
func (r *AgentReconciler) removeWorkspaceVolumes(ctx context.Context, a *achv1alpha1.Agent) (bool, error) {
	if r.SandboxNamespace == "" {
		return false, nil
	}
	pvcs, err := r.workspaceVolumes(ctx, a)
	if err != nil {
		return false, err
	}
	for i := range pvcs {
		if err := r.Delete(ctx, &pvcs[i]); client.IgnoreNotFound(err) != nil {
			return false, fmt.Errorf("delete Workspace PVC %s: %w", pvcs[i].Name, err)
		}
	}
	return len(pvcs) > 0, nil
}
