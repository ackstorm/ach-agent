// SPDX-License-Identifier: MIT

package controller

import (
	"context"
	"errors"
	"fmt"
	"slices"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
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

// deletingAgent builds a fake client holding an Agent that carries the finalizer and is being
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
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: c.Scheme(), Sandboxes: s}
	return r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key})
}

func hasFinalizer(t *testing.T, c client.Client, key types.NamespacedName) bool {
	t.Helper()
	var got achv1alpha1.Agent
	if err := c.Get(context.Background(), key, &got); err != nil {
		if apierrors.IsNotFound(err) {
			return false
		}
		t.Fatalf("get agent: %v", err)
	}
	return controllerutil.ContainsFinalizer(&got, executionFinalizer)
}

func TestFinalizer_AddedOnceForEveryAgent(t *testing.T) {
	a := &achv1alpha1.Agent{}
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

func TestFinalize_GatewayUnavailableKeepsTheFinalizerAndRetries(t *testing.T) {
	c, key := deletingAgent(t)
	rem := &fakeRemover{remaining: []bool{false}, err: fmt.Errorf("%w: down", host.ErrGatewayUnavailable)}
	res, err := finalizerReconcile(t, c, key, rem)
	if err != nil || res.RequeueAfter != 15*time.Second {
		t.Fatalf("reconcile = %+v, %v; want RequeueAfter 15s and no error", res, err)
	}
	if !hasFinalizer(t, c, key) {
		t.Error("finalizer removed while the gateway is unavailable")
	}
}

func TestFinalize_AnyInventoryErrorKeepsTheFinalizer(t *testing.T) {
	c, key := deletingAgent(t)
	rem := &fakeRemover{remaining: []bool{false}, err: errors.New("boom")}
	if res, err := finalizerReconcile(t, c, key, rem); err != nil || res.RequeueAfter != 15*time.Second {
		t.Fatalf("reconcile = %+v, %v", res, err)
	}
	if !hasFinalizer(t, c, key) {
		t.Error("finalizer removed despite the error")
	}
}

func TestFinalize_NoGatewayKeepsTheFinalizer(t *testing.T) {
	c, key := deletingAgent(t)
	res, err := finalizerReconcile(t, c, key, nil)
	if err != nil || res.RequeueAfter != 15*time.Second {
		t.Fatalf("reconcile = %+v, %v; want a retry", res, err)
	}
	if !hasFinalizer(t, c, key) {
		t.Error("finalizer removed with no gateway configured: a missing gateway is not an empty inventory")
	}
}

// order records the sequence of the stopper and the remover.
type order struct{ steps []string }

type fakeStopper struct {
	o     *order
	calls []types.UID
	err   error
}

func (f *fakeStopper) Stop(_ context.Context, uid types.UID) error {
	f.calls = append(f.calls, uid)
	f.o.steps = append(f.o.steps, "stop")
	return f.err
}

type orderedRemover struct {
	o *order
	fakeRemover
}

func (r *orderedRemover) RemoveAgentSandboxes(ctx context.Context, uid string) (bool, error) {
	r.o.steps = append(r.o.steps, "inventory")
	return r.fakeRemover.RemoveAgentSandboxes(ctx, uid)
}

func finalizerReconcileWithHost(t *testing.T, c client.Client, key types.NamespacedName, s sandboxRemover, h hostStopper) (ctrl.Result, error) {
	t.Helper()
	r := &AgentReconciler{Client: c, APIReader: c, Scheme: c.Scheme(), Sandboxes: s, Host: h}
	return r.Reconcile(context.Background(), ctrl.Request{NamespacedName: key})
}

func TestFinalize_StopsHostLoopBeforeInventory(t *testing.T) {
	c, key := deletingAgent(t)
	o := &order{}
	stop := &fakeStopper{o: o}
	rem := &orderedRemover{o: o, fakeRemover: fakeRemover{remaining: []bool{false}}}
	if _, err := finalizerReconcileWithHost(t, c, key, rem, stop); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(o.steps, []string{"stop", "inventory"}) || len(stop.calls) != 1 || stop.calls[0] != finalizerTestUID {
		t.Fatalf("steps = %v stop calls = %v, want the host loop of this UID stopped before the inventory", o.steps, stop.calls)
	}
}

func TestFinalize_AHostLoopThatWillNotStopKeepsTheFinalizerAndSkipsTheInventory(t *testing.T) {
	c, key := deletingAgent(t)
	o := &order{}
	stop := &fakeStopper{o: o, err: context.DeadlineExceeded}
	rem := &orderedRemover{o: o, fakeRemover: fakeRemover{remaining: []bool{false}}}
	if res, err := finalizerReconcileWithHost(t, c, key, rem, stop); err != nil || res.RequeueAfter == 0 {
		t.Fatalf("reconcile = %+v, %v; want a requeue", res, err)
	}
	if !hasFinalizer(t, c, key) || len(rem.calls) != 0 {
		t.Fatalf("finalizer kept = %v inventory calls = %v: sandboxes must not be listed while a create may still race", hasFinalizer(t, c, key), rem.calls)
	}
}

func TestFinalize_RemovesFinalizerOnlyWhenNothingRemains(t *testing.T) {
	c, key := deletingAgent(t)
	rem := &fakeRemover{remaining: []bool{true, true, false}}
	for i, wantKept := range []bool{true, true, false} {
		if _, err := finalizerReconcile(t, c, key, rem); err != nil {
			t.Fatalf("reconcile %d: %v", i, err)
		}
		if got := hasFinalizer(t, c, key); got != wantKept {
			t.Fatalf("after reconcile %d finalizer kept = %v, want %v", i, got, wantKept)
		}
	}
}

func sandboxService(name, uid string) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Namespace: "ns", Name: name, Labels: map[string]string{openshell.AgentUIDLabel: uid}},
		Spec:       corev1.ServiceSpec{Type: corev1.ServiceTypeExternalName, ExternalName: "gateway.openshell.svc"},
	}
}

func TestFinalize_PerSandboxServicesAreDeletedBeforeTheFinalizerGoes(t *testing.T) {
	mine, other := sandboxService("default--ws-mine", finalizerTestUID), sandboxService("default--ws-other", "another-uid")
	c, key := deletingAgent(t, mine, other)
	rem := &fakeRemover{remaining: []bool{false}}

	res, err := finalizerReconcile(t, c, key, rem)
	if err != nil || res.RequeueAfter != 5*time.Second {
		t.Fatalf("first reconcile = %+v, %v; want a re-check after deleting the Services", res, err)
	}
	if !hasFinalizer(t, c, key) {
		t.Fatal("finalizer removed in the pass that still found a Service")
	}
	var svcs corev1.ServiceList
	if err := c.List(context.Background(), &svcs); err != nil {
		t.Fatal(err)
	}
	if len(svcs.Items) != 1 || svcs.Items[0].Name != "default--ws-other" {
		t.Fatalf("services = %v, want only the other agent's left", svcs.Items)
	}
	if _, err := finalizerReconcile(t, c, key, rem); err != nil {
		t.Fatal(err)
	}
	if hasFinalizer(t, c, key) {
		t.Error("finalizer kept after the inventory and the Services are empty")
	}
}
