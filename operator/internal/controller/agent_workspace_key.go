// SPDX-License-Identifier: Apache-2.0

package controller

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"errors"
	"fmt"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
)

const workspaceKeyDataKey = host.KeySecretDataKey

func workspaceKeySecretName(uid string) string { return host.KeySecretName(uid) }

// ensureWorkspaceKey ensures the legacy Harness key exists without ever returning
// or replacing its secret value. K is the UTF-8 encoding of the stored hex string.
// errKeySecretConflict marks a key Secret the Agent cannot use: foreign or empty. Other errors
// (apiserver trouble) are transient and requeue.
var errKeySecretConflict = errors.New("workspace key Secret conflict")

func (r *AgentReconciler) ensureWorkspaceKey(ctx context.Context, a *achv1alpha1.Agent) error {
	return ensureKeySecret(ctx, r.Client, r.APIReader, r.Scheme, a, achAgentOwnerKind, agentLabels(a))
}

// ensureKeySecret is ensureWorkspaceKey for any owner: the UID-named key Secret, created once and
// never updated, controlled by owner (of kind).
func ensureKeySecret(ctx context.Context, c client.Client, reader client.Reader, scheme *runtime.Scheme,
	owner client.Object, kind string, labels map[string]string) error {
	key := types.NamespacedName{Namespace: owner.GetNamespace(), Name: workspaceKeySecretName(string(owner.GetUID()))}
	readAndValidate := func() error {
		var existing corev1.Secret
		if err := reader.Get(ctx, key, &existing); err != nil {
			return err
		}
		if !keyOwnedBy(&existing, owner, kind) {
			return fmt.Errorf("%w: not controlled by this %s", errKeySecretConflict, kind)
		}
		if len(existing.Data[workspaceKeyDataKey]) == 0 {
			return fmt.Errorf("%w: no nonempty key", errKeySecretConflict)
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
		ObjectMeta: metav1.ObjectMeta{Name: key.Name, Namespace: key.Namespace, Labels: labels},
		Type:       corev1.SecretTypeOpaque,
		Data:       map[string][]byte{workspaceKeyDataKey: []byte(hex.EncodeToString(raw))},
	}
	if err := controllerutil.SetControllerReference(owner, secret, scheme); err != nil {
		return fmt.Errorf("set workspace key owner: %w", err)
	}
	if err := c.Create(ctx, secret); err != nil {
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

func workspaceKeyOwnedBy(secret *corev1.Secret, a *achv1alpha1.Agent) bool {
	return keyOwnedBy(secret, a, achAgentOwnerKind)
}

func keyOwnedBy(secret *corev1.Secret, owner client.Object, kind string) bool {
	for _, ref := range secret.OwnerReferences {
		if ref.Controller != nil && *ref.Controller && ref.UID == owner.GetUID() && ref.Name == owner.GetName() && ref.Kind == kind && ref.APIVersion == achv1alpha1.GroupVersion.String() {
			return true
		}
	}
	return false
}
