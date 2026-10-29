// SPDX-License-Identifier: MIT

package host

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"net/http/httptest"
	"slices"
	"sort"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/go-logr/logr"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	clientgoscheme "k8s.io/client-go/kubernetes/scheme"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// fakeRuntime is the control unit as seen by the operator: it serves one listing and records
// every mutation. putStatus/putBody script the answer to a PUT of an instance.
type fakeRuntime struct {
	mu                 sync.Mutex
	listing            Listing
	calls              []string
	putStatus          int
	putBody            string
	inventoryStatus    int
	inventoryUnitNames []string
	gateways           []bool // bodies of PUT /gateway, in order
}

func (f *fakeRuntime) lastGateway() (available, ok bool) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if len(f.gateways) == 0 {
		return false, false
	}
	return f.gateways[len(f.gateways)-1], true
}

func (f *fakeRuntime) gatewayCalls() []bool {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]bool(nil), f.gateways...)
}

func (f *fakeRuntime) record(format string, args ...any) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.calls = append(f.calls, fmt.Sprintf(format, args...))
}

func (f *fakeRuntime) Calls() []string {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]string(nil), f.calls...)
}

func (f *fakeRuntime) handler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /host/v1/workspaces", func(w http.ResponseWriter, r *http.Request) {
		f.mu.Lock()
		defer f.mu.Unlock()
		_ = json.NewEncoder(w).Encode(f.listing)
	})
	mux.HandleFunc("PUT /host/v1/workspaces/{name}/instance", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			BaseURL string `json:"baseUrl"`
		}
		_ = json.NewDecoder(r.Body).Decode(&b)
		f.record("PUT %s %s", r.PathValue("name"), b.BaseURL)
		status := f.putStatus
		if status == 0 {
			status = 202
		}
		w.WriteHeader(status)
		_, _ = w.Write([]byte(f.putBody))
	})
	mux.HandleFunc("DELETE /host/v1/workspaces/{name}/instance", func(w http.ResponseWriter, r *http.Request) {
		f.record("DELETE %s %s", r.PathValue("name"), r.URL.Query().Get("instanceId"))
		w.WriteHeader(204)
	})
	mux.HandleFunc("PUT /host/v1/inventory", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			RuntimeEpoch string          `json:"runtimeEpoch"`
			Units        []InventoryUnit `json:"units"`
		}
		_ = json.NewDecoder(r.Body).Decode(&b)
		names := make([]string, 0, len(b.Units))
		for _, u := range b.Units {
			names = append(names, u.Name)
		}
		f.record("INVENTORY %s [%s]", b.RuntimeEpoch, strings.Join(names, ","))
		status := f.inventoryStatus
		if status == 0 {
			status = 202
		}
		w.WriteHeader(status)
	})
	mux.HandleFunc("PUT /host/v1/gateway", func(w http.ResponseWriter, r *http.Request) {
		var b struct {
			Available bool `json:"available"`
		}
		_ = json.NewDecoder(r.Body).Decode(&b)
		f.mu.Lock()
		f.gateways = append(f.gateways, b.Available)
		f.mu.Unlock()
		w.WriteHeader(204)
	})
	return mux
}

func testScheme(t *testing.T) *runtime.Scheme {
	t.Helper()
	s := runtime.NewScheme()
	if err := clientgoscheme.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	if err := achv1alpha1.AddToScheme(s); err != nil {
		t.Fatal(err)
	}
	return s
}

// fakeUnits is an in-memory Backend. Remove drops a unit unless it is listed in sticky, which
// models a pod that is still terminating.
type fakeUnits struct {
	state     map[string]UnitState
	sticky    map[string]bool
	created   []string
	removed   []string
	createErr error
	err       error // returned by every call but Create and BaseURL, like an unreachable gateway
}

func newFakeUnits() *fakeUnits {
	return &fakeUnits{state: map[string]UnitState{}, sticky: map[string]bool{}}
}

func (f *fakeUnits) State(_ context.Context, name string) (UnitState, error) {
	return f.state[name], nil
}

