// SPDX-License-Identifier: MIT

package host

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"time"

	"github.com/go-logr/logr"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// ErrGatewayUnavailable marks a backend error caused by an unreachable sandbox gateway. While
// it lasts the host never removes a unit and never confirms a stop or reports a unit lost:
// "cannot see" is not "gone".
var ErrGatewayUnavailable = errors.New("sandbox gateway unavailable")

var (
	errNotDesired    = errors.New("registration refused: the Workspace is no longer desired (stale listing), relisting")
	errAgentGone     = errors.New("Agent is gone or being deleted")
	errStillAdopting = errors.New("control unit still adopting after the inventory was accepted")
)

// Host reconciles ONE Agent's execution units against its control unit's /host/v1.
// It is a single sequential loop: no other goroutine sends host API mutations for the agent.
type Host struct {
	Agent types.NamespacedName
	UID   types.UID // an Agent recreated under the same name is a different Host
	API   *Client
	// Client writes, and reads Agent/AgentProfile from the informer cache.
	Client client.Client
	// Reader is uncached. It decides whether to create a unit, whether a unit is really gone
	// before a DELETE confirms it, and what the inventory reports: a stale cache must never
	// double-create or confirm a stop that did not happen.
	Reader client.Reader
	Scheme *runtime.Scheme
	// Backend runs the units.
	Backend Backend
	// Status reports what the host loop alone sees. Nil: nothing is reported.
	Status StatusWriter
	Log    logr.Logger

	createFailure string // reason of the Ready=False this loop last wrote for a refused create

	gatewayDown     bool  // the last backend call saw the gateway unavailable
	gatewayReported *bool // the availability the control unit was last told, nil: never told
}

func (h *Host) load(ctx context.Context) (*achv1alpha1.Agent, *achv1alpha1.AgentProfile, error) {
	var a achv1alpha1.Agent
	if err := h.Client.Get(ctx, h.Agent, &a); err != nil {
		return nil, nil, err
	}
	if a.UID != h.UID || !a.DeletionTimestamp.IsZero() {
		return nil, nil, errAgentGone
	}
	var p achv1alpha1.AgentProfile
	if err := h.Client.Get(ctx, types.NamespacedName{Namespace: a.Namespace, Name: a.Spec.ProfileRef.Name}, &p); err != nil {
		return nil, nil, err
	}
	return &a, &p, nil
}

// create loads the agent and profile, then starts the unit. A create the backend refuses for
// a reason the Agent's author can fix (Backend errors implementing StatusReason) is written to
// the Agent's Ready condition; the next successful create clears it.
func (h *Host) create(ctx context.Context, w Workspace) error {
	a, p, err := h.load(ctx)
	if err != nil {
		return err
	}
	err = h.Backend.Create(ctx, a, p, w)
	var r reasoned
	switch {
	case errors.As(err, &r) && h.Status != nil:
		h.createFailure = r.StatusReason()
		if serr := h.Status.SetCondition(ctx, h.Agent, metav1.Condition{
			Type: "Ready", Status: metav1.ConditionFalse, Reason: h.createFailure, Message: err.Error(), ObservedGeneration: a.Generation,
		}); serr != nil {
			h.Log.Error(serr, "report the refused sandbox on the Agent", "reason", h.createFailure)
		}
	case err == nil && h.createFailure != "" && h.Status != nil:
		h.createFailure = ""
		if serr := h.Status.SetCondition(ctx, h.Agent, metav1.Condition{
			Type: "Ready", Status: metav1.ConditionUnknown, Reason: "SandboxCreated", ObservedGeneration: a.Generation,
		}); serr != nil {
			h.Log.Error(serr, "clear the refused-sandbox condition")
		}
	}
	return err
}

