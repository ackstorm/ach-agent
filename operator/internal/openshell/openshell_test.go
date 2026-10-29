// SPDX-License-Identifier: MIT

package openshell

import (
	"context"
	"errors"
	"maps"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/client/fake"

	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
)

// fakeGateway behaves like the OpenShell gateway where the PoC showed it matters: Delete
// drops the record at once while the workload pod lingers (finding 7), and blocks when slow.
type fakeGateway struct {
	mu        sync.Mutex
	byName    map[string]*ostypes.Sandbox
	created   []string
	deleted   []string
	slowDel   bool // Delete waits for ctx (DeadlineExceeded), like the real RPC
	exposures []ostypes.ServiceExposure
	hang      bool // Get, ListAll and Create wait for ctx, like a frozen gateway
	createErr error
}

func (f *fakeGateway) block(ctx context.Context) error {
	if !f.hang {
		return nil
	}
	<-ctx.Done()
	return &ostypes.StatusError{Code: ostypes.ErrorDeadlineExceeded, Message: "deadline", Cause: ctx.Err()}
}

func newFakeGateway() *fakeGateway { return &fakeGateway{byName: map[string]*ostypes.Sandbox{}} }

func (f *fakeGateway) Create(ctx context.Context, _ string, name string, spec *ostypes.SandboxSpec, opts ...ostypes.CreateOption) (*ostypes.Sandbox, error) {
	if err := f.block(ctx); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if f.createErr != nil {
		return nil, f.createErr
	}
	cfg := ostypes.ApplyCreateOptions(opts)
	f.exposures = append(f.exposures, cfg.ServiceExposures()...)
	s := &ostypes.Sandbox{ID: "id-" + name, Name: name, Labels: cfg.Labels(), Spec: *spec,
		Status: ostypes.SandboxStatus{Phase: ostypes.SandboxProvisioning}}
	f.byName[name] = s
	f.created = append(f.created, name)
	return s, nil
}

func (f *fakeGateway) Get(ctx context.Context, _ string, name string) (*ostypes.Sandbox, error) {
	if err := f.block(ctx); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	if s, ok := f.byName[name]; ok {
		return s, nil
	}
	return nil, &ostypes.StatusError{Code: ostypes.ErrorNotFound, Message: "sandbox " + name}
}

func (f *fakeGateway) ListAll(ctx context.Context, _ string, opts ...ostypes.ListOptions) ([]*ostypes.Sandbox, error) {
	if err := f.block(ctx); err != nil {
		return nil, err
	}
	f.mu.Lock()
	defer f.mu.Unlock()
	want := map[string]string{}
	for _, o := range opts {
		for _, kv := range strings.Split(o.LabelSelector, ",") {
			if k, v, ok := strings.Cut(kv, "="); ok {
				want[k] = v
			}
		}
	}
	var out []*ostypes.Sandbox
	for _, s := range f.byName {
		match := true
		for k, v := range want {
			match = match && s.Labels[k] == v
		}
		if match {
			out = append(out, s)
		}
	}
	return out, nil
}

func (f *fakeGateway) Delete(ctx context.Context, _ string, name string, _ ...ostypes.DeleteOptions) (*ostypes.DeletionResult, error) {
	f.mu.Lock()
	delete(f.byName, name) // the record goes at once (finding 7)
	f.deleted = append(f.deleted, name)
	slow := f.slowDel
	f.mu.Unlock()
	if slow {
		<-ctx.Done()
		return nil, &ostypes.StatusError{Code: ostypes.ErrorDeadlineExceeded, Message: "deadline", Cause: ctx.Err()}
	}
	return &ostypes.DeletionResult{}, nil
}

func (f *fakeGateway) setPhase(name string, ph ostypes.SandboxPhase) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.byName[name].Status.Phase = ph
}

const bootstrapWithFacade = `{"bootstrapVersion":"workspace-v3","facadeEndpoint":"http://ach-facade.ach.svc.cluster.local:9090"}`

type osFixture struct {
	gw    *fakeGateway
	b     host.Backend
	a     *achv1alpha1.Agent
	p     *achv1alpha1.AgentProfile
	w     host.Workspace
	c     client.Client
	sName string
}