func (f *fakeUnits) Create(_ context.Context, _ *achv1alpha1.Agent, _ *achv1alpha1.AgentProfile, w Workspace) error {
	if f.createErr != nil {
		return f.createErr
	}
	f.created = append(f.created, w.Name)
	f.state[w.Name] = UnitStarting
	return nil
}

func (f *fakeUnits) BaseURL(w Workspace) string { return "http://" + w.Name + ".units" }

func (f *fakeUnits) Remove(_ context.Context, name string) (bool, error) {
	if f.err != nil {
		return false, f.err
	}
	f.removed = append(f.removed, name)
	if _, ok := f.state[name]; !ok {
		return false, nil
	}
	if f.sticky[name] {
		f.state[name] = UnitTerminating
		return true, nil
	}
	delete(f.state, name)
	return false, nil
}

func (f *fakeUnits) Owned(context.Context) ([]string, error) {
	if f.err != nil {
		return nil, f.err
	}
	names := make([]string, 0, len(f.state))
	for n := range f.state {
		names = append(names, n)
	}
	sort.Strings(names)
	return names, nil
}

func (f *fakeUnits) Live(ctx context.Context) ([]InventoryUnit, error) {
	if f.err != nil {
		return nil, f.err
	}
	units := []InventoryUnit{}
	names, _ := f.Owned(ctx)
	for _, n := range names {
		if f.state[n] == UnitStarted {
			units = append(units, InventoryUnit{Name: n, WorkspaceRef: "ref-" + n, BaseURL: "http://" + n + ".units"})
		}
	}
	return units, nil
}

func newHost(t *testing.T, rt *fakeRuntime, units *fakeUnits) *Host {
	t.Helper()
	a := &achv1alpha1.Agent{ObjectMeta: metav1.ObjectMeta{Name: "gitlab-reviewer", Namespace: "ach", UID: types.UID(testUID)},
		Spec: achv1alpha1.AgentSpec{ProfileRef: achv1alpha1.LocalObjectRef{Name: "prof"}}}
	p := &achv1alpha1.AgentProfile{ObjectMeta: metav1.ObjectMeta{Name: "prof", Namespace: "ach"}}
	scheme := testScheme(t)
	c := fake.NewClientBuilder().WithScheme(scheme).WithObjects(a, p).WithStatusSubresource(a).Build()
	srv := httptest.NewServer(rt.handler())
	t.Cleanup(srv.Close)
	return &Host{
		Agent: types.NamespacedName{Namespace: "ach", Name: a.Name}, UID: a.UID,
		API: &Client{BaseURL: srv.URL + "/host/v1", Token: "t"}, Client: c, Reader: c, Scheme: scheme, Log: logr.Discard(),
		Backend: units,
	}
}

func ws(name, desired string, instance *Instance) Workspace {
	return Workspace{Name: name, WorkspaceRef: "ref-" + name, Desired: desired, Instance: instance,
		Launch: Launch{Port: 8080, Command: []string{"run"}}}
}

func listing(w ...Workspace) *Listing { return &Listing{Workspaces: w} }

func TestPass_CreatesUnitThenRegistersOnceItIsStarted(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	l := listing(ws("w1", "running", nil))
	pending, err := h.Pass(context.Background(), l)
	if err != nil || !pending || !slices.Equal(u.created, []string{"w1"}) {
		t.Fatalf("Pass = (%v, %v) created = %v, want pending after one create", pending, err, u.created)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Fatalf("calls = %v: registering before the unit runs lets the runtime cache an NXDOMAIN for its DNS name", got)
	}
	u.state["w1"] = UnitStarted
	if pending, err = h.Pass(context.Background(), l); err != nil || pending {
		t.Fatalf("second Pass = (%v, %v)", pending, err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "PUT w1 http://w1.units" {
		t.Errorf("calls = %v", got)
	}
	if len(u.created) != 1 {
		t.Errorf("created = %v, want a single create", u.created)
	}
}

func TestPass_RunningAndAlreadyRegisteredDoesNothing(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitStarted
	h := newHost(t, rt, u)
	in := &Instance{BaseURL: "http://w1.units", State: "serving"}
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", in))); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Errorf("calls = %v, want none", got)
	}
}

func TestPass_RunningWithoutUnitButRegisteredIsLeftToTheRuntime(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	in := &Instance{BaseURL: "http://w1.units", State: "serving"}
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", in))); err != nil {
		t.Fatal(err)
	}
	if len(u.created) != 0 || len(rt.Calls()) != 0 {
		t.Error("a unit missing behind a registration is not recreated by the operator: the runtime recovers it (released, then running)")
	}
}