func (h *Host) ensureRunning(ctx context.Context, w Workspace) (pending bool, err error) {
	b := h.Backend
	st, err := b.State(ctx, w.Name)
	if err != nil {
		return false, err
	}
	switch st {
	case UnitAbsent:
		if w.Instance != nil {
			return false, nil // vanished behind a registration: the runtime notices and recovers by itself
		}
		return true, h.create(ctx, w) // registered on a later pass, once started
	case UnitTerminating:
		return true, nil
	case UnitLost:
		// Contract 5.4 rule 4: tell the runtime now instead of letting it find out (~60 s).
		// Latency is one host pass. Never before a registration, never without a listed
		// instanceId. An unregistered lost unit is removed so the next pass creates a new one;
		// after the DELETE the runtime lists the Workspace unregistered, which lands here.
		if w.Instance == nil {
			h.Log.Info("removing a lost unregistered unit", "workspace", w.Name)
			_, err := b.Remove(ctx, w.Name)
			return true, err
		}
		if w.Instance.InstanceID != nil && w.Instance.BaseURL == b.BaseURL(w) {
			h.Log.Info("reporting a lost unit", "workspace", w.Name, "instanceId", *w.Instance.InstanceID)
			return true, h.API.Delete(ctx, w.Name, w.Instance.InstanceID)
		}
		return true, nil
	}
	want := b.BaseURL(w)
	if w.Instance != nil && w.Instance.BaseURL == want {
		return false, nil
	}
	if st != UnitStarted {
		return true, nil // the contract registers a STARTED unit
	}
	outcome, err := h.API.Register(ctx, w.Name, want)
	if err != nil {
		return false, err
	}
	switch outcome {
	case NotDesired:
		return false, errNotDesired
	case AlreadyRegistered:
		h.Log.V(1).Info("another baseUrl is registered; the runtime keeps it", "workspace", w.Name)
	}
	return false, nil
}

// retire removes the unit and confirms the stop only when nothing of it remains.
func (h *Host) retire(ctx context.Context, w Workspace) (pending bool, err error) {
	remaining, err := h.Backend.Remove(ctx, w.Name)
	if err != nil || remaining {
		return remaining, err
	}
	var id *string
	if w.Instance != nil {
		id = w.Instance.InstanceID
	}
	h.Log.V(1).Info("confirmed stop", "workspace", w.Name)
	return false, h.API.Delete(ctx, w.Name, id)
}

// GatewayDown reports whether the last backend call found the gateway unavailable.
func (h *Host) GatewayDown() bool { return h.gatewayDown }

// noteGateway records the outcome of the backend calls of one step and, on a transition,
// tells the Agent's status; syncGateway tells the control unit.
func (h *Host) noteGateway(ctx context.Context, err error) {
	down := errors.Is(err, ErrGatewayUnavailable)
	if down == h.gatewayDown {
		return
	}
	h.gatewayDown = down
	if h.Status == nil {
		return
	}
	cond := metav1.Condition{Type: "GatewayAvailable", Status: metav1.ConditionTrue, Reason: "GatewayAvailable"}
	if down {
		cond = metav1.Condition{Type: "GatewayAvailable", Status: metav1.ConditionFalse, Reason: "GatewayUnavailable", Message: err.Error()}
	}
	if serr := h.Status.SetCondition(ctx, h.Agent, cond); serr != nil {
		h.Log.Error(serr, "report the gateway on the Agent")
	}
}

// syncGateway tells the control unit about a change of the gateway's availability. A failed
// call is retried by the next pass: gatewayReported only moves on success.
func (h *Host) syncGateway(ctx context.Context) {
	if h.gatewayReported != nil && *h.gatewayReported == !h.gatewayDown {
		return
	}
	available := !h.gatewayDown
	if err := h.API.Gateway(ctx, available); err != nil {
		h.Log.Error(err, "report the gateway to the control unit", "available", available)
		return
	}
	h.gatewayReported = &available
}

