// SPDX-License-Identifier: Apache-2.0

package openshell

import (
	"context"
	"errors"
	"testing"

	ostypes "github.com/ai-openshell/sdk-go/openshell/v1/types"
	"k8s.io/apimachinery/pkg/api/resource"
	"k8s.io/apimachinery/pkg/types"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

func TestEnsureHermes_CreatesWithPVCAndProxyRuleThenReplacesOnHashChange(t *testing.T) {
	f := newOSFixture(t)
	ctx := context.Background()
	h := HermesSandbox{
		Owner: types.NamespacedName{Namespace: "ach", Name: "hermes"}, UID: "uid-h",
		OpenShell: achv1alpha1.OpenShellSpec{Image: "hermes:1"},
		Volume:    achv1alpha1.WorkspaceVolumeSpec{Size: resource.MustParse("1Gi")},
		Env:       map[string]string{"HERMES_CONFIG": "{}"},
		ProxyHost: "hermes-hermes.ach.svc.cluster.local", ProxyPort: 8081, Hash: "h1",
	}
	if _, _, err := f.o.EnsureHermes(ctx, f.c, f.c, h); !errors.Is(err, errVolumeNeedsTLS) {
		t.Fatalf("without gateway TLS: err = %v, want errVolumeNeedsTLS", err)
	}
	f.o.Config.CA = []byte("test CA")

	if ready, _, err := f.o.EnsureHermes(ctx, f.c, f.c, h); err != nil || ready {
		t.Fatalf("first pass: ready=%v err=%v", ready, err)
	}
	name := SandboxName("hermes", "uid-h", hermesUnit)
	s := f.gw.byName[name]
	if s == nil || s.Spec.RestartPolicy != ostypes.SandboxRestartAlways || s.Labels[specHashLabel] != "h1" || s.Labels[AgentUIDLabel] != "uid-h" {
		t.Fatalf("sandbox = %+v", s)
	}
	rule := s.Spec.Policy.NetworkPolicies[reservedPrefix+"facades"]
	if len(rule.Endpoints) != 1 || rule.Endpoints[0].Host != h.ProxyHost || rule.Endpoints[0].Port != 8081 ||
		len(rule.Binaries) != 1 || rule.Binaries[0].Path != hermesBinaries {
		t.Fatalf("reserved rule = %+v", rule)
	}
	if s.Spec.Template.DriverConfig == nil {
		t.Fatal("no PVC mount in driver_config")
	}
	getPVC(t, f, VolumeName("uid-h", hermesUnit))

	f.gw.setPhase(name, ostypes.SandboxReady)
	if ready, _, err := f.o.EnsureHermes(ctx, f.c, f.c, h); err != nil || !ready {
		t.Fatalf("ready pass: ready=%v err=%v", ready, err)
	}
	h.Hash = "h2"
	if ready, _, err := f.o.EnsureHermes(ctx, f.c, f.c, h); err != nil || ready || len(f.gw.deleted) != 1 {
		t.Fatalf("hash change: ready=%v err=%v deleted=%v", ready, err, f.gw.deleted)
	}
}