func TestPass_RepairsMissingRegistrationWithoutRecreating(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitStarted
	h := newHost(t, rt, u)
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", nil))); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 1 || !strings.HasPrefix(got[0], "PUT w1 ") || len(u.created) != 0 {
		t.Errorf("calls = %v created = %v, want one PUT and no create", got, u.created)
	}
}

func TestPass_NotDesiredDeletesNothingAndAsksForARelist(t *testing.T) {
	rt, u := &fakeRuntime{putStatus: 409, putBody: `{"error":"not_desired"}`}, newFakeUnits()
	u.state["w1"] = UnitStarted
	h := newHost(t, rt, u)
	_, err := h.Pass(context.Background(), listing(ws("w1", "running", nil)))
	if !errors.Is(err, errNotDesired) {
		t.Fatalf("err = %v, want errNotDesired", err)
	}
	if len(u.removed) != 0 {
		t.Error("a stale PUT answer must not delete the unit: a new epoch's inventory may adopt it; the fresh listing decides")
	}
}

func TestPass_ReleasedRequestsDeletionAndConfirmsOnlyOnceTheStopIsConfirmed(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"], u.sticky["w1"] = UnitStarted, true
	h := newHost(t, rt, u)
	id := "inst-1"
	l := listing(ws("w1", "released", &Instance{InstanceID: &id, BaseURL: "x", State: "serving"}))

	pending, err := h.Pass(context.Background(), l) // pass 1: the unit is still terminating
	if err != nil || !pending {
		t.Fatalf("pass 1 = (%v, %v), want pending", pending, err)
	}
	if len(u.removed) != 1 {
		t.Error("removal must be requested in the first pass")
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Fatalf("calls = %v: DELETE must wait until nothing of the unit remains (a forced removal could leave the process alive)", got)
	}

	u.sticky["w1"] = false
	pending, err = h.Pass(context.Background(), l) // pass 2: gone
	if err != nil || pending {
		t.Fatalf("pass 2 = (%v, %v)", pending, err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "DELETE w1 inst-1" {
		t.Errorf("calls = %v, want DELETE with the listed instanceId", got)
	}
}

func TestPass_ReleasedWithoutKnownInstanceIDSendsNoQuery(t *testing.T) {
	rt := &fakeRuntime{}
	h := newHost(t, rt, newFakeUnits()) // never created: nothing to wait for
	if _, err := h.Pass(context.Background(), listing(ws("w1", "released", nil))); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "DELETE w1 " {
		t.Errorf("calls = %v", got)
	}
}

// One terminating unit must not block another Workspace.
func TestPass_StuckRetirementDoesNotBlockAnotherActivation(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["stuck"], u.sticky["stuck"] = UnitStarted, true
	h := newHost(t, rt, u)
	id := "i"
	l := listing(
		ws("stuck", "released", &Instance{InstanceID: &id, BaseURL: "x", State: "serving"}),
		ws("fresh", "running", nil),
	)
	pending, err := h.Pass(context.Background(), l)
	if err != nil || !pending {
		t.Fatalf("Pass = (%v, %v), want pending and no error", pending, err)
	}
	if !slices.Equal(u.created, []string{"fresh"}) {
		t.Fatalf("created = %v, the second Workspace must be created in the same pass", u.created)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Errorf("calls = %v, want no DELETE for stuck (it remains) and no PUT yet for fresh (it is not started)", got)
	}
}

func TestPass_UnlistedOwnedUnitsAreRemoved(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["old"] = UnitStarted
	h := newHost(t, rt, u)
	if _, err := h.Pass(context.Background(), &Listing{}); err != nil {
		t.Fatal(err)
	}
	if !slices.Equal(u.removed, []string{"old"}) {
		t.Errorf("removed = %v, an owned unit the runtime does not list must be removed", u.removed)
	}
}

func TestInventory_ListsOnlyStartedUnits(t *testing.T) {
	u := newFakeUnits()
	u.state["alive"], u.state["starting"] = UnitStarted, UnitStarting
	h := newHost(t, &fakeRuntime{}, u)
	units, err := h.Inventory(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	if len(units) != 1 || units[0].Name != "alive" || units[0].BaseURL != "http://alive.units" || units[0].WorkspaceRef != "ref-alive" {
		t.Fatalf("inventory = %+v, want only alive", units)
	}
}

func TestSync_AdoptionSendsInventoryAndChangesNothing(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}}, newFakeUnits()
	u.state["alive"] = UnitStarted
	h := newHost(t, rt, u)
	s := &State{}
	if err := h.Sync(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "INVENTORY e1 [alive]" {
		t.Fatalf("calls = %v, want exactly the inventory", got)
	}
	if len(u.removed) != 0 {
		t.Error("an empty adopting listing is not desired state: nothing may be removed")
	}
	if s.Epoch != "e1" || s.Cursor != "" {
		t.Errorf("state = %+v, want epoch e1 and an immediate relist", *s)
	}
	// Adoption done: the control unit now lists the unit as running/probing.
	rt.mu.Lock()
	rt.listing = Listing{Cursor: "e1.2", RuntimeEpoch: "e1", Workspaces: []Workspace{
		ws("alive", "running", &Instance{BaseURL: "http://alive.units", State: "probing"}),
	}}
	rt.mu.Unlock()
	if err := h.Sync(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	if len(rt.Calls()) != 1 || s.Cursor != "e1.2" {
		t.Errorf("calls = %v, cursor = %q: a registered unit needs no PUT and the cursor advances", rt.Calls(), s.Cursor)
	}
}

func TestSync_StaleInventoryEpochRelistsWithoutRecordingTheEpoch(t *testing.T) {
	rt := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}, inventoryStatus: 409}
	h := newHost(t, rt, newFakeUnits())
	s := &State{}
	if err := h.Sync(context.Background(), s); err != nil {
		t.Fatalf("a stale epoch is retried, not an error: %v", err)
	}
	if s.Epoch != "" || s.Cursor != "" {
		t.Errorf("state = %+v, want epoch unset so the next listing re-sends", *s)
	}
}

func TestSync_NewEpochAlreadyAdoptedNeedsNoInventory(t *testing.T) {
	rt := &fakeRuntime{listing: Listing{Cursor: "e9.3", RuntimeEpoch: "e9"}} // operator restarted, control did not
	h := newHost(t, rt, newFakeUnits())
	s := &State{}
	if err := h.Sync(context.Background(), s); err != nil {
		t.Fatal(err)
	}
	if len(rt.Calls()) != 0 || s.Epoch != "e9" || s.Cursor != "e9.3" {
		t.Errorf("calls = %v, state = %+v", rt.Calls(), *s)
	}
}

func TestSync_PendingShortensTheLongPoll(t *testing.T) {
	var waits []string
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		waits = append(waits, r.URL.Query().Get("waitSeconds"))
		_, _ = w.Write([]byte(`{"cursor":"e.1","runtimeEpoch":"e","adopting":false,"workspaces":[]}`))
	}))
	defer srv.Close()
	h := newHost(t, &fakeRuntime{}, newFakeUnits())
	h.API = &Client{BaseURL: srv.URL + "/host/v1", Token: "t"}
	s := &State{Epoch: "e"}
	_ = h.Sync(context.Background(), s)
	s.Pending = true
	_ = h.Sync(context.Background(), s)
	if len(waits) != 2 || waits[0] != "30" || waits[1] != "2" {
		t.Errorf("waitSeconds = %v, want [30 2]", waits)
	}
}