func newOSFixture(t *testing.T, pods ...client.Object) *osFixture {
	t.Helper()
	a, p, w := unitFixture()
	a.Finalizers = []string{SandboxFinalizer} // the controller adds it before any sandbox may exist
	w.Launch.Env["ACH_BOOTSTRAP_JSON"] = bootstrapWithFacade
	gw := newFakeGateway()
	c := fake.NewClientBuilder().WithScheme(testScheme(t)).WithObjects(pods...).Build()
	o := &OpenShell{Sandboxes: gw, Config: OpenShellConfig{
		Workspace: "default", PodNamespace: "openshell",
		ServiceURL:  "http://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080",
		GatewayHost: "openshell.openshell.svc.cluster.local",
	}}
	return &osFixture{gw: gw, b: o.ForAgent(types.NamespacedName{Namespace: "ach", Name: a.Name}, a.UID, c, c), a: a, p: p, w: w, c: c,
		sName: SandboxName(string(a.UID), w.Name)}
}

func workloadPod(sandbox string, phase corev1.PodPhase, terminating bool) *corev1.Pod {
	pod := &corev1.Pod{ObjectMeta: metav1.ObjectMeta{Name: "default--" + sandbox, Namespace: "openshell"}, Status: corev1.PodStatus{Phase: phase}}
	if terminating {
		now := metav1.Now()
		pod.DeletionTimestamp = &now
		pod.Finalizers = []string{"test/keep"}
	}
	return pod
}

func TestSandboxName_StableShortAndOpaque(t *testing.T) {
	a := SandboxName("uid-1", "agent-x-0123456789abcdef0123")
	if a != SandboxName("uid-1", "agent-x-0123456789abcdef0123") || len(a) > 19 || !strings.HasPrefix(a, "ws-") {
		t.Fatalf("name %q: want stable, <= 19 chars, ws- prefix (finding 1)", a)
	}
	if a == SandboxName("uid-2", "agent-x-0123456789abcdef0123") {
		t.Fatal("same Workspace name under another agent UID must map to another sandbox (finding 1)")
	}
}

