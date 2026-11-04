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
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"time"

	osv1 "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1"
	ostypes "github.com/NVIDIA/OpenShell/sdk/go/openshell/v1/types"
	"github.com/prometheus/client_golang/prometheus"
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/types"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/metrics"

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
	CA           []byte // ca.crt of the ACH_AGENT_OPENSHELL_TLS_SECRET Secret; nil with a plaintext gateway
}

// OpenShell is the gateway connection shared by every agent's backend and by the finalizer.
type OpenShell struct {
	Sandboxes sandboxes
	Config    OpenShellConfig
}

// gw is the gateway as the backend sees it: an unavailable gateway is marked as such so the
// host never mistakes "cannot see" for "gone".
func (o *OpenShell) gw() sandboxes { return markUnavailable{o.Sandboxes} }

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

// OpenShellFromEnv dials the gateway named by ACH_AGENT_OPENSHELL_GATEWAY once. With
// ACH_AGENT_OPENSHELL_TLS_SECRET (<namespace>/<name> of a Secret with ca.crt, tls.crt, tls.key)
// the operator verifies the gateway with that CA and authenticates with that client certificate
// (OU=operator); reader reads the Secret once, here. Without it the gateway is plaintext and the
// mesh is its only protection. An unset address is an error: the operator has no other sandbox
// backend.
func OpenShellFromEnv(ctx context.Context, reader client.Reader) (*OpenShell, error) {
	addr := envOr("ACH_AGENT_OPENSHELL_GATEWAY", "")
	if addr == "" {
		return nil, errors.New("ACH_AGENT_OPENSHELL_GATEWAY is required")
	}
	gw, err := gatewayHost(addr)
	if err != nil {
		return nil, err
	}
	cfg := osv1.Config{Address: addr}
	var ca []byte
	scheme := "http"
	if ref := envOr("ACH_AGENT_OPENSHELL_TLS_SECRET", ""); ref != "" {
		if cfg.TLS, ca, err = tlsFromSecret(ctx, reader, ref); err != nil {
			return nil, err
		}
		scheme = "https"
	}
	factory := func() (sandboxes, error) {
		c, err := osv1.NewClient(cfg)
		if err != nil {
			return nil, fmt.Errorf("OpenShell gateway %s: %w", addr, err)
		}
		return clientBox{SandboxInterface: c.Sandboxes(), workspaces: c.Workspaces(), client: c}, nil
	}
	if _, err := factory(); err != nil { // fail the start on an address or TLS material the SDK refuses
		return nil, err
	}
	return &OpenShell{Sandboxes: NewRebuilding(factory, rebuildEvery), Config: OpenShellConfig{
		Workspace:    envOr("ACH_AGENT_OPENSHELL_WORKSPACE", "default"),
		ServiceURL:   envOr("ACH_AGENT_OPENSHELL_SERVICE_URL", scheme+"://{workspace}--{sandbox}.{namespace}.svc.cluster.local:8080"),
		PodNamespace: envOr("ACH_AGENT_OPENSHELL_POD_NAMESPACE", "openshell"),
		GatewayHost:  gw,
		CA:           ca,
	}}, nil
}

// tlsFromSecret reads the operator's client certificate Secret ref (<namespace>/<name>) and writes
// its ca.crt, tls.crt and tls.key to a private directory under os.TempDir(): the SDK takes TLS
// material as file paths only, and re-reads them on every client build. It returns the TLS config
// and the CA.
func tlsFromSecret(ctx context.Context, reader client.Reader, ref string) (*osv1.TLSConfig, []byte, error) {
	ns, name, ok := strings.Cut(ref, "/")
	if !ok || ns == "" || name == "" {
		return nil, nil, fmt.Errorf("ACH_AGENT_OPENSHELL_TLS_SECRET %q is not <namespace>/<name>", ref)
	}
	var s corev1.Secret
	if err := reader.Get(ctx, client.ObjectKey{Namespace: ns, Name: name}, &s); err != nil {
		return nil, nil, fmt.Errorf("ACH_AGENT_OPENSHELL_TLS_SECRET %s: %w", ref, err)
	}
	dir, err := os.MkdirTemp("", "openshell-tls-")
	if err != nil {
		return nil, nil, fmt.Errorf("ACH_AGENT_OPENSHELL_TLS_SECRET: %w", err)
	}
	path := map[string]string{}
	for _, key := range []string{"ca.crt", "tls.crt", "tls.key"} {
		if len(s.Data[key]) == 0 {
			return nil, nil, fmt.Errorf("ACH_AGENT_OPENSHELL_TLS_SECRET %s has no %s", ref, key)
		}
		path[key] = filepath.Join(dir, key)
		if err := os.WriteFile(path[key], s.Data[key], 0o600); err != nil {
			return nil, nil, fmt.Errorf("ACH_AGENT_OPENSHELL_TLS_SECRET: write %s: %w", key, err)
		}
	}
	return &osv1.TLSConfig{CAFile: path["ca.crt"], CertFile: path["tls.crt"], KeyFile: path["tls.key"]}, s.Data["ca.crt"], nil
}