func TestBackoff_BoundedWithJitter(t *testing.T) {
	for n := 1; n <= 12; n++ {
		d := backoff(n)
		if d <= 0 || d > 30*time.Second {
			t.Fatalf("backoff(%d) = %v, want within (0, 30s]", n, d)
		}
	}
	if backoff(1) > time.Second {
		t.Errorf("backoff(1) = %v, want at most 1s", backoff(1))
	}
}

func TestPass_RunningWithTerminatingUnitIsPending(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["old"] = UnitTerminating
	h := newHost(t, rt, u)
	pending, err := h.Pass(context.Background(), listing(ws("old", "running", nil)))
	if err != nil || !pending {
		t.Fatalf("Pass = (%v, %v), want pending: the replacement is created when the old unit is gone", pending, err)
	}
	if got := rt.Calls(); len(got) != 0 || len(u.created) != 0 {
		t.Errorf("calls = %v created = %v, want none", got, u.created)
	}
}

func TestSync_StillAdoptingAfterInventoryMutatesNothing(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}}, newFakeUnits()
	u.state["alive"] = UnitStarted
	h := newHost(t, rt, u)
	s := &State{Epoch: "e1"} // the inventory for e1 was already accepted
	if err := h.Sync(context.Background(), s); !errors.Is(err, errStillAdopting) {
		t.Fatalf("Sync = %v, want errStillAdopting", err)
	}
	if got := rt.Calls(); len(got) != 0 || len(u.removed) != 0 {
		t.Errorf("calls = %v removed = %v, want no PUT, DELETE or removal while adopting", got, u.removed)
	}
}

