// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"errors"
	"testing"
	"time"

	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

type fakeRemover struct {
	calls     []string
	remaining []bool // returned in order, last one repeated
	err       error
}

func (f *fakeRemover) RemoveAgentSandboxes(_ context.Context, uid string) (bool, error) {
	f.calls = append(f.calls, uid)
	return f.remaining[min(len(f.calls), len(f.remaining))-1], f.err
}

const finalizerTestUID = "11111111-2222-4333-8444-555555555555"

// deletingAgent builds a fake client holding an ACHAgent that carries the finalizer and is being
// deleted (the fake client sets deletionTimestamp on Delete when finalizers exist).
func deletingAgent(t *testing.T, objs ...client.Object) (client.Client, types.NamespacedName) {
	t.Helper()
	a := workspaceKeyTestAgent("ns", "agent", finalizerTestUID)
	a.Finalizers = []string{executionFinalizer}
	c := fake.NewClientBuilder().WithScheme(workspaceKeyTestScheme(t)).WithObjects(append(objs, a)...).Build()
	if err := c.Delete(context.Background(), a); err != nil {
		t.Fatalf("delete agent: %v", err)
	}
	return c, client.ObjectKeyFromObject(a)
}

func finalizerReconcile(t *testing.T, c client.Client, key types.NamespacedName, s sandboxRemover) (ctrl.Result, error) {
	t.Helper()
	r := &ACHAgentReconciler{Client: c, APIReader: c, Scheme: c.Scheme(), Sandboxes: s}
	return r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key})
}

func hasFinalizer(t *testing.T, c client.Client, key types.NamespacedName) bool {
	t.Helper()
	var got achv1alpha1.ACHAgent
	if err := c.Get(context.Background(), key, &got); err != nil {
		if apierrors.IsNotFound(err) {
			return false
		}
		t.Fatalf("get agent: %v", err)
	}
	return controllerutil.ContainsFinalizer(&got, executionFinalizer)
}

func TestFinalizer_AddedOnceForEveryAgent(t *testing.T) {
	a := &achv1alpha1.ACHAgent{}
	if !ensureExecutionFinalizer(a) || !controllerutil.ContainsFinalizer(a, executionFinalizer) {
		t.Fatalf("finalizer not added: %v", a.Finalizers)
	}
	if ensureExecutionFinalizer(a) {
		t.Error("second call reported a change")
	}
}

func TestFinalize_DeletesByUIDAndRemovesTheFinalizerWhenNothingRemains(t *testing.T) {
	c, key := deletingAgent(t)
	rem := &fakeRemover{remaining: []bool{true, false}}

	res, err := finalizerReconcile(t, c, key, rem)
	if err != nil || res.RequeueAfter != 5*time.Second {
		t.Fatalf("first reconcile = %+v, %v; want RequeueAfter 5s", res, err)
	}
	if !hasFinalizer(t, c, key) {
		t.Fatal("finalizer removed while sandboxes remain")
	}
	if _, err := finalizerReconcile(t, c, key, rem); err != nil {
		t.Fatalf("second reconcile: %v", err)
	}
	if hasFinalizer(t, c, key) {
		t.Error("finalizer kept after the gateway lists nothing")
	}
	if len(rem.calls) != 2 || rem.calls[0] != finalizerTestUID || rem.calls[1] != finalizerTestUID {
		t.Errorf("calls = %v, want two by agent UID", rem.calls)
	}
}

func TestFinalize_RunsBeforeTheProfileIsResolved(t *testing.T) {
	// The deletion branch runs before the profile is resolved.
	profile := &achv1alpha1.AgentProfile{
		ObjectMeta: metav1.ObjectMeta{Namespace: "ns", Name: "p"},
	}
	for name, objs := range map[string][]client.Object{"profile present": {profile}, "profile gone": nil} {
		t.Run(name, func(t *testing.T) {
			c, key := deletingAgent(t, objs...)
			rem := &fakeRemover{remaining: []bool{true, false}}
			if res, err := finalizerReconcile(t, c, key, rem); err != nil || res.RequeueAfter == 0 {
				t.Fatalf("first reconcile = %+v, %v; want a requeue", res, err)
			}
			if !hasFinalizer(t, c, key) {
				t.Fatal("finalizer removed while sandboxes remain")
			}
			if _, err := finalizerReconcile(t, c, key, rem); err != nil {
				t.Fatalf("second reconcile: %v", err)
			}
			if hasFinalizer(t, c, key) {
				t.Error("finalizer kept after remaining=false")
			}
			if len(rem.calls) != 2 || rem.calls[0] != finalizerTestUID {
				t.Errorf("calls = %v, want two by agent UID", rem.calls)
			}
		})
	}
}

func TestFinalize_GatewayErrorKeepsTheFinalizer(t *testing.T) {
	c, key := deletingAgent(t)
	rem := &fakeRemover{remaining: []bool{false}, err: errors.New("gateway down")}
	if _, err := finalizerReconcile(t, c, key, rem); err == nil {
		t.Fatal("want the gateway error returned")
	}
	if !hasFinalizer(t, c, key) {
		t.Error("finalizer removed despite the gateway error")
	}
}

func TestFinalize_NoGatewayRemovesTheFinalizer(t *testing.T) {
	// The reconciler has no event recorder: the warning is logged, nothing to assert about events.
	c, key := deletingAgent(t)
	if _, err := finalizerReconcile(t, c, key, nil); err != nil {
		t.Fatalf("reconcile: %v", err)
	}
	if hasFinalizer(t, c, key) {
		t.Error("finalizer kept with no gateway configured")
	}
}