// SandboxName is stable and at most 19 characters (the OpenShell limit): up to 10 characters
// of the Agent name, so pod lists show whose sandbox it is, then 8 hex of a hash of the Agent
// UID and the Workspace name. Neither name is ever parsed back out of it.
func SandboxName(agentName, agentUID, workspaceName string) string {
	sum := sha256.Sum256([]byte(agentUID + "/" + workspaceName))
	prefix := strings.TrimRight(strings.ReplaceAll(agentName[:min(len(agentName), 10)], ".", "-"), "-")
	return prefix + "-" + hex.EncodeToString(sum[:])[:8]
}

// legacySandboxName is the name operators up to 0.2.0 gave a sandbox; one still running
// after an upgrade is found and removed under it.
func legacySandboxName(agentUID, workspaceName string) string {
	sum := sha256.Sum256([]byte(agentUID + "/" + workspaceName))
	return "ws-" + hex.EncodeToString(sum[:])[:16]
}

// sandboxFor names the Workspace's sandbox: the legacy name while a sandbox or workload pod of
// that name exists, else the current one.
func (b *openShellUnits) sandboxFor(ctx context.Context, name string) (string, error) {
	current := SandboxName(b.agent.Name, string(b.uid), name)
	cctx, cancel := call(ctx)
	_, err := b.gw().Get(cctx, b.Config.Workspace, current)
	cancel()
	if !osv1.IsNotFound(err) {
		return current, err
	}
	legacy := legacySandboxName(string(b.uid), name)
	cctx, cancel = call(ctx)
	_, err = b.gw().Get(cctx, b.Config.Workspace, legacy)
	cancel()
	switch {
	case err == nil:
		return legacy, nil
	case !osv1.IsNotFound(err):
		return "", err
	}
	pod, err := b.workloadPod(ctx, b.reader, legacy)
	if err != nil || pod == nil {
		return current, err
	}
	return legacy, nil
}

type openShellUnits struct {
	*OpenShell
	agent  types.NamespacedName
	uid    types.UID
	reader client.Reader
	writer client.Client
	timed  map[string]types.UID // sandbox -> workload pod whose start is recorded
	since  time.Time            // pods ready before this backend existed are not recorded
	swept  time.Time            // last Sweep listing; zero: the next pass sweeps
}

// sandboxStartSeconds times a workload pod's start from its condition timestamps, which have
// whole-second resolution: volume (created to scheduled; WaitForFirstConsumer binds the
// workspace volume first), init (OpenShell's init containers) and container (to ready).
var sandboxStartSeconds = prometheus.NewHistogramVec(prometheus.HistogramOpts{
	Name:    "ach_agent_sandbox_start_seconds",
	Help:    "Sandbox workload pod start-up duration by phase",
	Buckets: []float64{0.5, 1, 2, 3, 5, 8, 13, 21, 34, 60, 120},
}, []string{"phase"})

func init() { metrics.Registry.MustRegister(sandboxStartSeconds) }

// observeStart records the start phases of a ready workload pod, once per pod.
func (b *openShellUnits) observeStart(sandbox string, pod *corev1.Pod) {
	if b.timed[sandbox] == pod.UID {
		return
	}
	at := map[corev1.PodConditionType]time.Time{}
	for _, c := range pod.Status.Conditions {
		if c.Status == corev1.ConditionTrue {
			at[c.Type] = c.LastTransitionTime.Time
		}
	}
	scheduled, initialized, ready := at[corev1.PodScheduled], at[corev1.PodInitialized], at[corev1.ContainersReady]
	if scheduled.IsZero() || initialized.IsZero() || ready.IsZero() {
		return // not ready yet: a later pass records it
	}
	b.timed[sandbox] = pod.UID
	if ready.Before(b.since.Truncate(time.Second)) {
		return // an earlier operator, or this one before a restart of the agent's loop, saw it start
	}
	sandboxStartSeconds.WithLabelValues("volume").Observe(scheduled.Sub(pod.CreationTimestamp.Time).Seconds())
	sandboxStartSeconds.WithLabelValues("init").Observe(initialized.Sub(scheduled).Seconds())
	sandboxStartSeconds.WithLabelValues("container").Observe(ready.Sub(initialized).Seconds())
}

