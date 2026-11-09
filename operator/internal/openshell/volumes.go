// SPDX-License-Identifier: Apache-2.0

package openshell

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const (
	// AgentLabel names the owning Agent on a Workspace PVC; AgentUIDLabel selects.
	AgentLabel = "ach-agent.ackstorm.ai/agent"
	// DetachedAtAnnotation is when the Workspace PVC's sandbox was last removed (RFC 3339, UTC);
	// absent while a sandbox may mount it.
	DetachedAtAnnotation = "ach-agent.ackstorm.ai/detached-at"
	// The gateway's resource admission mounts only a PVC carrying both labels.
	attachableLabel          = "openshell.ai/sandbox-attachable"
	attachableWorkspaceLabel = "openshell.ai/sandbox-attachable-workspace"
	volumeMountPath          = "/sandbox/ach"
	volumeName               = "ach-data"
)

// sweepEvery bounds how often a host pass lists the Agent's Workspace PVCs for retention.
// A var so tests can shorten it.
var sweepEvery = 10 * time.Minute

var errVolumeNeedsTLS = errors.New("workspace.persistence.type volume needs the OpenShell gateway mTLS (openshell.tls.secret)")

// VolumeName is the Workspace PVC of persistence type volume: stable per Agent UID and Workspace
// name, DNS-safe, 23 characters.
func VolumeName(agentUID, workspaceName string) string {
	sum := sha256.Sum256([]byte(agentUID + "/" + workspaceName))
	return "ach-ws-" + hex.EncodeToString(sum[:])[:16]
}

// workspacePersistence is the Agent's resolved workspace.persistence; nil means type none.
func workspacePersistence(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) *achv1alpha1.WorkspacePersistenceSpec {
	if w := render.ResolveWorkspace(a.Spec.Workspace, p.Spec.Defaults.Workspace); w != nil {
		return w.Persistence
	}
	return nil
}

// driverConfig mounts the Workspace PVC read-write at /sandbox/ach and sets nothing else: it is
// the only driver_config the operator sends, and no Agent or AgentProfile field reaches it.
func driverConfig(claim string) map[string]any {
	return map[string]any{"kubernetes": map[string]any{
		"volumes": []any{map[string]any{"name": volumeName,
			"persistent_volume_claim": map[string]any{"claim_name": claim, "read_only": false}}},
		"containers": map[string]any{"agent": map[string]any{"volume_mounts": []any{
			map[string]any{"name": volumeName, "mount_path": volumeMountPath, "read_only": false}}}},
	}}
}

// ensureVolume returns the Workspace's PVC, created on its first sandbox and reused after, and
// clears its detached-at mark. A PVC being deleted is not reused: the create is retried.
func (b *openShellUnits) ensureVolume(ctx context.Context, a *achv1alpha1.Agent, v *achv1alpha1.WorkspaceVolumeSpec, workspace string) (string, error) {
	name := VolumeName(string(b.uid), workspace)
	var pvc corev1.PersistentVolumeClaim
	err := b.reader.Get(ctx, client.ObjectKey{Namespace: b.Config.PodNamespace, Name: name}, &pvc)
	switch {
	case apierrors.IsNotFound(err):
		mode := corev1.ReadWriteOnce
		if v.AccessMode == string(corev1.ReadWriteMany) {
			mode = corev1.ReadWriteMany
		}
		pvc = corev1.PersistentVolumeClaim{
			ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: b.Config.PodNamespace, Labels: map[string]string{
				attachableLabel: "true", attachableWorkspaceLabel: b.Config.Workspace,
				AgentLabel: a.Name, AgentUIDLabel: string(b.uid),
			}},
			Spec: corev1.PersistentVolumeClaimSpec{
				AccessModes: []corev1.PersistentVolumeAccessMode{mode},
				Resources:   corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: v.Size}},
			},
		}
		if v.StorageClassName != "" {
			pvc.Spec.StorageClassName = &v.StorageClassName
		}
		if err := b.writer.Create(ctx, &pvc); err != nil && !apierrors.IsAlreadyExists(err) {
			return "", err
		}
		return name, nil
	case err != nil:
		return "", err
	case !pvc.DeletionTimestamp.IsZero():
		return "", fmt.Errorf("Workspace PVC %s/%s is being deleted; retrying", b.Config.PodNamespace, name)
	}
	if _, ok := pvc.Annotations[DetachedAtAnnotation]; ok {
		patch := client.MergeFrom(pvc.DeepCopy())
		delete(pvc.Annotations, DetachedAtAnnotation)
		if err := b.writer.Patch(ctx, &pvc, patch); err != nil {
			return "", err
		}
	}
	return name, nil
}

// markDetached stamps the Workspace's PVC, when it has one, with the time its sandbox went.
func (b *openShellUnits) markDetached(ctx context.Context, workspace string) error {
	var pvc corev1.PersistentVolumeClaim
	err := b.reader.Get(ctx, client.ObjectKey{Namespace: b.Config.PodNamespace, Name: VolumeName(string(b.uid), workspace)}, &pvc)
	if apierrors.IsNotFound(err) {
		return nil
	}
	if err != nil {
		return err
	}
	if _, ok := pvc.Annotations[DetachedAtAnnotation]; ok || !pvc.DeletionTimestamp.IsZero() {
		return nil
	}
	patch := client.MergeFrom(pvc.DeepCopy())
	metav1.SetMetaDataAnnotation(&pvc.ObjectMeta, DetachedAtAnnotation, time.Now().UTC().Format(time.RFC3339))
	return b.writer.Patch(ctx, &pvc, patch)
}

// Sweep deletes the Agent's Workspace PVCs detached for longer than persistence.retentionDays.
// After a change away from type volume no sandbox mounts them again: a detached one goes at once.
func (b *openShellUnits) Sweep(ctx context.Context, a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) error {
	if time.Since(b.swept) < sweepEvery {
		return nil
	}
	var pvcs corev1.PersistentVolumeClaimList
	if err := b.reader.List(ctx, &pvcs, client.InNamespace(b.Config.PodNamespace),
		client.MatchingLabels{AgentUIDLabel: string(b.uid)}); err != nil {
		return err
	}
	b.swept = time.Now()
	var keep time.Duration
	if persist := workspacePersistence(a, p); persist != nil && persist.Type == "volume" {
		keep = time.Duration(*persist.RetentionDays) * 24 * time.Hour
	}
	for i := range pvcs.Items {
		at, err := time.Parse(time.RFC3339, pvcs.Items[i].Annotations[DetachedAtAnnotation])
		if err != nil || time.Since(at) < keep {
			continue // mounted, or detached for less than retentionDays
		}
		if err := b.writer.Delete(ctx, &pvcs.Items[i]); client.IgnoreNotFound(err) != nil {
			return err
		}
	}
	return nil
}