func TestPass_LostRegisteredUnitIsReportedWithTheListedInstanceID(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitLost
	h := newHost(t, rt, u)
	id := "inst-1"
	in := &Instance{InstanceID: &id, BaseURL: "http://w1.units", State: "serving"}
	pending, err := h.Pass(context.Background(), listing(ws("w1", "running", in)))
	if err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "DELETE w1 inst-1" {
		t.Errorf("calls = %v, want exactly one DELETE carrying the listed instanceId (contract 5.4 rule 4)", got)
	}
	if !pending {
		t.Error("a lost unit keeps the loop polling sooner")
	}
}

func TestPass_LostUnitWithoutARegistrationIsRemovedNotReported(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitLost
	h := newHost(t, rt, u)
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", nil))); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Errorf("calls = %v: nothing was registered, so there is nothing to report", got)
	}
	if !slices.Equal(u.removed, []string{"w1"}) {
		t.Errorf("removed = %v, want the stale unit removed once so a new one is created", u.removed)
	}
}

func TestPass_LostUnitWithoutAnInstanceIDSendsNoDelete(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitLost
	h := newHost(t, rt, u)
	in := &Instance{BaseURL: "http://w1.units", State: "starting"} // id not known yet
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", in))); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 0 || len(u.removed) != 0 {
		t.Errorf("calls = %v removed = %v, want nothing without an instanceId", got, u.removed)
	}
}

func TestPass_LostUnitIsNotReportedAgainstAnotherRegistration(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"] = UnitLost
	h := newHost(t, rt, u)
	id := "inst-1"
	in := &Instance{InstanceID: &id, BaseURL: "http://sandbox.elsewhere:8080", State: "serving"}
	pending, err := h.Pass(context.Background(), listing(ws("w1", "running", in)))
	if err != nil || !pending || len(rt.Calls()) != 0 {
		t.Errorf("Pass = (%v, %v) calls = %v, want pending and no DELETE: the registration is not this unit's", pending, err, rt.Calls())
	}
}

type fakeStatus struct{ conds []metav1.Condition }

func (f *fakeStatus) SetCondition(_ context.Context, _ types.NamespacedName, c metav1.Condition) error {
	f.conds = append(f.conds, c)
	return nil
}

func condsOf(f *fakeStatus, typ string) []metav1.Condition {
	var out []metav1.Condition
	for _, c := range f.conds {
		if c.Type == typ {
			out = append(out, c)
		}
	}
	return out
}

type refusal struct{ error }

func (refusal) StatusReason() string { return "SandboxPolicyRejected" }

