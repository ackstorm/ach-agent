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

// removeWorkspaceVolumes deletes the Agent's Workspace PVCs, which live in the sandbox namespace
// and so carry no owner reference, and reports whether any is left (a PVC stays until the last
// pod using it is gone).
func (r *AgentReconciler) removeWorkspaceVolumes(ctx context.Context, a *achv1alpha1.Agent) (bool, error) {
	return removeVolumes(ctx, r.APIReader, r.Client, r.SandboxNamespace, string(a.UID))
}

// removeVolumes is removeWorkspaceVolumes for any owner UID.
func removeVolumes(ctx context.Context, reader client.Reader, c client.Client, ns, uid string) (bool, error) {
	if ns == "" {
		return false, nil
	}
	var pvcs corev1.PersistentVolumeClaimList
	if err := reader.List(ctx, &pvcs, client.InNamespace(ns), client.MatchingLabels{openshell.AgentUIDLabel: uid}); err != nil {
		return false, fmt.Errorf("list Workspace PVCs of %s: %w", uid, err)
	}
	for i := range pvcs.Items {
		if err := c.Delete(ctx, &pvcs.Items[i]); client.IgnoreNotFound(err) != nil {
			return false, fmt.Errorf("delete Workspace PVC %s: %w", pvcs.Items[i].Name, err)
		}
	}
	return len(pvcs.Items) > 0, nil
}
