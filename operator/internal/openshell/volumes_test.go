// SPDX-License-Identifier: MIT

package openshell

import (
	"context"
	"maps"
	"reflect"
	"sort"
	"testing"
	"time"

	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func days(n int64) *int64 { return &n }

// volumeFixture is an osFixture whose profile resolves to persistence type volume and whose
// gateway connection has mTLS.
func volumeFixture(t *testing.T, v achv1alpha1.WorkspaceVolumeSpec) *osFixture {
	t.Helper()
	f := newOSFixture(t)
	f.o.Config.CA = []byte("test CA")
	f.p.Spec.Defaults.Workspace = &achv1alpha1.WorkspaceSpec{Persistence: &achv1alpha1.WorkspacePersistenceSpec{
		Type: "volume", RetentionDays: days(7), Volume: &v}}
	return f
}

func getPVC(t *testing.T, f *osFixture, name string) *corev1.PersistentVolumeClaim {
	t.Helper()
	var pvc corev1.PersistentVolumeClaim
	if err := f.c.Get(context.Background(), client.ObjectKey{Namespace: "openshell", Name: name}, &pvc); err != nil {
		t.Fatalf("get PVC %s: %v", name, err)
	}
	return &pvc
}

func TestVolumeName_StableDNSSafeAndPerAgentUID(t *testing.T) {
	a := VolumeName("uid-1", "agent-x-0123")
	if a != VolumeName("uid-1", "agent-x-0123") || len(a) != 23 || a[:7] != "ach-ws-" {
		t.Fatalf("VolumeName = %q: want stable, ach-ws- + 16 hex", a)
	}
	if a == VolumeName("uid-2", "agent-x-0123") {
		t.Fatal("the same Workspace name under another Agent UID must get another PVC")
	}
}

// The driver_config invariant (operator/CLAUDE.md): exactly the Workspace PVC at /sandbox/ach,
// read-write, nothing else.
func TestOpenShell_CreateWithVolumeMountsOnlyTheWorkspacePVC(t *testing.T) {
	for _, tc := range []struct {
		name  string
		v     achv1alpha1.WorkspaceVolumeSpec
		mode  corev1.PersistentVolumeAccessMode
		class *string
	}{
		{"defaults", achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("10Gi")}, corev1.ReadWriteOnce, nil},
		{"rwx class", achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("10Gi"), StorageClassName: "efs-ach", AccessMode: "ReadWriteMany"},
			corev1.ReadWriteMany, ptrTo("efs-ach")},
	} {
		t.Run(tc.name, func(t *testing.T) {
			f := volumeFixture(t, tc.v)
			if err := f.b.Create(context.Background(), f.a, f.p, f.w); err != nil {
				t.Fatal(err)
			}
			claim := VolumeName(string(f.a.UID), f.w.Name)
			want := map[string]any{"kubernetes": map[string]any{
				"volumes": []any{map[string]any{"name": "ach-data",
					"persistent_volume_claim": map[string]any{"claim_name": claim, "read_only": false}}},
				"containers": map[string]any{"agent": map[string]any{"volume_mounts": []any{
					map[string]any{"name": "ach-data", "mount_path": "/sandbox/ach", "read_only": false}}}},
			}}
			if got := f.gw.byName[f.sName].Spec.Template.DriverConfig; !reflect.DeepEqual(got, want) {
				t.Fatalf("driver_config = %#v\nwant exactly %#v", got, want)
			}
			pvc := getPVC(t, f, claim)
			wantLabels := map[string]string{
				"openshell.ai/sandbox-attachable": "true", "openshell.ai/sandbox-attachable-workspace": "default",
				AgentLabel: f.a.Name, AgentUIDLabel: string(f.a.UID),
			}
			if !maps.Equal(pvc.Labels, wantLabels) {
				t.Errorf("labels = %v, want %v", pvc.Labels, wantLabels)
			}
			if len(pvc.OwnerReferences) != 0 {
				t.Errorf("owner references = %v: the Agent is in another namespace, its finalizer deletes the PVC", pvc.OwnerReferences)
			}
			if got := pvc.Spec.AccessModes; len(got) != 1 || got[0] != tc.mode {
				t.Errorf("access modes = %v, want [%s]", got, tc.mode)
			}
			if got := pvc.Spec.Resources.Requests[corev1.ResourceStorage]; got.Cmp(resource.MustParse("10Gi")) != 0 {
				t.Errorf("request = %s, want 10Gi", got.String())
			}
			if !reflect.DeepEqual(pvc.Spec.StorageClassName, tc.class) {
				t.Errorf("storageClassName = %v, want %v", pvc.Spec.StorageClassName, tc.class)
			}
		})
	}
}

func ptrTo(s string) *string { return &s }