func TestOpenShell_CreateMapsLaunchExactly(t *testing.T) {
	f := newOSFixture(t)
	if err := f.b.Create(context.Background(), f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	s := f.gw.byName[f.sName]
	if s == nil {
		t.Fatalf("no sandbox %s created; created = %v", f.sName, f.gw.created)
	}
	if !maps.Equal(s.Spec.Environment, f.w.Launch.Env) {
		t.Errorf("environment differs from launch.env (global constraint: straight from launch)")
	}
	if strings.Join(s.Spec.Command, " ") != strings.Join(f.w.Launch.Command, " ") {
		t.Errorf("command = %v, want %v", s.Spec.Command, f.w.Launch.Command)
	}
	if s.Spec.Template == nil || s.Spec.Template.Image != f.p.Spec.Sandbox.OpenShell.Image {
		t.Errorf("template = %+v, want the profile image (finding 9)", s.Spec.Template)
	}
	if s.Spec.RestartPolicy != ostypes.SandboxRestartNever {
		t.Errorf("restart policy = %q, want Never (finding 8)", s.Spec.RestartPolicy)
	}
	want := map[string]string{AgentUIDLabel: string(f.a.UID), NameLabel: f.w.Name, portLabel: "8080"}
	if !maps.Equal(s.Labels, want) {
		t.Errorf("labels = %v, want %v (finding 2)", s.Labels, want)
	}
	if len(f.gw.exposures) != 1 || f.gw.exposures[0].TargetPort != 8080 || f.gw.exposures[0].AuthorizationMode != ostypes.ServiceAuthorizationModeBearerPassthrough {
		t.Errorf("exposures = %+v, want one {8080, BearerPassthrough} (finding 11)", f.gw.exposures)
	}
	rule, ok := s.Spec.Policy.NetworkPolicies["ach-agent-facades"]
	if !ok || len(rule.Endpoints) != 1 || rule.Endpoints[0].Host != "ach-facade.ach.svc.cluster.local" || rule.Endpoints[0].Port != 9090 {
		t.Errorf("facades rule = %+v, want the facadeEndpoint host:port (finding 10)", rule)
	}
	if len(rule.Binaries) != len(runtimeBinaries) {
		t.Errorf("binaries = %+v, want %v (finding 10)", rule.Binaries, runtimeBinaries)
	}
	if fs := s.Spec.Policy.Filesystem; fs == nil || !fs.IncludeWorkdir || !slices.Equal(fs.ReadOnly, runtimeReadOnly) || !slices.Equal(fs.ReadWrite, runtimeReadWrite) {
		t.Errorf("filesystem policy = %+v (finding 10)", fs)
	}
	if l := s.Spec.Policy.Landlock; l == nil || l.Compatibility != "hard_requirement" {
		t.Errorf("landlock = %+v, want hard_requirement (finding 10)", l)
	}
	if s.Spec.Template.Resources != nil {
		t.Errorf("resources = %v, want none when the profile sets none", s.Spec.Template.Resources)
	}

	// a repeated pass is a no-op on AlreadyExists is the gateway's job; a wrong release is refused here
	g := newOSFixture(t)
	g.p.Spec.Sandbox.OpenShell.Image = "ghcr.io/ackstorm/ach-runtime-execution:0.1.21"
	if err := g.b.Create(context.Background(), g.a, g.p, g.w); err == nil || len(g.gw.created) != 0 {
		t.Errorf("err = %v created = %v, want a refusal without Create (finding 9)", err, g.gw.created)
	}
}

func TestOpenShell_StateFollowsGatewayPhase(t *testing.T) {
	ctx := context.Background()
	f := newOSFixture(t)
	state := func() host.UnitState {
		t.Helper()
		st, err := f.b.State(ctx, f.w.Name)
		if err != nil {
			t.Fatal(err)
		}
		return st
	}
	if st := state(); st != host.UnitAbsent {
		t.Fatalf("before Create state = %v, want host.UnitAbsent", st)
	}
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	if st := state(); st != host.UnitStarting {
		t.Fatalf("Provisioning state = %v, want host.UnitStarting (finding 5)", st)
	}
	f.gw.setPhase(f.sName, ostypes.SandboxDeleting)
	if st := state(); st != host.UnitTerminating {
		t.Fatalf("Deleting state = %v, want host.UnitTerminating", st)
	}
	g := newOSFixture(t, workloadPod(SandboxName(string(f.a.UID), f.w.Name), corev1.PodRunning, false))
	if err := g.b.Create(ctx, g.a, g.p, g.w); err != nil {
		t.Fatal(err)
	}
	g.gw.setPhase(g.sName, ostypes.SandboxReady)
	if st, err := g.b.State(ctx, g.w.Name); err != nil || st != host.UnitStarted {
		t.Fatalf("Ready + Running pod state = %v err = %v, want host.UnitStarted (finding 5)", st, err)
	}
}

func TestOpenShell_StateIsLostWhenAReadySandboxHasNoRunningPod(t *testing.T) {
	ctx := context.Background()
	name := SandboxName(string(testUID), "agent-gitlab-reviewer-1a2b3c4d5e6f7a8b9c0d")
	for _, tc := range []struct {
		name  string
		pod   *corev1.Pod
		phase ostypes.SandboxPhase
		want  host.UnitState
	}{
		{"no pod", nil, ostypes.SandboxReady, host.UnitLost},
		{"running pod", workloadPod(name, corev1.PodRunning, false), ostypes.SandboxReady, host.UnitStarted},
		{"terminating pod", workloadPod(name, corev1.PodRunning, true), ostypes.SandboxReady, host.UnitLost},
		{"failed pod", workloadPod(name, corev1.PodFailed, false), ostypes.SandboxReady, host.UnitLost},
		{"pending pod", workloadPod(name, corev1.PodPending, false), ostypes.SandboxReady, host.UnitLost},
		{"provisioning without pod", nil, ostypes.SandboxProvisioning, host.UnitStarting},
		{"starting", nil, ostypes.SandboxStarting, host.UnitStarting},
		{"unknown", nil, ostypes.SandboxUnknown, host.UnitStarting},
		{"error is final with restart Never", nil, ostypes.SandboxError, host.UnitLost},
		{"completed is final", nil, ostypes.SandboxCompleted, host.UnitLost},
		{"stopped is final", nil, ostypes.SandboxStopped, host.UnitLost},
		{"stopping", nil, ostypes.SandboxStopping, host.UnitTerminating},
	} {
		t.Run(tc.name, func(t *testing.T) {
			var objs []client.Object
			if tc.pod != nil {
				objs = append(objs, tc.pod)
			}
			f := newOSFixture(t, objs...)
			if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
				t.Fatal(err)
			}
			f.gw.setPhase(f.sName, tc.phase)
			if st, err := f.b.State(ctx, f.w.Name); err != nil || st != tc.want {
				t.Fatalf("state = %v err = %v, want %v (finding 12: Ready without a running workload pod is lost)", st, err, tc.want)
			}
		})
	}
}