// ForAgent is the unit backend of one agent.
func (o *OpenShell) ForAgent(agent types.NamespacedName, uid types.UID, reader client.Reader, writer client.Client) host.Backend {
	return &openShellUnits{OpenShell: o, agent: agent, uid: uid, reader: reader, writer: writer,
		timed: map[string]types.UID{}, since: time.Now()}
}

func (o *OpenShell) url(namespace, sandbox string, port int32) string {
	return strings.NewReplacer("{workspace}", o.Config.Workspace, "{sandbox}", sandbox,
		"{namespace}", namespace, "{port}", strconv.Itoa(int(port))).Replace(o.Config.ServiceURL)
}

// serviceName is the ExternalName Service that makes a sandbox's URL resolvable with plain
// cluster DNS: <workspace>--<sandbox>.<ns>.svc.cluster.local -> the gateway Service. The gateway
// routes that Host because *.<ns>.svc.cluster.local is one of its server_sans.
func (o *OpenShell) serviceName(sandbox string) string { return o.Config.Workspace + "--" + sandbox }

func (b *openShellUnits) ensureService(ctx context.Context, a *achv1alpha1.Agent, sandbox, workspace string) error {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: b.serviceName(sandbox), Namespace: a.Namespace,
			Labels: map[string]string{AgentUIDLabel: string(a.UID), NameLabel: workspace}},
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
	return b.url(b.agent.Namespace, SandboxName(b.agent.Name, string(b.uid), w.Name), w.Launch.Port)
}

func (b *openShellUnits) State(ctx context.Context, name string) (host.UnitState, error) {
	sandbox, err := b.sandboxFor(ctx, name)
	if err != nil {
		return 0, err
	}
	cctx, cancel := call(ctx)
	s, err := b.gw().Get(cctx, b.Config.Workspace, sandbox)
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
		b.observeStart(s.Name, pod)
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
	var agentPolicy *apiextensionsv1.JSON
	if a.Spec.Sandbox != nil && a.Spec.Sandbox.OpenShell != nil {
		agentPolicy = a.Spec.Sandbox.OpenShell.Policy
	}
	policy, err := BuildPolicy(p.Spec.Sandbox.OpenShell.Policy, agentPolicy, facade.Hostname(), port)
	if err != nil {
		return rejected(err, host.ReasonSandboxPolicyRejected)
	}
	resources, err := Resources(p.Spec.Sandbox.OpenShell.Resources)
	if err != nil {
		return rejected(err, host.ReasonSandboxResourcesUnsupported)
	}
	spec := &ostypes.SandboxSpec{
		Environment:   maps.Clone(w.Launch.Env),
		Command:       slices.Clone(w.Launch.Command),
		Template:      &ostypes.SandboxTemplate{Image: image, Resources: resources},
		RestartPolicy: ostypes.SandboxRestartNever,
		Policy:        policy,
	}
	// A host loop that was stopped (Agent deleted) must not start a sandbox the finalizer
	// would then have to find.
	if err := ctx.Err(); err != nil {
		return err
	}
	if persist := workspacePersistence(a, p); persist != nil && persist.Type == "volume" {
		if len(b.Config.CA) == 0 {
			return errVolumeNeedsTLS
		}
		claim, err := b.ensureVolume(ctx, a, persist.Volume, w.Name)
		if err != nil {
			return err
		}
		spec.Template.DriverConfig = driverConfig(claim)
	}
	// A unit is never registered without its Service.
	sandbox := SandboxName(b.agent.Name, string(b.uid), w.Name)
	if err := b.ensureService(ctx, a, sandbox, w.Name); err != nil {
		return err
	}
	// launch.port is not in launch.env: the gateway record keeps it for the inventory.
	// Not derived from the loop context: Stop waits for a create already sent, so the
	// finalizer's inventory sees the sandbox. callDeadline bounds it.
	cctx, cancel := call(context.WithoutCancel(ctx))
	defer cancel()
	_, err = b.gw().Create(cctx, b.Config.Workspace, sandbox, spec,
		ostypes.WithLabels(map[string]string{AgentUIDLabel: string(b.uid), NameLabel: w.Name, portLabel: strconv.Itoa(int(w.Launch.Port))}),
		ostypes.WithServiceExposures(ostypes.ServiceExposure{
			TargetPort: uint32(w.Launch.Port), AuthorizationMode: ostypes.ServiceAuthorizationModeBearerPassthrough,
		}))
	if osv1.IsAlreadyExists(err) {
		return nil // a pass may repeat
	}
	if osv1.IsInvalidArgument(err) {
		// The gateway refuses the spec: say which input it objects to.
		if resources != nil && strings.Contains(strings.ToLower(err.Error()), "resource") {
			return rejected(fmt.Errorf("%w: %v", ErrResourcesUnsupported, err), host.ReasonSandboxResourcesUnsupported)
		}
		return rejected(fmt.Errorf("%w: %v", ErrPolicyRejected, err), host.ReasonSandboxPolicyRejected)
	}
	return err
}