func TestOpenShell_CreateWithoutVolumeSendsNoDriverConfig(t *testing.T) {
	f := newOSFixture(t)
	f.o.Config.CA = []byte("test CA")
	if err := f.b.Create(context.Background(), f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	if dc := f.gw.byName[f.sName].Spec.Template.DriverConfig; dc != nil {
		t.Errorf("driver_config = %v, want none for persistence other than volume", dc)
	}
	var pvcs corev1.PersistentVolumeClaimList
	if err := f.c.List(context.Background(), &pvcs); err != nil || len(pvcs.Items) != 0 {
		t.Errorf("PVCs = %v, %v; want none", pvcs.Items, err)
	}
}

func TestOpenShell_CreateRefusesVolumeWithoutGatewayTLS(t *testing.T) {
	f := volumeFixture(t, achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("1Gi")})
	f.o.Config.CA = nil
	if err := f.b.Create(context.Background(), f.a, f.p, f.w); err == nil || len(f.gw.created) != 0 {
		t.Fatalf("err = %v created = %v; want a refusal before any sandbox", err, f.gw.created)
	}
	var pvcs corev1.PersistentVolumeClaimList
	if err := f.c.List(context.Background(), &pvcs); err != nil || len(pvcs.Items) != 0 {
		t.Errorf("PVCs = %v, %v; want none", pvcs.Items, err)
	}
}

func TestOpenShell_VolumeIsReusedAndMarkedDetachedWhileNoSandboxExists(t *testing.T) {
	ctx := context.Background()
	f := volumeFixture(t, achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("1Gi")})
	claim := VolumeName(string(f.a.UID), f.w.Name)
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	if remaining, err := f.b.Remove(ctx, f.w.Name); err != nil || !remaining {
		t.Fatalf("first Remove = %v, %v; want the delete requested", remaining, err)
	}
	if _, ok := getPVC(t, f, claim).Annotations[DetachedAtAnnotation]; ok {
		t.Fatal("detached-at set while the sandbox may still run")
	}
	if remaining, err := f.b.Remove(ctx, f.w.Name); err != nil || remaining {
		t.Fatalf("second Remove = %v, %v; want nothing left", remaining, err)
	}
	at, err := time.Parse(time.RFC3339, getPVC(t, f, claim).Annotations[DetachedAtAnnotation])
	if err != nil || time.Since(at) > time.Minute {
		t.Fatalf("detached-at = %v, %v; want now", at, err)
	}
	if err := f.b.Create(ctx, f.a, f.p, f.w); err != nil {
		t.Fatal(err)
	}
	if _, ok := getPVC(t, f, claim).Annotations[DetachedAtAnnotation]; ok {
		t.Error("detached-at kept while a sandbox mounts the PVC")
	}
	var pvcs corev1.PersistentVolumeClaimList
	if err := f.c.List(ctx, &pvcs); err != nil || len(pvcs.Items) != 1 {
		t.Errorf("PVCs = %d, %v; want the one PVC reused", len(pvcs.Items), err)
	}
}

func TestOpenShell_SweepDeletesVolumesDetachedPastRetention(t *testing.T) {
	ctx := context.Background()
	defer func(d time.Duration) { sweepEvery = d }(sweepEvery)
	sweepEvery = 0
	f := volumeFixture(t, achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("1Gi")})
	pvc := func(name, uid string, detached time.Duration) {
		obj := &corev1.PersistentVolumeClaim{ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: "openshell",
			Labels: map[string]string{AgentUIDLabel: uid}}}
		if detached > 0 {
			obj.Annotations = map[string]string{DetachedAtAnnotation: time.Now().Add(-detached).UTC().Format(time.RFC3339)}
		}
		if err := f.c.Create(ctx, obj); err != nil {
			t.Fatal(err)
		}
	}
	pvc("old", testUID, 8*24*time.Hour)
	pvc("recent", testUID, time.Hour)
	pvc("attached", testUID, 0)
	pvc("other-agent", "another-uid", 30*24*time.Hour)
	names := func() []string {
		var l corev1.PersistentVolumeClaimList
		if err := f.c.List(ctx, &l); err != nil {
			t.Fatal(err)
		}
		var out []string
		for _, p := range l.Items {
			out = append(out, p.Name)
		}
		sort.Strings(out)
		return out
	}
	if err := f.b.Sweep(ctx, f.a, f.p); err != nil {
		t.Fatal(err)
	}
	if got := names(); !reflect.DeepEqual(got, []string{"attached", "other-agent", "recent"}) {
		t.Fatalf("after a 7-day sweep: %v, want old deleted only", got)
	}
	// Away from type volume no sandbox mounts these PVCs again: a detached one goes.
	f.p.Spec.Defaults.Workspace.Persistence = &achv1alpha1.WorkspacePersistenceSpec{Type: "s3", RetentionDays: days(7)}
	if err := f.b.Sweep(ctx, f.a, f.p); err != nil {
		t.Fatal(err)
	}
	if got := names(); !reflect.DeepEqual(got, []string{"attached", "other-agent"}) {
		t.Fatalf("after a change to s3: %v, want recent deleted too", got)
	}
}