func TestPass_RefusedCreateIsReportedOnTheAgentAndClearedOnSuccess(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	l := listing(ws("w1", "running", nil))

	u.createErr = refusal{errors.New("rule name \"ach-agent-x\" is reserved")}
	if _, err := h.Pass(context.Background(), l); err == nil {
		t.Fatal("a refused create must fail the pass")
	}
	if rc := condsOf(st, "Ready"); len(rc) != 1 || rc[0].Status != metav1.ConditionFalse ||
		rc[0].Reason != "SandboxPolicyRejected" || !strings.Contains(rc[0].Message, "reserved") {
		t.Fatalf("conditions = %+v", st.conds)
	}
	setReady(t, h, "SandboxPolicyRejected") // what the status writer stored

	u.createErr = nil
	if _, err := h.Pass(context.Background(), l); err != nil {
		t.Fatal(err)
	}
	if rc := condsOf(st, "Ready"); len(rc) != 2 || rc[1].Status != metav1.ConditionUnknown || rc[1].Reason != "SandboxCreated" {
		t.Fatalf("conditions = %+v, want the refusal cleared", st.conds)
	}
	// An ordinary error is not the Agent author's to fix: nothing is reported.
	u2 := newFakeUnits()
	u2.createErr = errors.New("gateway exploded")
	h2 := newHost(t, rt, u2)
	st2 := &fakeStatus{}
	h2.Status = st2
	_, _ = h2.Pass(context.Background(), l)
	if rc := condsOf(st2, "Ready"); len(rc) != 0 {
		t.Fatalf("conditions = %+v, want no Ready", st2.conds)
	}
}

var errDown = fmt.Errorf("%w: connection refused", ErrGatewayUnavailable)

func TestPass_GatewayDownReportsUnavailableAndNeverConfirmsAStopOrRemovesAUnit(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	u.state["w1"], u.state["lost"], u.state["unlisted"] = UnitStarted, UnitLost, UnitStarted
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	id := "inst-1"
	l := listing(
		ws("w1", "released", &Instance{InstanceID: &id, BaseURL: "x", State: "serving"}),
		ws("lost", "running", &Instance{InstanceID: &id, BaseURL: "http://lost.units", State: "serving"}),
	)
	u.err = errDown
	pending, err := h.Pass(context.Background(), l)
	if !errors.Is(err, ErrGatewayUnavailable) || !pending {
		t.Fatalf("Pass = (%v, %v), want pending with the outage", pending, err)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Fatalf("calls = %v: a stop was confirmed or a unit reported lost while the gateway was unavailable", got)
	}
	if len(u.removed) != 0 || len(u.created) != 0 {
		t.Fatalf("removed = %v created = %v, want nothing touched", u.removed, u.created)
	}
	if got := rt.gatewayCalls(); len(got) != 1 || got[0] {
		t.Fatalf("gateway reports = %v, want exactly one available=false", got)
	}
	if !h.GatewayDown() {
		t.Fatal("host does not track the outage")
	}
	if len(st.conds) != 1 || st.conds[0].Type != "GatewayAvailable" || st.conds[0].Status != metav1.ConditionFalse || st.conds[0].Reason != "GatewayUnavailable" {
		t.Fatalf("conditions = %+v", st.conds)
	}
	// Still down: nothing new is reported.
	_, _ = h.Pass(context.Background(), l)
	if got := rt.gatewayCalls(); len(got) != 1 || len(st.conds) != 1 {
		t.Fatalf("outage re-reported: gateway = %v conditions = %+v", got, st.conds)
	}
}

func TestPass_GatewayBackReportsAvailableOnce(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	l := listing()
	u.err = errDown
	_, _ = h.Pass(context.Background(), l)
	u.err = nil
	for range 3 {
		if _, err := h.Pass(context.Background(), l); err != nil {
			t.Fatal(err)
		}
	}
	if got := rt.gatewayCalls(); len(got) != 2 || got[0] || !got[1] {
		t.Fatalf("gateway reports = %v, want [false true]", got)
	}
	if h.GatewayDown() {
		t.Fatal("host still believes the gateway is down")
	}
	if len(st.conds) != 2 || st.conds[1].Type != "GatewayAvailable" || st.conds[1].Status != metav1.ConditionTrue {
		t.Fatalf("conditions = %+v, want False then True", st.conds)
	}
}