// reasonError carries the Agent condition reason of a refused sandbox; the host loop writes
// it onto the Agent (host.StatusWriter).
type reasonError struct {
	error
	reason string
}

func (e reasonError) Unwrap() error        { return e.error }
func (e reasonError) StatusReason() string { return e.reason }

func rejected(err error, reason string) error { return reasonError{error: err, reason: reason} }

// Remove requests deletion with a short deadline and reports whether anything remains: the
// gateway record, or the workload pod, which outlives the record by up to its grace period.
func (b *openShellUnits) Remove(ctx context.Context, name string) (bool, error) {
	sandbox, err := b.sandboxFor(ctx, name)
	if err != nil {
		return false, err
	}
	remaining, err := b.removeSandbox(ctx, b.reader, sandbox)
	if remaining || err != nil {
		return remaining, err
	}
	delete(b.timed, sandbox)
	if err := b.markDetached(ctx, name); err != nil {
		return false, err
	}
	// Agent deletion is covered by the Service's owner reference.
	svc := &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: b.serviceName(sandbox), Namespace: b.agent.Namespace}}
	return false, client.IgnoreNotFound(b.writer.Delete(ctx, svc))
}

func (o *OpenShell) removeSandbox(ctx context.Context, reader client.Reader, sandbox string) (bool, error) {
	cctx, cancel := call(ctx)
	s, err := o.gw().Get(cctx, o.Config.Workspace, sandbox)
	cancel()
	switch {
	case err == nil:
		if s.Status.Phase != ostypes.SandboxDeleting {
			dctx, cancel := context.WithTimeout(ctx, deleteDeadline)
			_, err := o.gw().Delete(dctx, o.Config.Workspace, sandbox)
			cancel()
			if err != nil && !osv1.IsDeadlineExceeded(err) && !osv1.IsNotFound(err) {
				return false, err
			}
		}
		return true, nil
	case !osv1.IsNotFound(err):
		return false, err
	}
	if reader == nil {
		return false, nil
	}
	pod, err := o.workloadPod(ctx, reader, sandbox)
	return pod != nil, err
}

func (o *OpenShell) forUID(ctx context.Context, uid string) ([]*ostypes.Sandbox, error) {
	ctx, cancel := call(ctx)
	defer cancel()
	return o.gw().ListAll(ctx, o.Config.Workspace, ostypes.ListOptions{LabelSelector: AgentUIDLabel + "=" + uid})
}

func (b *openShellUnits) Owned(ctx context.Context) ([]string, error) {
	l, err := b.forUID(ctx, string(b.uid))
	names := make([]string, 0, len(l))
	for _, s := range l {
		names = append(names, s.Labels[NameLabel])
	}
	if err != nil {
		return names, err
	}
	// The gateway forgets a sandbox before its workload pod is gone, and Remove keeps the
	// Service until then: list the Service too, so a later pass removes it.
	var svcs corev1.ServiceList
	if err := b.writer.List(ctx, &svcs, client.InNamespace(b.agent.Namespace),
		client.MatchingLabels{AgentUIDLabel: string(b.uid)}); err != nil {
		return names, err
	}
	for _, svc := range svcs.Items {
		if n := svc.Labels[NameLabel]; n != "" && !slices.Contains(names, n) {
			names = append(names, n)
		}
	}
	return names, nil
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
// whether any remains: on the gateway, or as a workload pod that outlives its record (reader
// is the uncached client that can read pods in the sandbox namespace). Used by the Agent finalizer.
func (o *OpenShell) RemoveAgentSandboxes(ctx context.Context, reader client.Reader, agentUID string) (bool, error) {
	l, err := o.forUID(ctx, agentUID)
	if err != nil {
		return false, err
	}
	for _, s := range l {
		if _, err := o.removeSandbox(ctx, reader, s.Name); err != nil {
			return true, err
		}
	}
	return len(l) > 0, nil
}
