// SPDX-License-Identifier: MIT

package host

import (
	"context"
	"errors"
	"fmt"
	"sync"
	"time"

	"github.com/go-logr/logr"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const (
	// PauseAnnotation set to "true" on an Agent stops its host loop: no unit is created,
	// registered or removed until the annotation is removed. Units already running keep
	// running. It exists so the kind gate can observe a control unit waiting for its
	// inventory (T04), and for debugging.
	PauseAnnotation = "ach-agent.ackstorm.ai/pause-host"

	// KeySecretDataKey is the data key of the per-agent sandbox key Secret the reconciler
	// creates and this package reads.
	KeySecretDataKey = "key"
)

// KeySecretName is the name of the per-agent sandbox key Secret.
func KeySecretName(agentUID string) string { return "ach-agent-key-" + agentUID }

// Runner starts one Host per Agent whose control pod is Running and stops it when the
// agent is deleted, paused, or its control pod is not Running. It deliberately does NOT wait
// for the Agent to be Ready: Ready follows the control pod's readiness, which waits for
// the inventory this runner sends.
type Runner struct {
	Client    client.Client // informer-backed
	Reader    client.Reader // uncached: Secret reads and the Host's absence decisions
	Scheme    *runtime.Scheme
	Namespace string // "" lists every namespace the cache serves
	Log       logr.Logger
	Interval  time.Duration // agent scan period, default 5s
	// NewBackend builds the unit backend of one agent.
	NewBackend func(agent types.NamespacedName, uid types.UID, reader client.Reader, writer client.Client) Backend

	run func(ctx context.Context, a *achv1alpha1.Agent) // test seam, default runAgent

	mu      sync.Mutex
	running map[types.UID]*loop
}

// loop is one agent's running host loop, for Stop.
type loop struct {
	cancel context.CancelFunc
	done   chan struct{} // closed when the loop goroutine has returned
}

// Stop cancels the host loop of the Agent with this UID and waits until it has returned, so
// no pass (and no sandbox create in flight) outlives the call. A UID without a loop is not an
// error. ctx bounds the wait.
func (r *Runner) Stop(ctx context.Context, uid types.UID) error {
	r.mu.Lock()
	l := r.running[uid]
	r.mu.Unlock()
	if l == nil {
		return nil
	}
	l.cancel()
	select {
	case <-l.done:
		return nil
	case <-ctx.Done():
		return ctx.Err()
	}
}

// NeedLeaderElection: exactly one operator may send host mutations for an agent.
func (r *Runner) NeedLeaderElection() bool { return true }

func (r *Runner) Start(ctx context.Context) error {
	if r.run == nil {
		r.run = r.runAgent
	}
	interval := r.Interval
	if interval <= 0 {
		interval = 5 * time.Second
	}
	loops := map[types.UID]context.CancelFunc{}
	var wg sync.WaitGroup
	t := time.NewTicker(interval)
	defer t.Stop()
	for {
		r.sync(ctx, loops, &wg)
		select {
		case <-ctx.Done():
			wg.Wait() // every loop ends with ctx
			return nil
		case <-t.C:
		}
	}
}

func (r *Runner) controlRunning(ctx context.Context, a *achv1alpha1.Agent) bool {
	var p corev1.Pod
	name := render.ControlName(a.Name) + "-0"
	err := r.Client.Get(ctx, client.ObjectKey{Namespace: a.Namespace, Name: name}, &p)
	return err == nil && p.Status.Phase == corev1.PodRunning && p.DeletionTimestamp.IsZero()
}

// sync starts a loop for every wanted agent without one and cancels the loops of agents that
// are no longer wanted. Loops are keyed by UID: a recreated agent gets a new loop.
func (r *Runner) sync(ctx context.Context, loops map[types.UID]context.CancelFunc, wg *sync.WaitGroup) {
	var opts []client.ListOption
	if r.Namespace != "" {
		opts = append(opts, client.InNamespace(r.Namespace))
	}
	var agents achv1alpha1.AgentList
	if err := r.Client.List(ctx, &agents, opts...); err != nil {
		r.Log.Error(err, "list Agents")
		return
	}
	want := make(map[types.UID]*achv1alpha1.Agent, len(agents.Items))
	for i := range agents.Items {
		a := &agents.Items[i]
		if a.DeletionTimestamp.IsZero() && a.Annotations[PauseAnnotation] != "true" && r.controlRunning(ctx, a) {
			want[a.UID] = a
		}
	}
	for uid, cancel := range loops {
		if _, ok := want[uid]; !ok {
			cancel()
			delete(loops, uid)
		}
	}
	for uid, a := range want {
		if _, ok := loops[uid]; ok {
			continue
		}
		loopCtx, cancel := context.WithCancel(ctx)
		loops[uid] = cancel
		l := &loop{cancel: cancel, done: make(chan struct{})}
		r.mu.Lock()
		if r.running == nil {
			r.running = map[types.UID]*loop{}
		}
		r.running[uid] = l
		r.mu.Unlock()
		wg.Add(1)
		go func(a *achv1alpha1.Agent) {
			defer wg.Done()
			defer func() {
				r.mu.Lock()
				if r.running[a.UID] == l {
					delete(r.running, a.UID)
				}
				r.mu.Unlock()
				close(l.done)
			}()
			r.run(loopCtx, a)
		}(a.DeepCopy())
	}
}

func (r *Runner) key(ctx context.Context, a *achv1alpha1.Agent) ([]byte, error) {
	var s corev1.Secret
	if err := r.Reader.Get(ctx, client.ObjectKey{Namespace: a.Namespace, Name: KeySecretName(string(a.UID))}, &s); err != nil {
		return nil, err
	}
	if len(s.Data[KeySecretDataKey]) == 0 {
		return nil, fmt.Errorf("secret %s has no %q", s.Name, KeySecretDataKey)
	}
	return s.Data[KeySecretDataKey], nil
}

// runAgent runs one agent's Host until ctx ends. A rejected token or an unreadable key is
// retried with backoff after re-reading the Secret. Neither the key nor the token is logged.
func (r *Runner) runAgent(ctx context.Context, a *achv1alpha1.Agent) {
	log := r.Log.WithValues("agent", client.ObjectKeyFromObject(a).String())
	for failures := 1; ctx.Err() == nil; failures++ {
		key, err := r.key(ctx, a)
		if err == nil {
			h := &Host{
				Agent: client.ObjectKeyFromObject(a), UID: a.UID,
				API:    &Client{BaseURL: ControlBaseURL(a.Name, a.Namespace), Token: HostToken(key, string(a.UID))},
				Client: r.Client, Reader: r.Reader, Scheme: r.Scheme, Log: log,
				Backend: r.NewBackend(client.ObjectKeyFromObject(a), a.UID, r.Reader, r.Client),
				Status:  &AgentStatusWriter{Client: r.Client, Reader: r.Reader},
			}
			if err = h.Run(ctx); !errors.Is(err, ErrUnauthorized) {
				return
			}
		}
		log.Error(err, "workspace host loop will retry")
		if !sleep(ctx, backoff(failures)) {
			return
		}
	}
}