func TestOpenShell_RemoveNeverConfirmsWhileTheWorkloadPodRemains(t *testing.T) {
	ctx := context.Background()
	_, _, w := unitFixture()
	pod := workloadPod(SandboxName(string(testUID), w.Name), corev1.PodRunning, false)
	f := newOSFixture(t, pod) // the gateway holds no record: it forgot the sandbox at once
	remaining, err := f.b.Remove(ctx, w.Name)
	if err != nil || !remaining {
		t.Fatalf("remaining = %v err = %v, want true while the workload pod exists (finding 7)", remaining, err)
	}
	if err := f.c.Delete(ctx, pod); err != nil {
		t.Fatal(err)
	}
	if remaining, err = f.b.Remove(ctx, w.Name); err != nil || remaining {
		t.Fatalf("remaining = %v err = %v, want false once gateway and pod are gone (finding 7)", remaining, err)
	}
}

func TestOpenShell_RemoveDoesNotBlockOnASlowDelete(t *testing.T) {
	ctx := context.Background()
	f := newOSFixture(t)
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	f.gw.slowDel = true
	start := time.Now()
	remaining, err := f.b.Remove(ctx, f.w.Name)
	if err != nil || !remaining {
		t.Fatalf("remaining = %v err = %v, want true and no error on DeadlineExceeded (finding 6)", remaining, err)
	}
	if d := time.Since(start); d > 6*time.Second {
		t.Fatalf("Remove took %v, want <= 6s (5s deadline, finding 6)", d)
	}
}