func TestPass_ARejectedGatewayReportIsRetriedNextPass(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	h.API = &Client{BaseURL: "http://127.0.0.1:1/host/v1", Token: "t"} // control unit unreachable
	u.err = errDown
	_, _ = h.Pass(context.Background(), listing())
	if h.gatewayReported != nil {
		t.Fatal("an unreported outage was recorded as reported")
	}
}

func TestSync_ReportsGatewayAfterInventory(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}}, newFakeUnits()
	u.state["alive"] = UnitStarted
	h := newHost(t, rt, u)
	if err := h.Sync(context.Background(), &State{}); err != nil {
		t.Fatal(err)
	}
	if got := rt.Calls(); len(got) != 1 || got[0] != "INVENTORY e1 [alive]" {
		t.Fatalf("calls = %v", got)
	}
	if got := rt.gatewayCalls(); len(got) != 1 || !got[0] {
		t.Fatalf("gateway reports = %v, want one available=true after the inventory", got)
	}
}

func TestSync_InventoryWithTheGatewayDownIsNotSentAndTheOutageIsReported(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}}, newFakeUnits()
	u.err = errDown
	h := newHost(t, rt, u)
	s := &State{}
	if err := h.Sync(context.Background(), s); !errors.Is(err, ErrGatewayUnavailable) {
		t.Fatalf("Sync = %v, want the outage", err)
	}
	if got := rt.Calls(); len(got) != 0 {
		t.Fatalf("calls = %v: an inventory built without the gateway would claim there are no units", got)
	}
	if s.Epoch != "" {
		t.Fatalf("epoch = %q recorded for an inventory that was never sent", s.Epoch)
	}
	if got := rt.gatewayCalls(); len(got) != 1 || got[0] {
		t.Fatalf("gateway reports = %v, want available=false", got)
	}
}

func setReady(t *testing.T, h *Host, reason string) {
	t.Helper()
	var a achv1alpha1.Agent
	if err := h.Reader.Get(context.Background(), h.Agent, &a); err != nil {
		t.Fatal(err)
	}
	a.Status.Conditions = []metav1.Condition{{Type: "Ready", Status: metav1.ConditionFalse, Reason: reason, LastTransitionTime: metav1.Now()}}
	if err := h.Client.Status().Update(context.Background(), &a); err != nil {
		t.Fatal(err)
	}
}

func TestPass_ARestartedLoopClearsARefusalAnEarlierLoopWrote(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u) // a fresh Host: it never saw the refusal itself
	st := &fakeStatus{}
	h.Status = st
	setReady(t, h, "SandboxPolicyRejected")
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", nil))); err != nil {
		t.Fatal(err)
	}
	if rc := condsOf(st, "Ready"); len(rc) != 1 || rc[0].Reason != "SandboxCreated" {
		t.Fatalf("conditions = %+v, want the refusal cleared", st.conds)
	}
	// Any other Ready state is left alone.
	h2 := newHost(t, rt, newFakeUnits())
	st2 := &fakeStatus{}
	h2.Status = st2
	setReady(t, h2, "SecretMissing")
	if _, err := h2.Pass(context.Background(), listing(ws("w1", "running", nil))); err != nil {
		t.Fatal(err)
	}
	if rc := condsOf(st2, "Ready"); len(rc) != 0 {
		t.Fatalf("conditions = %+v, want Ready untouched", st2.conds)
	}
}

func TestPass_NewHostWritesGatewayAvailableAndRetriesAFailedWrite(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &failingStatus{fakeStatus: &fakeStatus{}, fail: 1}
	h.Status = st
	for range 3 {
		if _, err := h.Pass(context.Background(), listing()); err != nil {
			t.Fatal(err)
		}
	}
	if gc := condsOf(st.fakeStatus, "GatewayAvailable"); len(gc) != 1 || gc[0].Status != metav1.ConditionTrue {
		t.Fatalf("conditions = %+v, want one True after the failed first write", st.conds)
	}
	if st.attempts != 2 {
		t.Fatalf("attempts = %d, want 2 (one failure, one retry, then settled)", st.attempts)
	}
}

