// SPDX-License-Identifier: MIT

package openshell

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"net/url"
	"os"
	"slices"
	"strconv"
	"strings"
	"time"

	osv1 "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1"
	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/host"
)

const (
	// SandboxFinalizer is on every Agent: sandboxes have no owner
	// reference, so the Agent controller deletes them through the gateway before the agent goes.
	SandboxFinalizer = "ach-agent.ackstorm.ai/sandboxes"
	deleteDeadline   = 5 * time.Second // DeleteSandbox blocks until the pod is gone (PoC: > 30 s)
	refEnv           = "ACH_WORKSPACE_REF"
	portLabel        = "runtime.ach-agent.ackstorm.ai/port"
)

// callDeadline bounds Get/ListAll/Create: the SDK's Config.Timeout is never read, and a hung
// gateway must not stall the Host loop.
// A var so tests can shorten it.
var callDeadline = 10 * time.Second

func call(ctx context.Context) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, callDeadline)
}

// facadeBinaries call the control unit's facades from inside the sandbox. They are paths in
// the ach-runtime execution image: an image that moves them breaks facade egress (risk noted
// in the PoC handover).
var facadeBinaries = []string{
	"/usr/local/bin/python3.13", "/usr/local/bin/node",
	"/opt/opencode/node_modules/@opencode/cli/bin/opencode.exe",
}

// sandboxes is the part of the OpenShell Go SDK's v1.SandboxInterface the backend uses;
// (*v1.Client).Sandboxes() satisfies it, tests use an in-memory fake.
type sandboxes interface {
	Create(ctx context.Context, workspace, name string, spec *ostypes.SandboxSpec, opts ...ostypes.CreateOption) (*ostypes.Sandbox, error)
	Get(ctx context.Context, workspace, name string) (*ostypes.Sandbox, error)
	ListAll(ctx context.Context, workspace string, opts ...ostypes.ListOptions) ([]*ostypes.Sandbox, error)
	Delete(ctx context.Context, workspace, name string, opts ...ostypes.DeleteOptions) (*ostypes.DeletionResult, error)
}

type OpenShellConfig struct {
	Workspace    string // OpenShell workspace, default "default"
	ServiceURL   string // template with {workspace} {sandbox} {namespace} {port}
	PodNamespace string // where the driver creates sandbox pods
	GatewayHost  string // host of ACH_AGENT_OPENSHELL_GATEWAY: target of the per-sandbox ExternalName Service
}

// OpenShell is the gateway connection shared by every agent's backend and by the finalizer.
type OpenShell struct {
	Sandboxes sandboxes
	Config    OpenShellConfig
}

// gatewayHost is the host of the gateway address: "http(s)://host:port", or "host:port" (TLS, no scheme).
func gatewayHost(addr string) (string, error) {
	u, err := url.Parse(addr)
	if err != nil || u.Host == "" {
		u, err = url.Parse("//" + addr)
	}
	if err != nil || u.Hostname() == "" {
		return "", fmt.Errorf("ACH_AGENT_OPENSHELL_GATEWAY %q has no host", addr)
	}
	return u.Hostname(), nil
}