func TestOpenShell_LiveReportsReadySandboxesWithTheirRefAndURL(t *testing.T) {
	ctx := context.Background()
	f := newOSFixture(t)
	for _, n := range []string{"w-a", "w-b", "w-c"} {
		w := f.w
		w.Name = n
		w.Launch.Env = maps.Clone(f.w.Launch.Env)
		w.Launch.Env["ACH_WORKSPACE_REF"] = strings.Repeat(n[2:], 64)
		if err := f.b.Create(ctx, f.a, f.p, w); err != nil {
			t.Fatal(err)
		}
	}
	f.gw.setPhase(SandboxName(string(f.a.UID), "w-a"), ostypes.SandboxReady)
	f.gw.setPhase(SandboxName(string(f.a.UID), "w-b"), ostypes.SandboxReady)
	// a Ready sandbox of another agent UID
	other := newOSFixture(t)
	other.gw = f.gw
	other.b = (&OpenShell{Sandboxes: f.gw, Config: f.b.(*openShellUnits).Config}).ForAgent(types.NamespacedName{Namespace: "ach", Name: "other"}, "uid-other", f.c, f.c)
	if err := other.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	f.gw.setPhase(SandboxName("uid-other", f.w.Name), ostypes.SandboxReady)

	got, err := f.b.Live(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 2 {
		t.Fatalf("Live = %+v, want the two Ready sandboxes of this agent only (finding 5)", got)
	}
	for _, u := range got {
		sb := SandboxName(string(f.a.UID), u.Name)
		wantURL := "http://default--" + sb + ".ach.svc.cluster.local:8080"
		if u.BaseURL != wantURL || u.WorkspaceRef != strings.Repeat(u.Name[2:], 64) {
			t.Errorf("unit %+v, want baseUrl %s and the ref from ACH_WORKSPACE_REF (findings 3, 4)", u, wantURL)
		}
	}
}

func TestOpenShell_RemoveAgentSandboxesSelectsByUIDOnly(t *testing.T) {
	ctx := context.Background()
	gw := newFakeGateway()
	o := &OpenShell{Sandboxes: gw, Config: OpenShellConfig{Workspace: "default", PodNamespace: "openshell", ServiceURL: "x"}}
	for _, uid := range []string{"uid-1", "uid-2"} {
		if _, err := gw.Create(ctx, "default", SandboxName(uid, "w1"), &ostypes.SandboxSpec{},
			ostypes.WithLabels(map[string]string{AgentUIDLabel: uid, NameLabel: "w1"})); err != nil {
			t.Fatal(err)
		}
	}
	remaining, err := o.RemoveAgentSandboxes(ctx, "uid-1")
	if err != nil || !remaining {
		t.Fatalf("first call remaining = %v err = %v, want true (a sandbox was listed)", remaining, err)
	}
	if len(gw.deleted) != 1 || gw.deleted[0] != SandboxName("uid-1", "w1") {
		t.Fatalf("deleted = %v, want only uid-1's sandbox (finding 2: select by UID, never by name)", gw.deleted)
	}
	if remaining, err = o.RemoveAgentSandboxes(ctx, "uid-1"); err != nil || remaining {
		t.Fatalf("second call remaining = %v err = %v, want false", remaining, err)
	}
}

func TestOpenShell_StateOnNotFoundChecksTheWorkloadPod(t *testing.T) {
	ctx := context.Background()
	_, _, w := unitFixture()
	pod := workloadPod(SandboxName(string(testUID), w.Name), corev1.PodRunning, true)
	f := newOSFixture(t, pod) // the gateway forgot the sandbox, the pod is still terminating
	if st, err := f.b.State(ctx, w.Name); err != nil || st != host.UnitTerminating {
		t.Fatalf("state = %v err = %v, want host.UnitTerminating while the pod remains (a new sandbox would collide)", st, err)
	}
	pod.Finalizers = nil
	if err := f.c.Update(ctx, pod); err != nil { // the fake client drops a terminating object once its finalizers go
		t.Fatal(err)
	}
	if st, err := f.b.State(ctx, w.Name); err != nil || st != host.UnitAbsent {
		t.Fatalf("state = %v err = %v, want host.UnitAbsent once the pod is gone", st, err)
	}
}

func TestOpenShell_CreateRefusesWithoutTheSandboxFinalizer(t *testing.T) {
	f := newOSFixture(t)
	f.a.Finalizers = nil
	if err := f.b.Create(context.Background(), f.a, f.p, f.w); err == nil || !strings.Contains(err.Error(), "finalizer") || len(f.gw.created) != 0 {
		t.Fatalf("err = %v created = %v, want a refusal without Create", err, f.gw.created)
	}
}

func TestOpenShell_FacadePortDefaultsByScheme(t *testing.T) {
	for url, want := range map[string]uint32{"http://ach-facade.ach:9090": 9090, "http://ach-facade.ach": 80, "https://ach-facade.ach": 443} {
		f := newOSFixture(t)
		f.w.Launch.Env["ACH_BOOTSTRAP_JSON"] = `{"facadeEndpoint":"` + url + `"}`
		if err := f.b.Create(context.Background(), f.a, f.p, f.w); err != nil {
			t.Fatal(err)
		}
		got := f.gw.byName[f.sName].Spec.Policy.NetworkPolicies["ach-agent-facades"].Endpoints[0].Port
		if got != want {
			t.Errorf("%s: port = %d, want %d", url, got, want)
		}
	}
}

func TestOpenShell_GatewayCallsAreBounded(t *testing.T) {
	old := callDeadline
	callDeadline = 50 * time.Millisecond
	t.Cleanup(func() { callDeadline = old })
	ctx := context.Background()
	f := newOSFixture(t)
	f.gw.hang = true
	o := f.b.(*openShellUnits)
	for name, fn := range map[string]func() error{
		"State":     func() error { _, err := f.b.State(ctx, f.w.Name); return err },
		"Create":    func() error { return f.b.Create(ctx, f.a, f.p, f.w) },
		"Owned":     func() error { _, err := f.b.Owned(ctx); return err },
		"Remove":    func() error { _, err := f.b.Remove(ctx, f.w.Name); return err },
		"Finalizer": func() error { _, err := o.RemoveAgentSandboxes(ctx, string(f.a.UID)); return err },
	} {
		start := time.Now()
		if err := fn(); err == nil || time.Since(start) > 2*time.Second {
			t.Errorf("%s: err = %v after %v, want an error within the call deadline", name, err, time.Since(start))
		}
	}
}

func TestOpenShell_CreateMakesExternalNameService(t *testing.T) {
	f := newOSFixture(t)
	ctx := context.Background()
	for range 2 { // a repeated pass is fine
		if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
			t.Fatal(err)
		}
	}
	var svc corev1.Service
	if err := f.c.Get(ctx, client.ObjectKey{Namespace: "ach", Name: "default--" + f.sName}, &svc); err != nil {
		t.Fatal(err)
	}
	if svc.Spec.Type != corev1.ServiceTypeExternalName || svc.Spec.ExternalName != "openshell.openshell.svc.cluster.local" {
		t.Fatalf("got %s %q", svc.Spec.Type, svc.Spec.ExternalName)
	}
	if len(svc.OwnerReferences) != 1 || svc.OwnerReferences[0].UID != f.a.UID || svc.Labels[AgentUIDLabel] != string(f.a.UID) {
		t.Fatalf("owner %v labels %v", svc.OwnerReferences, svc.Labels)
	}
}

