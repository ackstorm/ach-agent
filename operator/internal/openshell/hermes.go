// SPDX-License-Identifier: Apache-2.0

package openshell

import (
	"context"
	"fmt"

	osv1 "github.com/ai-openshell/sdk-go/openshell/v1"
	ostypes "github.com/ai-openshell/sdk-go/openshell/v1/types"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

const (
	// HermesHome is Hermes' state directory on the PVC (mounted at /sandbox/ach).
	HermesHome = volumeMountPath + "/hermes"
	// hermesBinaries may reach the credential proxy: Hermes' interpreter lives under /opt/hermes,
	// which the policy keeps read-only.
	hermesBinaries = "/opt/hermes/**"
	hermesUnit     = "hermes"
	specHashLabel  = "runtime.ach-agent.ackstorm.ai/spec-hash"
	// hermesCommand bypasses the image's /init, which exits for any uid but root and 10000.
	// The gateway refuses newlines in env values: HERMES_CONFIG is single-line JSON (valid YAML).
	hermesCommand = `mkdir -p "$HERMES_HOME" "$HOME" && printf '%s' "$HERMES_CONFIG" > "$HERMES_HOME/config.yaml" && exec /opt/hermes/.venv/bin/hermes gateway run`
)

// HermesSandbox is one HermesAgent's long-running sandbox.
type HermesSandbox struct {
	Owner     types.NamespacedName
	UID       types.UID
	OpenShell achv1alpha1.OpenShellSpec
	Volume    achv1alpha1.WorkspaceVolumeSpec
	Env       map[string]string
	ProxyHost string
	ProxyPort uint32
	// Hash labels the sandbox; a sandbox with another hash is replaced.
	Hash string
}

// EnsureHermes creates the HermesAgent's PVC and sandbox, replaces a sandbox whose spec hash
// differs, and reports whether it is Ready (with the gateway's phase otherwise). The gateway
// restarts the workload itself (RestartPolicy Always).
func (o *OpenShell) EnsureHermes(ctx context.Context, reader client.Reader, writer client.Client, h HermesSandbox) (bool, string, error) {
	if len(o.Config.CA) == 0 {
		return false, "", errVolumeNeedsTLS
	}
	name := SandboxName(h.Owner.Name, string(h.UID), hermesUnit)
	cctx, cancel := call(ctx)
	s, err := o.gw().Get(cctx, o.Config.Workspace, name)
	cancel()
	switch {
	case err == nil && s.Labels[specHashLabel] != h.Hash:
		_, err := o.removeSandbox(ctx, reader, name)
		return false, "replacing the sandbox for a spec change", err
	case err == nil:
		return s.Status.Phase == ostypes.SandboxReady, string(s.Status.Phase), nil
	case !osv1.IsNotFound(err):
		return false, "", err
	}
	// The record is gone before its pod: wait for the pod so two never share the PVC.
	if pod, err := o.workloadPod(ctx, reader, name); err != nil || pod != nil {
		return false, "previous sandbox terminating", err
	}
	units := &openShellUnits{OpenShell: o, agent: h.Owner, uid: h.UID, reader: reader, writer: writer}
	claim, err := units.ensureVolume(ctx, h.Owner.Name, &h.Volume, hermesUnit)
	if err != nil {
		return false, "", err
	}
	policy, err := buildPolicy(h.OpenShell.Policy, nil, h.ProxyHost, h.ProxyPort, []string{hermesBinaries})
	if err != nil {
		return false, "", err
	}
	resources, err := Resources(h.OpenShell.Resources)
	if err != nil {
		return false, "", err
	}
	spec := &ostypes.SandboxSpec{
		Environment:   h.Env,
		Command:       []string{"sh", "-c", hermesCommand},
		Template:      &ostypes.SandboxTemplate{Image: h.OpenShell.Image, Resources: resources, DriverConfig: driverConfig(claim)},
		RestartPolicy: ostypes.SandboxRestartAlways,
		Policy:        policy,
	}
	cctx, cancel = call(ctx)
	defer cancel()
	_, err = o.gw().Create(cctx, o.Config.Workspace, name, spec, ostypes.WithLabels(map[string]string{
		AgentUIDLabel: string(h.UID), NameLabel: hermesUnit, specHashLabel: h.Hash,
	}))
	if err != nil && !osv1.IsAlreadyExists(err) {
		return false, "", fmt.Errorf("create Hermes sandbox: %w", err)
	}
	return false, "sandbox created", nil
}