type failingStatus struct {
	*fakeStatus
	fail, attempts int
}

func (f *failingStatus) SetCondition(ctx context.Context, k types.NamespacedName, c metav1.Condition) error {
	f.attempts++
	if f.attempts <= f.fail {
		return errors.New("apiserver unavailable")
	}
	return f.fakeStatus.SetCondition(ctx, k, c)
}

func TestSync_NewEpochWhileDownReportsTheOutageAgain(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1"}}, newFakeUnits()
	u.err = errDown
	h := newHost(t, rt, u)
	s := &State{}
	_ = h.Sync(context.Background(), s) // epoch e1, down: reported false
	if got := rt.gatewayCalls(); len(got) != 1 || got[0] {
		t.Fatalf("gateway reports = %v, want [false]", got)
	}
	// The control container restarted in place: a new epoch that starts with the gateway up.
	rt.mu.Lock()
	rt.listing = Listing{Cursor: "e2.1", RuntimeEpoch: "e2", Adopting: true}
	rt.mu.Unlock()
	_ = h.Sync(context.Background(), s)
	if got := rt.gatewayCalls(); len(got) != 2 || got[1] {
		t.Fatalf("gateway reports = %v, want a second available=false for the new epoch", got)
	}
}

func TestSync_InventoryLoadFailureDoesNotMarkTheGatewayAvailable(t *testing.T) {
	rt, u := &fakeRuntime{listing: Listing{Cursor: "e1.1", RuntimeEpoch: "e1", Adopting: true}}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	h.gatewayDown = true
	h.UID = "other" // load fails with errAgentGone, not a gateway error
	if err := h.Sync(context.Background(), &State{}); err == nil {
		t.Fatal("want the load error")
	}
	if !h.gatewayDown || len(st.conds) != 0 {
		t.Fatalf("gatewayDown = %v conditions = %+v: a load failure is no evidence about the gateway", h.gatewayDown, st.conds)
	}
}

func TestPass_AStandingRefusalIsWrittenOnceNotEveryPass(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	u.createErr = refusal{errors.New("rule rejected")}
	l := listing(ws("w1", "running", nil))
	for range 3 {
		if _, err := h.Pass(context.Background(), l); err == nil {
			t.Fatal("a refused create must fail the pass")
		}
	}
	rc := condsOf(st, "Ready")
	if len(rc) != 1 {
		t.Fatalf("Ready written %d times, want once: %+v", len(rc), rc)
	}
	if g, ok := RefusalProfileGeneration(rc[0].Message); !ok || g != 0 {
		t.Errorf("message %q does not record the AgentProfile generation", rc[0].Message)
	}
	// A changed message is written again.
	u.createErr = refusal{errors.New("another rule rejected")}
	_, _ = h.Pass(context.Background(), l)
	if rc := condsOf(st, "Ready"); len(rc) != 2 {
		t.Fatalf("Ready written %d times, want a second write for a new message", len(rc))
	}
}

func TestPass_RefusalIsClearedOnceNoRunningWorkspaceIsRefused(t *testing.T) {
	rt, u := &fakeRuntime{}, newFakeUnits()
	h := newHost(t, rt, u)
	st := &fakeStatus{}
	h.Status = st
	u.createErr = refusal{errors.New("rule rejected")}
	if _, err := h.Pass(context.Background(), listing(ws("w1", "running", nil))); err == nil {
		t.Fatal("a refused create must fail the pass")
	}
	setReady(t, h, "SandboxPolicyRejected") // what the status writer stored
	// The refused Workspace is released; nothing is running-desired any more.
	if _, err := h.Pass(context.Background(), listing(ws("w1", "released", nil))); err != nil {
		t.Fatal(err)
	}
	if rc := condsOf(st, "Ready"); len(rc) != 2 || rc[1].Status != metav1.ConditionUnknown {
		t.Fatalf("conditions = %+v, want the refusal cleared", st.conds)
	}
}