func TestOpenShell_URLUsesAgentNamespace(t *testing.T) {
	f := newOSFixture(t)
	want := "http://default--" + f.sName + ".ach.svc.cluster.local:8080"
	if got := f.b.BaseURL(f.w); got != want {
		t.Fatalf("got %s want %s", got, want)
	}
}

func TestOpenShell_RemoveDeletesServiceOnceGone(t *testing.T) {
	f := newOSFixture(t)
	ctx := context.Background()
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	key := client.ObjectKey{Namespace: "ach", Name: "default--" + f.sName}
	// the gateway record was still there: the Service stays (the fake drops the record on Delete)
	if remaining, err := f.b.Remove(ctx, f.w.Name); err != nil || !remaining {
		t.Fatalf("remaining=%v err=%v", remaining, err)
	}
	if remaining, err := f.b.Remove(ctx, f.w.Name); err != nil || remaining {
		t.Fatalf("remaining=%v err=%v", remaining, err)
	}
	if err := f.c.Get(ctx, key, &corev1.Service{}); !apierrors.IsNotFound(err) {
		t.Fatalf("service still there: %v", err)
	}
}

func TestGatewayHost(t *testing.T) {
	for addr, want := range map[string]string{
		"http://h:8080": "h", "https://h:443": "h", "h:443": "h", "10.0.0.1:8080": "10.0.0.1",
		"openshell.openshell.svc.cluster.local:443": "openshell.openshell.svc.cluster.local",
	} {
		if got, err := gatewayHost(addr); err != nil || got != want {
			t.Errorf("%s: got %q err %v, want %q", addr, got, err, want)
		}
	}
	if _, err := gatewayHost(""); err == nil {
		t.Error("empty address: want an error")
	}
}

func TestOpenShell_RemoveKeepsServiceWhileTheWorkloadPodTerminates(t *testing.T) {
	_, _, w := unitFixture()
	f := newOSFixture(t, workloadPod(SandboxName(string(testUID), w.Name), corev1.PodRunning, true))
	ctx := context.Background()
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	delete(f.gw.byName, f.sName) // the gateway forgot the record, the pod lingers
	if remaining, err := f.b.Remove(ctx, f.w.Name); err != nil || !remaining {
		t.Fatalf("remaining=%v err=%v, want true", remaining, err)
	}
	if err := f.c.Get(ctx, client.ObjectKey{Namespace: "ach", Name: "default--" + f.sName}, &corev1.Service{}); err != nil {
		t.Fatalf("service must stay: %v", err)
	}
}