func envOr(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// OpenShellFromEnv dials the gateway named by ACH_AGENT_OPENSHELL_GATEWAY once. The gateway is
// reached over the mesh, without TLS of its own. An unset address is an error: the operator
// has no other sandbox backend.
func OpenShellFromEnv() (*OpenShell, error) {
	addr := envOr("ACH_AGENT_OPENSHELL_GATEWAY", "")
	if addr == "" {
		return nil, errors.New("ACH_AGENT_OPENSHELL_GATEWAY is required")
	}
	gw, err := gatewayHost(addr)
	if err != nil {
		return nil, err
	}
	c, err := osv1.NewClient(osv1.Config{Address: addr})
	if err != nil {
		return nil, fmt.Errorf("OpenShell gateway %s: %w", addr, err)
	}
	return &OpenShell{Sandboxes: c.Sandboxes(), Config: OpenShellConfig{
		Workspace:    envOr("ACH_AGENT_OPENSHELL_WORKSPACE", "default"),
		ServiceURL:   envOr("ACH_AGENT_OPENSHELL_SERVICE_URL", "http://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080"),
		PodNamespace: envOr("ACH_AGENT_OPENSHELL_POD_NAMESPACE", "openshell"),
		GatewayHost:  gw,
	}}, nil
}

// SandboxName is stable and at most 19 characters (the OpenShell limit); the Workspace name
// is never parsed back out of it.
func SandboxName(agentUID, workspaceName string) string {
	sum := sha256.Sum256([]byte(agentUID + "/" + workspaceName))
	return "ws-" + hex.EncodeToString(sum[:])[:16]
}

type openShellUnits struct {
	*OpenShell
	agent  types.NamespacedName
	uid    types.UID
	reader client.Reader
	writer client.Client
}

// ForAgent is the unit backend of one agent.
func (o *OpenShell) ForAgent(agent types.NamespacedName, uid types.UID, reader client.Reader, writer client.Client) host.Backend {
	return &openShellUnits{OpenShell: o, agent: agent, uid: uid, reader: reader, writer: writer}
}

func (o *OpenShell) url(namespace, sandbox string, port int32) string {
	return strings.NewReplacer("{workspace}", o.Config.Workspace, "{sandbox}", sandbox,
		"{namespace}", namespace, "{port}", strconv.Itoa(int(port))).Replace(o.Config.ServiceURL)
}

// serviceName is the ExternalName Service that makes a sandbox's URL resolvable with plain
// cluster DNS: <workspace>--<sandbox>.<ns>.svc.cluster.local -> the gateway Service. The gateway
// routes that Host because *.<ns>.svc.cluster.local is one of its server_sans.
func (o *OpenShell) serviceName(sandbox string) string { return o.Config.Workspace + "--" + sandbox }

func (b *openShellUnits) ensureService(ctx context.Context, a *achv1alpha1.Agent, sandbox string) error {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: b.serviceName(sandbox), Namespace: a.Namespace,
			Labels: map[string]string{AgentUIDLabel: string(a.UID)}},
		Spec: corev1.ServiceSpec{Type: corev1.ServiceTypeExternalName, ExternalName: b.Config.GatewayHost},
	}
	if err := controllerutil.SetOwnerReference(a, svc, b.writer.Scheme()); err != nil {
		return err
	}
	if err := b.writer.Create(ctx, svc); err != nil && !apierrors.IsAlreadyExists(err) {
		return err
	}
	return nil
}

func (b *openShellUnits) BaseURL(w host.Workspace) string {
	return b.url(b.agent.Namespace, SandboxName(string(b.uid), w.Name), w.Launch.Port)
}

func (b *openShellUnits) State(ctx context.Context, name string) (host.UnitState, error) {
	sandbox := SandboxName(string(b.uid), name)
	cctx, cancel := call(ctx)
	s, err := b.Sandboxes.Get(cctx, b.Config.Workspace, sandbox)
	cancel()
	switch {
	case osv1.IsNotFound(err):
		// The record is dropped at once; the workload pod outlives it by its grace period.
		pod, err := b.workloadPod(ctx, b.reader, sandbox)
		if err != nil {
			return 0, err
		}
		if pod != nil {
			return host.UnitTerminating, nil
		}
		return host.UnitAbsent, nil
	case err != nil:
		return 0, err
	case s.Status.Phase == ostypes.SandboxDeleting, s.Status.Phase == ostypes.SandboxStopping:
		return host.UnitTerminating, nil
	case s.Status.Phase == ostypes.SandboxError, s.Status.Phase == ostypes.SandboxCompleted, s.Status.Phase == ostypes.SandboxStopped:
		return host.UnitLost, nil // restart policy Never: it does not come back
	case s.Status.Phase == ostypes.SandboxReady:
		// Ready on the gateway but no running workload pod: the unit is lost .
		pod, err := b.workloadPod(ctx, b.reader, s.Name)
		if err != nil {
			return 0, err
		}
		if pod == nil || !pod.DeletionTimestamp.IsZero() || pod.Status.Phase != corev1.PodRunning {
			return host.UnitLost, nil
		}
		return host.UnitStarted, nil
	}
	return host.UnitStarting, nil
}