// Pass reconciles one complete listing (adopting == false). It never waits on Kubernetes: a
// terminating pod only makes `pending` true, and the pass moves on to the next Workspace.
// One failing Workspace does not stop the others; the errors are joined. The first backend
// call that finds the gateway unavailable ends the pass: nothing is removed or confirmed
// while the gateway cannot be seen.
func (h *Host) Pass(ctx context.Context, l *Listing) (pending bool, err error) {
	if _, _, err := h.load(ctx); err != nil {
		return false, err
	}
	var errs []error
	defer func() {
		h.noteGateway(ctx, errors.Join(errs...))
		h.syncGateway(ctx)
	}()
	listed := make(map[string]struct{}, len(l.Workspaces))
	for _, w := range l.Workspaces {
		listed[w.Name] = struct{}{}
		var p bool
		var e error
		switch w.Desired {
		case "running":
			p, e = h.ensureRunning(ctx, w)
		case "released":
			p, e = h.retire(ctx, w)
		}
		if e != nil {
			errs = append(errs, fmt.Errorf("%s: %w", w.Name, e))
			if errors.Is(e, ErrGatewayUnavailable) {
				return true, errors.Join(errs...)
			}
			continue
		}
		pending = pending || p
	}
	// The list is complete: a Workspace that is not listed must have no unit.
	owned, e := h.Backend.Owned(ctx)
	if e != nil {
		errs = append(errs, e)
		return pending, errors.Join(errs...)
	}
	for _, name := range owned {
		if _, ok := listed[name]; ok {
			continue
		}
		remaining, e := h.Backend.Remove(ctx, name)
		if e != nil {
			errs = append(errs, fmt.Errorf("%s: %w", name, e))
			if errors.Is(e, ErrGatewayUnavailable) {
				return true, errors.Join(errs...)
			}
			continue
		}
		pending = pending || remaining
	}
	return pending, errors.Join(errs...)
}

// Inventory lists the live units for the adoption inventory.
func (h *Host) Inventory(ctx context.Context) ([]InventoryUnit, error) {
	if _, _, err := h.load(ctx); err != nil {
		return nil, err
	}
	units, err := h.Backend.Live(ctx)
	if units == nil {
		units = []InventoryUnit{}
	}
	return units, err
}

// State is the loop's memory between Sync calls.
type State struct {
	Epoch   string // the runtime epoch an inventory was sent for, or that was seen already adopted
	Cursor  string // "" answers at once
	Pending bool   // a unit is still terminating or starting: poll sooner
}

// Sync is one iteration: list, then either send the inventory (adopting) or reconcile.
func (h *Host) Sync(ctx context.Context, s *State) error {
	wait := 30
	if s.Pending {
		wait = 2
	}
	l, err := h.API.List(ctx, s.Cursor, wait)
	if err != nil {
		s.Cursor = ""
		return err
	}
	if l.RuntimeEpoch != s.Epoch && l.Adopting {
		units, err := h.Inventory(ctx)
		h.noteGateway(ctx, err)
		if err != nil {
			h.syncGateway(ctx) // best effort: the inventory itself cannot be sent
			return err
		}
		if err := h.API.Inventory(ctx, l.RuntimeEpoch, units); err != nil {
			if errors.Is(err, ErrStaleEpoch) {
				s.Cursor = ""
				return nil
			}
			return err
		}
		s.Epoch, s.Cursor = l.RuntimeEpoch, ""
		// A new inventory starts a new epoch on the control unit: tell it the gateway's state.
		h.gatewayReported = nil
		h.syncGateway(ctx)
		return nil
	}
	if l.Adopting { // while adopting: create, delete and PUT nothing
		s.Cursor = ""
		return errStillAdopting
	}
	s.Epoch = l.RuntimeEpoch
	pending, err := h.Pass(ctx, l)
	s.Pending = pending
	if err != nil {
		s.Cursor = ""
		return err
	}
	s.Cursor = l.Cursor
	return nil
}

// backoff is 1s doubling to 30s, with jitter down to half.
func backoff(failures int) time.Duration {
	d := min(time.Second<<min(max(failures, 1)-1, 5), 30*time.Second)
	return d/2 + rand.N(d/2+1) //nolint:gosec // G404: retry jitter, not a secret
}

func sleep(ctx context.Context, d time.Duration) bool {
	t := time.NewTimer(d)
	defer t.Stop()
	select {
	case <-ctx.Done():
		return false
	case <-t.C:
		return true
	}
}

// Run loops until ctx is done (nil) or the host token is rejected (ErrUnauthorized: the
// caller re-reads the key). Errors, including connection refused while the control pod
// starts, back off with jitter, capped at 30 s.
func (h *Host) Run(ctx context.Context) error {
	var s State
	failures := 0
	for ctx.Err() == nil {
		err := h.Sync(ctx, &s)
		switch {
		case err == nil:
			failures = 0
		case ctx.Err() != nil:
			return nil
		case errors.Is(err, ErrUnauthorized):
			return err
		default:
			failures++
			h.Log.Error(err, "host sync failed", "agent", h.Agent.String(), "failures", failures)
			if !sleep(ctx, backoff(failures)) {
				return nil
			}
		}
	}
	return nil
}
