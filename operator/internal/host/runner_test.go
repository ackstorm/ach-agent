// SPDX-License-Identifier: MIT

package host

import (
	"context"
	"errors"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

func TestRunner_OneLoopPerAgentWithARunningControlPod(t *testing.T) {
	const ns = "ach"
	agent := func(name string, ann map[string]string) *achv1alpha1.Agent {
		return &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: ns, UID: types.UID("uid-" + name), Annotations: ann}}
	}
	pod := func(name string, phase corev1.PodPhase) *corev1.Pod {
		return &corev1.Pod{
			ObjectMeta: metav1.ObjectMeta{Name: render.ControlName(name) + "-0", Namespace: ns},
			Status:     corev1.PodStatus{Phase: phase},
		}
	}
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(
		agent("running", nil), pod("running", corev1.PodRunning),
		agent("starting", nil), pod("starting", corev1.PodPending),
		agent("nopod", nil),
		agent("paused", map[string]string{PauseAnnotation: "true"}), pod("paused", corev1.PodRunning),
	).Build()

	started := make(chan string, 8)
	stopped := make(chan string, 8)
	r := &Runner{Client: c, Reader: c, Namespace: ns, Log: logr.Discard()}
	r.run = func(ctx context.Context, a *achv1alpha1.Agent) {
		started <- a.Name
		<-ctx.Done()
		stopped <- a.Name
	}
	loops := map[types.UID]context.CancelFunc{}
	var wg sync.WaitGroup
	ctx := context.Background()

	r.sync(ctx, loops, &wg)
	r.sync(ctx, loops, &wg) // idempotent: no second loop for the same agent
	if len(loops) != 1 || loops["uid-running"] == nil {
		t.Fatalf("loops = %v, want only the agent whose control pod is Running and is not paused (the reconciler must not wait for Agent Ready: Ready waits for this reconciler's inventory)", loops)
	}
	select {
	case n := <-started:
		if n != "running" {
			t.Fatalf("started %q", n)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("loop did not start")
	}

	// Pausing stops the loop at the next tick.
	var a achv1alpha1.Agent
	if err := c.Get(ctx, client.ObjectKey{Namespace: ns, Name: "running"}, &a); err != nil {
		t.Fatal(err)
	}
	a.Annotations = map[string]string{PauseAnnotation: "true"}
	if err := c.Update(ctx, &a); err != nil {
		t.Fatal(err)
	}
	r.sync(ctx, loops, &wg)
	if len(loops) != 0 {
		t.Fatalf("loops = %v after pausing", loops)
	}
	select {
	case <-stopped:
	case <-time.After(5 * time.Second):
		t.Fatal("paused loop was not cancelled")
	}
	wg.Wait()
}

func TestRunner_KeyReadsTheAgentSecret(t *testing.T) {
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "x", Namespace: "ach", UID: "u1"}}
	sec := &corev1.Secret{
		ObjectMeta: metav1.ObjectMeta{Name: KeySecretName("u1"), Namespace: "ach"},
		Data:       map[string][]byte{KeySecretDataKey: []byte("abcd")},
	}
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(a, sec).Build()
	r := &Runner{Client: c, Reader: c}
	got, err := r.key(context.Background(), a)
	if err != nil || string(got) != "abcd" {
		t.Fatalf("key = (%q, %v)", got, err)
	}
	other := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "y", Namespace: "ach", UID: "u2"}}
	if _, err := r.key(context.Background(), other); err == nil {
		t.Error("a missing key Secret must be an error")
	}
}

func TestRunner_StopCancelsTheLoopAndWaitsForIt(t *testing.T) {
	const ns = "ach"
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "x", Namespace: ns, UID: "uid-x"}}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: render.ControlName("x") + "-0", Namespace: ns}, Status: corev1.PodStatus{Phase: corev1.PodRunning}}
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(a, pod).Build()

	started, release, finished := make(chan struct{}), make(chan struct{}), make(chan struct{})
	r := &Runner{Client: c, Reader: c, Namespace: ns, Log: logr.Discard()}
	r.run = func(ctx context.Context, _ *achv1alpha1.Agent) {
		close(started)
		<-ctx.Done()
		<-release // an in-flight create that is still returning
		close(finished)
	}
	loops := map[types.UID]context.CancelFunc{}
	var wg sync.WaitGroup
	r.sync(context.Background(), loops, &wg)
	<-started

	stopped := make(chan error, 1)
	go func() { stopped <- r.Stop(context.Background(), "uid-x") }()
	select {
	case err := <-stopped:
		t.Fatalf("Stop returned (%v) before the loop returned", err)
	case <-time.After(100 * time.Millisecond):
	}
	close(release)
	select {
	case err := <-stopped:
		if err != nil {
			t.Fatal(err)
		}
	case <-time.After(5 * time.Second):
		t.Fatal("Stop did not return after the loop did")
	}
	select {
	case <-finished:
	default:
		t.Fatal("Stop returned before the loop finished")
	}
	wg.Wait()
	if err := r.Stop(context.Background(), "uid-x"); err != nil {
		t.Fatalf("stopping a gone loop: %v", err)
	}
}

func TestRunner_StopHonoursItsContext(t *testing.T) {
	const ns = "ach"
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "x", Namespace: ns, UID: "uid-x"}}
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: render.ControlName("x") + "-0", Namespace: ns}, Status: corev1.PodStatus{Phase: corev1.PodRunning}}
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(a, pod).Build()
	started, release := make(chan struct{}), make(chan struct{})
	r := &Runner{Client: c, Reader: c, Namespace: ns, Log: logr.Discard()}
	r.run = func(ctx context.Context, _ *achv1alpha1.Agent) {
		close(started)
		<-release
	}
	loops := map[types.UID]context.CancelFunc{}
	var wg sync.WaitGroup
	r.sync(context.Background(), loops, &wg)
	<-started
	ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
	defer cancel()
	if err := r.Stop(ctx, "uid-x"); !errors.Is(err, context.DeadlineExceeded) {
		t.Fatalf("Stop = %v, want the context's deadline", err)
	}
	close(release)
	wg.Wait()
}