// workloadPod reads the sandbox's workload pod, uncached; nil when it is gone.
func (o *OpenShell) workloadPod(ctx context.Context, reader client.Reader, sandbox string) (*corev1.Pod, error) {
	var pod corev1.Pod
	err := reader.Get(ctx, client.ObjectKey{Namespace: o.Config.PodNamespace, Name: o.serviceName(sandbox)}, &pod)
	if apierrors.IsNotFound(err) {
		return nil, nil
	}
	return &pod, err
}

func (b *openShellUnits) Create(ctx context.Context, a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile, w host.Workspace) error {
	// Sandboxes have no owner reference: without the finalizer an agent deleted now would orphan them.
	if !controllerutil.ContainsFinalizer(a, SandboxFinalizer) {
		return fmt.Errorf("Agent %s has no sandbox finalizer yet; retrying", client.ObjectKeyFromObject(a))
	}
	image := p.Spec.Sandbox.OpenShell.Image
	if rel, want := imageRelease(image), strings.TrimPrefix(w.Launch.RuntimeRelease, "v"); rel != "" && want != "" && rel != want {
		return fmt.Errorf("execution image %q is release %s but the runtime requires %s (launch.runtimeRelease)", image, rel, want)
	}
	if len(w.Launch.Command) == 0 || w.Launch.Port < 1 {
		return fmt.Errorf("launch of %s needs a command and a port", w.Name)
	}
	var boot struct {
		FacadeEndpoint string `json:"facadeEndpoint"`
	}
	if err := json.Unmarshal([]byte(w.Launch.Env["ACH_BOOTSTRAP_JSON"]), &boot); err != nil {
		return fmt.Errorf("launch of %s: ACH_BOOTSTRAP_JSON: %w", w.Name, err)
	}
	facade, err := url.Parse(boot.FacadeEndpoint)
	if err != nil || facade.Hostname() == "" {
		return fmt.Errorf("launch of %s: facadeEndpoint %q is not a URL", w.Name, boot.FacadeEndpoint)
	}
	port := uint32(80)
	if facade.Scheme == "https" {
		port = 443
	}
	if facade.Port() != "" {
		n, _ := strconv.Atoi(facade.Port())
		port = uint32(n)
	}
	bins := make([]ostypes.PolicyNetworkBinary, 0, len(facadeBinaries))
	for _, path := range facadeBinaries {
		bins = append(bins, ostypes.PolicyNetworkBinary{Path: path})
	}
	spec := &ostypes.SandboxSpec{
		Environment:   maps.Clone(w.Launch.Env),
		Command:       slices.Clone(w.Launch.Command),
		Template:      &ostypes.SandboxTemplate{Image: image},
		RestartPolicy: ostypes.SandboxRestartNever,
		Policy: &ostypes.SandboxPolicy{
			Version: 1,
			Filesystem: &ostypes.FilesystemPolicy{
				IncludeWorkdir: true,
				ReadOnly:       []string{"/usr", "/lib", "/etc", "/bin", "/sbin", "/work", "/opt"},
				ReadWrite:      []string{"/tmp", "/workspace", "/var/lib/ach-runtime", "/home/ach"},
			},
			Landlock: &ostypes.LandlockPolicy{Compatibility: "best_effort"},
			NetworkPolicies: map[string]ostypes.NetworkPolicyRule{"facades": {
				Name:      "facades",
				Endpoints: []ostypes.PolicyNetworkEndpoint{{Host: facade.Hostname(), Port: port}},
				Binaries:  bins,
			}},
		},
	}
	// A unit is never registered without its Service.
	sandbox := SandboxName(string(b.uid), w.Name)
	if err := b.ensureService(ctx, a, sandbox); err != nil {
		return err
	}
	// launch.port is not in launch.env: the gateway record keeps it for the inventory.
	cctx, cancel := call(ctx)
	defer cancel()
	_, err = b.Sandboxes.Create(cctx, b.Config.Workspace, sandbox, spec,
		ostypes.WithLabels(map[string]string{AgentUIDLabel: string(b.uid), NameLabel: w.Name, portLabel: strconv.Itoa(int(w.Launch.Port))}),
		ostypes.WithServiceExposures(ostypes.ServiceExposure{
			TargetPort: uint32(w.Launch.Port), AuthorizationMode: ostypes.ServiceAuthorizationModeBearerPassthrough,
		}))
	if osv1.IsAlreadyExists(err) {
		return nil // a pass may repeat
	}
	return err
}

