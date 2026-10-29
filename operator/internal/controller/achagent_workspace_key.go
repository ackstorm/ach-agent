// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

const workspaceKeyDataKey = "key"

func workspaceKeySecretName(uid string) string { return "ach-agent-key-" + uid }

// ensureWorkspaceKey ensures the legacy Harness key exists without ever returning
// or replacing its secret value. K is the UTF-8 encoding of the stored hex string.
func (r *ACHAgentReconciler) ensureWorkspaceKey(ctx context.Context, a *achv1alpha1.ACHAgent) error {
	key := types.NamespacedName{Namespace: a.Namespace, Name: workspaceKeySecretName(string(a.UID))}
	readAndValidate := func() error {
		var existing corev1.Secret
		if err := r.APIReader.Get(ctx, key, &existing); err != nil {
			return err
		}
		if !workspaceKeyOwnedBy(&existing, a) {
			return fmt.Errorf("workspace key Secret is not controlled by this ACHAgent")
		}
		if len(existing.Data[workspaceKeyDataKey]) == 0 {
			return fmt.Errorf("workspace key Secret has no nonempty key")
		}
		return nil
	}
	if err := readAndValidate(); err == nil {
		return nil
	} else if !apierrors.IsNotFound(err) {
		return err
	}

	raw := make([]byte, 32)
	if _, err := rand.Read(raw); err != nil {
		return fmt.Errorf("generate workspace key: %w", err)
	}
	secret := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: a.Namespace, Labels: agentLabels(a)},
		Type:       corev1.SecretTypeOpaque,
		Data:       map[string][]byte{workspaceKeyDataKey: []byte(hex.EncodeToString(raw))},
	}
	if err := controllerutil.SetControllerReference(a, secret, r.Scheme); err != nil {
		return fmt.Errorf("set workspace key owner: %w", err)
	}
	if err := r.Client.Create(ctx, secret); err != nil {
		if apierrors.IsAlreadyExists(err) {
			// A concurrent reconcile won creation. Read and validate that exact object;
			// never overwrite it or generate a replacement in place.
			if readErr := readAndValidate(); readErr != nil {
				return readErr
			}
			return nil
		}
		return fmt.Errorf("create workspace key Secret: %w", err)
	}
	return nil
}

func workspaceKeyOwnedBy(secret *corev1.Secret, a *achv1alpha1.ACHAgent) bool {
	for _, ref := range secret.OwnerReferences {
		if ref.Controller != nil && *ref.Controller && ref.UID == a.UID && ref.Name == a.Name && ref.Kind == achAgentOwnerKind && ref.APIVersion == achv1alpha1.GroupVersion.String() {
			return true
		}
	}
	return false
}
