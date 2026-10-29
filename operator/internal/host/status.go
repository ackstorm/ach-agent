// SPDX-License-Identifier: MIT

package host

import (
	"context"

	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/util/retry"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// StatusWriter writes one condition onto an Agent's status. The host loop runs outside the
// reconciler, so it reports what only it sees (a sandbox the gateway refuses, a gateway that
// is down) through this.
type StatusWriter interface {
	SetCondition(ctx context.Context, agent types.NamespacedName, cond metav1.Condition) error
}

// AgentStatusWriter patches status.conditions of an Agent: a fresh read, one condition
// replaced, an optimistic-lock merge patch retried on conflict.
type AgentStatusWriter struct {
	Client client.Client
	Reader client.Reader // uncached
}

func (w *AgentStatusWriter) SetCondition(ctx context.Context, key types.NamespacedName, cond metav1.Condition) error {
	return retry.RetryOnConflict(retry.DefaultRetry, func() error {
		var a achv1alpha1.Agent
		if err := w.Reader.Get(ctx, key, &a); err != nil {
			return client.IgnoreNotFound(err)
		}
		base := a.DeepCopy()
		apimeta.SetStatusCondition(&a.Status.Conditions, cond)
		return w.Client.Status().Patch(ctx, &a, client.MergeFromWithOptions(base, client.MergeFromWithOptimisticLock{}))
	})
}

// reasoned is implemented by backend errors that should surface on the Agent.
type reasoned interface {
	StatusReason() string
}