// Remove requests deletion with a short deadline and reports whether anything remains: the
// gateway record, or the workload pod, which outlives the record by up to its grace period.
func (b *openShellUnits) Remove(ctx context.Context, name string) (bool, error) {
	sandbox := SandboxName(string(b.uid), name)
	remaining, err := b.removeSandbox(ctx, b.reader, sandbox)
	if remaining || err != nil {
		return remaining, err
	}
	// Agent deletion is covered by the Service's owner reference.
	svc := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: b.serviceName(sandbox), Namespace: b.agent.Namespace}}
	return false, client.IgnoreNotFound(b.writer.Delete(ctx, svc))
}

func (o *OpenShell) removeSandbox(ctx context.Context, reader client.Reader, sandbox string) (bool, error) {
	cctx, cancel := call(ctx)
	s, err := o.Sandboxes.Get(cctx, o.Config.Workspace, sandbox)
	cancel()
	switch {
	case err == nil:
		if s.Status.Phase != ostypes.SandboxDeleting {
			dctx, cancel := context.WithTimeout(ctx, deleteDeadline)
			_, err := o.Sandboxes.Delete(dctx, o.Config.Workspace, sandbox)
			cancel()
			if err != nil && !osv1.IsDeadlineExceeded(err) && !osv1.IsNotFound(err) {
				return false, err
			}
		}
		return true, nil
	case !osv1.IsNotFound(err):
		return false, err
	}
	if reader == nil { // the finalizer: no process confirmation needed, the runtime is gone
		return false, nil
	}
	pod, err := o.workloadPod(ctx, reader, sandbox)
	return pod != nil, err
}

func (o *OpenShell) forUID(ctx context.Context, uid string) ([]*ostypes.Sandbox, error) {
	ctx, cancel := call(ctx)
	defer cancel()
	return o.Sandboxes.ListAll(ctx, o.Config.Workspace, ostypes.ListOptions{LabelSelector: AgentUIDLabel + "=" + uid})
}

func (b *openShellUnits) Owned(ctx context.Context) ([]string, error) {
	l, err := b.forUID(ctx, string(b.uid))
	names := make([]string, 0, len(l))
	for _, s := range l {
		names = append(names, s.Labels[NameLabel])
	}
	return names, err
}

func (b *openShellUnits) Live(ctx context.Context) ([]host.InventoryUnit, error) {
	l, err := b.forUID(ctx, string(b.uid))
	if err != nil {
		return nil, err
	}
	units := []host.InventoryUnit{}
	for _, s := range l {
		ref := s.Spec.Environment[refEnv]
		if s.Status.Phase != ostypes.SandboxReady || s.Labels[NameLabel] == "" || ref == "" {
			continue
		}
		port, err := strconv.ParseInt(s.Labels[portLabel], 10, 32)
		if err != nil || port < 1 {
			continue // not created by this backend: the unlisted-unit rule removes it
		}
		units = append(units, host.InventoryUnit{Name: s.Labels[NameLabel], WorkspaceRef: ref, BaseURL: b.url(b.agent.Namespace, s.Name, int32(port))})
	}
	return units, nil
}

// RemoveAgentSandboxes requests deletion of every sandbox labelled with agentUID and reports
// whether any remains on the gateway. Used by the Agent finalizer.
func (o *OpenShell) RemoveAgentSandboxes(ctx context.Context, agentUID string) (bool, error) {
	l, err := o.forUID(ctx, agentUID)
	if err != nil {
		return false, err
	}
	for _, s := range l {
		if _, err := o.removeSandbox(ctx, nil, s.Name); err != nil {
			return true, err
		}
	}
	return len(l) > 0, nil
}