func reasonOf(t *testing.T, err error) string {
	t.Helper()
	var r interface{ StatusReason() string }
	if !errors.As(err, &r) {
		t.Fatalf("err = %v carries no status reason", err)
	}
	return r.StatusReason()
}

func TestOpenShell_CreateMergesProfileAndAgentPolicyAndPassesResources(t *testing.T) {
	f := newOSFixture(t)
	f.p.Spec.Sandbox.OpenShell.Policy = raw(`{"network_policies":{"pypi":{"name":"pypi","endpoints":[{"host":"pypi.org","port":443}]}}}`)
	f.p.Spec.Sandbox.OpenShell.Resources = &achv1alpha1.SandboxResources{CPU: "2", Memory: "4Gi"}
	f.a.Spec.Sandbox = &achv1alpha1.AgentSandboxSpec{OpenShell: &achv1alpha1.AgentOpenShellSpec{
		Policy: raw(`{"network_policies":{"serper":{"name":"serper","endpoints":[{"host":"google.serper.dev","port":443}]}}}`),
	}}
	if err := f.b.Create(context.Background(), f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	s := f.gw.byName[f.sName]
	for _, name := range []string{"pypi", "serper", "ach-agent-facades"} {
		if _, ok := s.Spec.Policy.NetworkPolicies[name]; !ok {
			t.Errorf("rule %q missing from %v", name, s.Spec.Policy.NetworkPolicies)
		}
	}
	if got := s.Spec.Template.Resources; got["cpu"] != "2" || got["memory"] != "4Gi" {
		t.Errorf("resources = %v", got)
	}
}

func TestOpenShell_CreateRefusesARejectedPolicyBeforeTouchingTheGateway(t *testing.T) {
	f := newOSFixture(t)
	f.a.Spec.Sandbox = &achv1alpha1.AgentSandboxSpec{OpenShell: &achv1alpha1.AgentOpenShellSpec{
		Policy: raw(`{"filesystem":{"read_write":["/usr/local/bin"]}}`),
	}}
	err := f.b.Create(context.Background(), f.a, f.p, f.w)
	if !errors.Is(err, ErrPolicyRejected) || reasonOf(t, err) != "SandboxPolicyRejected" {
		t.Fatalf("err = %v", err)
	}
	if len(f.gw.created) != 0 {
		t.Fatalf("created = %v, want none", f.gw.created)
	}
	var svc corev1.Service
	if getErr := f.c.Get(context.Background(), client.ObjectKey{Namespace: "ach", Name: "default--" + f.sName}, &svc); !apierrors.IsNotFound(getErr) {
		t.Fatalf("a per-sandbox Service exists for a refused sandbox: %v", getErr)
	}
}

func TestOpenShell_CreateMapsGatewayInvalidArgument(t *testing.T) {
	for name, tc := range map[string]struct {
		message   string
		resources *achv1alpha1.SandboxResources
		want      error
		reason    string
	}{
		"policy":    {"policy: network rule invalid", nil, ErrPolicyRejected, "SandboxPolicyRejected"},
		"resources": {"template.resources: unknown key", &achv1alpha1.SandboxResources{CPU: "2"}, ErrResourcesUnsupported, "SandboxResourcesUnsupported"},
	} {
		t.Run(name, func(t *testing.T) {
			f := newOSFixture(t)
			f.p.Spec.Sandbox.OpenShell.Resources = tc.resources
			f.gw.createErr = &ostypes.StatusError{Code: ostypes.ErrorInvalidArgument, Message: tc.message}
			err := f.b.Create(context.Background(), f.a, f.p, f.w)
			if !errors.Is(err, tc.want) || reasonOf(t, err) != tc.reason || !strings.Contains(err.Error(), tc.message) {
				t.Fatalf("err = %v, want %v / %s carrying the gateway message", err, tc.want, tc.reason)
			}
		})
	}
}
