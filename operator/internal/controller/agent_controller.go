// SPDX-License-Identifier: MIT

// Package controller — AgentReconciler renders an Agent (+ its AgentProfile) into the
// workspace-v3 ConfigMap and a single-replica control StatefulSet (runtime contract v2), plus
// the per-Workspace execution units it hosts over /host/v1 (internal/host). The
// control unit self-hydrates against ACH at boot, so this reconciler owns NO init container
// and derives WorkloadReady from pod.status (probe-backed) only.
package controller

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	apimeta "k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/builder"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"
	"sigs.k8s.io/controller-runtime/pkg/handler"
	"sigs.k8s.io/controller-runtime/pkg/log"
	"sigs.k8s.io/controller-runtime/pkg/reconcile"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const achAgentOwnerKind = "Agent"

const (
	condReady           = "Ready"
	condProfileResolved = "ProfileResolved"
	condSecretsResolved = "SecretsResolved"
	condWorkloadApplied = "WorkloadApplied"
	condWorkloadReady   = "WorkloadReady"
	// condGatewayAvailable is written by the host loop and the finalizer, never by Reconcile;
	// it does not gate Ready.
	condGatewayAvailable = "GatewayAvailable"
)

var requiredConds = []string{condProfileResolved, condSecretsResolved, condWorkloadApplied, condWorkloadReady}

// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=agents,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=agents/finalizers,verbs=update
// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=agents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=agentprofiles,verbs=get;list;watch
// +kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims,verbs=get;list;watch;create;update;patch;delete
// Header, key, channel and runtime-storage-credential Secrets are read-only. The sole write is
// create for the UID-owned key Secret; existing keys are never updated or deleted. No Secret
// verbs are ever granted to the control or sandbox ServiceAccounts.
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;create
// Pods: delete only for replaceStaleControlPod.
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;delete
// +kubebuilder:rbac:groups="",resources=events,verbs=create;patch
// +kubebuilder:rbac:groups=gateway.networking.k8s.io,resources=httproutes,verbs=get;list;watch;create;update;patch;delete

// AgentReconciler reconciles Agent objects. APIReader is the UNCACHED client used for
// Secret content reads (so Secret bodies are never cached in this shared binary).
type AgentReconciler struct {
	client.Client
	APIReader client.Reader
	Scheme    *runtime.Scheme

	// Storage is the operator/chart-level global Storage backend config, never per-agent. An
	// empty Bucket means the backend is unconfigured: ValidateRuntimeStorage fails closed
	// (StorageUnavailable) only for an Agent whose resolved config actually requires
	// persistence/artifacts; an all-disabled agent is unaffected.
	Storage render.RuntimeStorageOptions

	// RouteParent is the default parentRef of an Agent's HTTPRoute.
	RouteParent achv1alpha1.ParentRef

	// HTTPRouteAvailable reports whether the cluster serves gateway.networking.k8s.io/v1
	// HTTPRoute. An Agent that asks for a route without it gets WorkloadApplied=False.
	HTTPRouteAvailable bool

	// Sandboxes deletes an agent's OpenShell sandboxes on finalization. Nil when the
	// operator has no gateway configured.
	Sandboxes sandboxRemover
}

const executionFinalizer = openshell.SandboxFinalizer

// sandboxRemover is *openshell.OpenShell.
type sandboxRemover interface {
	RemoveAgentSandboxes(ctx context.Context, agentUID string) (remaining bool, err error)
}

// ensureExecutionFinalizer adds the sandbox finalizer and reports whether it changed the
// agent. It is only removed by finalization.
func ensureExecutionFinalizer(a *achv1alpha1.Agent) bool {
	return controllerutil.AddFinalizer(a, executionFinalizer)
}

// finalizeExecutionUnits deletes the agent's OpenShell sandboxes (they cannot carry an owner
// reference) and then lets the deletion proceed. Selection is by agent UID, so a newer
// Agent with the same name is never touched. Gateway errors requeue with backoff.
func (r *AgentReconciler) finalizeExecutionUnits(ctx context.Context, a *achv1alpha1.Agent) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(a, executionFinalizer) {
		return ctrl.Result{}, nil
	}
	if r.Sandboxes == nil {
		// No event recorder on this reconciler: the warning is logged.
		log.FromContext(ctx).Info("Warning: OpenShellUnavailable: no OpenShell gateway configured; removing the sandbox finalizer", "achagent", client.ObjectKeyFromObject(a))
	} else {
		remaining, err := r.Sandboxes.RemoveAgentSandboxes(ctx, string(a.UID))
		if err != nil {
			return ctrl.Result{}, fmt.Errorf("delete OpenShell sandboxes of %s: %w", a.Name, err)
		}
		if remaining {
			return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
		}
	}
	controllerutil.RemoveFinalizer(a, executionFinalizer)
	return ctrl.Result{}, r.Update(ctx, a)
}

// graceMarginSeconds is added to the runtime drain to get the minimum grace period.
const graceMarginSeconds = 15

// effectiveShutdownTimeout is workspace.shutdownTimeoutSeconds after the profile/agent merge.
func effectiveShutdownTimeout(p *achv1alpha1.AgentProfile, a *achv1alpha1.Agent) int64 {
	if w := render.ResolveWorkspace(a.Spec.Workspace, p.Spec.Defaults.Workspace); w != nil && w.ShutdownTimeoutSeconds != nil {
		return *w.ShutdownTimeoutSeconds
	}
	return 0
}

// checkGracePeriod rejects a control terminationGracePeriodSeconds below the runtime drain
// (workspace.shutdownTimeoutSeconds) plus a margin: the kubelet would kill the pod mid-drain.
func checkGracePeriod(p *achv1alpha1.AgentProfile, a *achv1alpha1.Agent) error {
	grace := defaultGraceSeconds
	if g := p.Spec.Control.TerminationGracePeriodSeconds; g != nil {
		grace = *g
	}
	if min := effectiveShutdownTimeout(p, a) + graceMarginSeconds; grace < min {
		return fmt.Errorf("control terminationGracePeriodSeconds %d is below workspace.shutdownTimeoutSeconds + %d = %d", grace, graceMarginSeconds, min)
	}
	return nil
}

//nolint:gocyclo // Single linear resolve→render→apply→status flow; splitting scatters status ordering.
func (r *AgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("agent", req.NamespacedName)

	var agent achv1alpha1.Agent
	if err := r.Get(ctx, req.NamespacedName, &agent); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !agent.DeletionTimestamp.IsZero() {
		return r.finalizeExecutionUnits(ctx, &agent)
	}
	logger.Info("reconciling agent")

	// The sandbox finalizer goes on before anything can create a sandbox (the control pod,
	// and so any host pass, come later in this reconcile).
	if ensureExecutionFinalizer(&agent) {
		if err := r.Update(ctx, &agent); err != nil {
			return ctrl.Result{}, err
		}
	}

	var conds []metav1.Condition

	// 1. AgentProfile.
	var profile achv1alpha1.AgentProfile
	if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: agent.Spec.ProfileRef.Name}, &profile); err != nil {
		if apierrors.IsNotFound(err) {
			setCond(&conds, condProfileResolved, metav1.ConditionFalse, "ProfileNotFound", fmt.Sprintf("AgentProfile %q not found", agent.Spec.ProfileRef.Name), agent.Generation)
			return r.finish(ctx, &agent, conds)
		}
		return ctrl.Result{}, fmt.Errorf("get agentprofile: %w", err)
	}

	// 1b. A profile-named control SA is external: it must already exist. Created later, the
	// ServiceAccount watch (agentsForControlServiceAccount) re-enqueues this agent.
	if name := profile.Spec.Control.ServiceAccountName; name != "" {
		var sa corev1.ServiceAccount
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: name}, &sa); err != nil {
			if apierrors.IsNotFound(err) {
				setCond(&conds, condProfileResolved, metav1.ConditionFalse, "ServiceAccountNotFound",
					fmt.Sprintf("ServiceAccount %q named by AgentProfile %q spec.control.serviceAccountName not found in namespace %q", name, profile.Name, agent.Namespace), agent.Generation)
				return r.finish(ctx, &agent, conds)
			}
			return ctrl.Result{}, fmt.Errorf("get control serviceaccount: %w", err)
		}
	}
	setCond(&conds, condProfileResolved, metav1.ConditionTrue, "ProfileFound", "", agent.Generation)

	// 2. Render (workspace-v3).
	cfg, bindings, err := render.Render(profile, agent)
	if err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "RenderFailed", err.Error(), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	configJSON, err := json.Marshal(cfg)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("marshal config: %w", err)
	}

	// 3. The control pod must outlive the runtime drain.
	if err := checkGracePeriod(&profile, &agent); err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "InvalidGracePeriod", err.Error(), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}

	// 4. Global runtime Storage: required configuration only, never cloud reachability.
	// All-disabled policies skip this dependency entirely (RuntimeRequiresStorage false).
	requiresStorage := render.RuntimeRequiresStorage(cfg)
	if err := render.ValidateRuntimeStorage(cfg, r.Storage); err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "StorageUnavailable", err.Error(), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}

	// 5. Referenced Secrets + keys (model/MCP headers, ACH key, channels, memory), merged with
	// the global storage credential Secret's required keys when storage is required.
	refSecrets := render.ReferencedSecrets(profile, agent)
	checkSecrets, hashSecretsMap := refSecrets, refSecrets
	if requiresStorage && r.Storage.CredentialsSecretName != "" {
		checkSecrets = mergeSecretKeys(refSecrets, r.Storage.CredentialsSecretName, requiredStorageCredentialKeys)
		hashSecretsMap = mergeSecretKeys(refSecrets, r.Storage.CredentialsSecretName, allStorageCredentialKeys)
	}
	if failReason, failMsg := r.checkSecrets(ctx, agent.Namespace, checkSecrets); failReason != "" {
		setCond(&conds, condSecretsResolved, metav1.ConditionFalse, failReason, failMsg, agent.Generation)
		return r.finish(ctx, &agent, conds)
	}

	// The private key Secret is created only after render and the authored Secret checks pass.
	// Errors are deliberately sanitized so the key can never appear in status or logs.
	if err := r.ensureWorkspaceKey(ctx, &agent); err != nil {
		setCond(&conds, condSecretsResolved, metav1.ConditionFalse, "SecretConflict", "key Secret is not controlled by this Agent or is empty", agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	setCond(&conds, condSecretsResolved, metav1.ConditionTrue, "SecretsFound", "", agent.Generation)
	hashSecretsMap = mergeSecretKeys(hashSecretsMap, workspaceKeySecretName(string(agent.UID)), []string{workspaceKeyDataKey})

	// 6. Env (once, with the private storage env merged in when required) + salted secret
	// hash + config hash.
	env := buildAgentEnv(&agent, &profile, bindings)
	env = overrideEnv(env, []corev1.EnvVar{{Name: "ACH_SANDBOX_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: workspaceKeySecretName(string(agent.UID))}, Key: workspaceKeyDataKey,
	}}}})
	if requiresStorage {
		env = overrideEnv(env, render.RuntimeStorageEnv(r.Storage))
	}
	envJSON, err := json.Marshal(env)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("marshal env: %w", err)
	}
	secretHash, err := r.hashSecrets(ctx, &agent, hashSecretsMap)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("hash secrets: %w", err)
	}
	configHash := computeConfigHash(configJSON, envJSON, profile.Spec.Control.Image, secretHash)

	sts := buildStatefulSet(&agent, &profile, configHash, env)
	// 7. Apply children.
	if err := r.apply(ctx, &agent, buildConfigMap(&agent, configJSON)); err != nil {
		return r.applyFail(ctx, &agent, conds, "ConfigMap", err)
	}
	// A profile-named control SA is pre-existing and external: never created/owned here.
	if profile.Spec.Control.ServiceAccountName == "" {
		if err := r.apply(ctx, &agent, buildControlServiceAccount(&agent)); err != nil {
			return r.applyFail(ctx, &agent, conds, "ServiceAccount", err)
		}
	}
	if err := r.apply(ctx, &agent, buildControlService(&agent)); err != nil {
		return r.applyFail(ctx, &agent, conds, "Service", err)
	}
	if profile.Spec.Control.Persistence != nil && profile.Spec.Control.Persistence.Enabled {
		if err := r.applyPVC(ctx, &agent, &profile); err != nil {
			return r.applyFail(ctx, &agent, conds, "PVC", err)
		}
	}
	if err := r.apply(ctx, &agent, sts); err != nil {
		return r.applyFail(ctx, &agent, conds, "StatefulSet", err)
	}
	if err := r.replaceStaleControlPod(ctx, &agent); err != nil {
		return r.applyFail(ctx, &agent, conds, "ControlPod", err)
	}
	if needsService(&agent) {
		if err := r.apply(ctx, &agent, buildService(&agent, &profile)); err != nil {
			return r.applyFail(ctx, &agent, conds, "Service", err)
		}
	} else if err := r.prune(ctx, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: httpServiceName(agent.Name), Namespace: agent.Namespace}}); err != nil {
		// Converge expose.service true→false: owner-ref GC only fires on Agent delete, not
		// when the owner stops desiring the child.
		return r.applyFail(ctx, &agent, conds, "Service", err)
	}
	if res, err := r.reconcileRoute(ctx, &agent, conds); res != nil || err != nil {
		return *res, err
	}
	setCond(&conds, condWorkloadApplied, metav1.ConditionTrue, "WorkloadApplied", "", agent.Generation)

	// 8. WorkloadReady from pod.status (probe-backed).
	r.deriveWorkloadReady(ctx, &agent, configHash, &conds)

	// 9. Ready is aggregated in finish() every pass.
	return r.finish(ctx, &agent, conds)
}

// requiredStorageCredentialKeys/allStorageCredentialKeys are the AWS SecretKeyRef key names
// RuntimeStorageEnv wires into the global credentials Secret: the first two are required
// (merged into the checkSecrets gate), the third (session token) is optional and
// merged only into the rollout-hash input, so its own add/change/removal still rolls the pod
// without ever being treated as a missing-key failure (Task2 D1 amendment, Step 6).
var (
	requiredStorageCredentialKeys = []string{"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}
	allStorageCredentialKeys      = []string{"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"}
)

// mergeSecretKeys returns a copy of base with keys appended under secretName (deduplicating
// against any keys the same Secret already carries for another role — e.g. a channel secret
// reused as the storage credential Secret).
func mergeSecretKeys(base map[string][]string, secretName string, keys []string) map[string][]string {
	out := make(map[string][]string, len(base)+1)
	for k, v := range base {
		out[k] = v
	}
	have := map[string]struct{}{}
	for _, k := range out[secretName] {
		have[k] = struct{}{}
	}
	merged := append([]string(nil), out[secretName]...)
	for _, k := range keys {
		if _, ok := have[k]; !ok {
			merged = append(merged, k)
			have[k] = struct{}{}
		}
	}
	out[secretName] = merged
	return out
}

func (r *AgentReconciler) checkSecrets(ctx context.Context, ns string, refSecrets map[string][]string) (reason, msg string) {
	names := slices.Sorted(maps.Keys(refSecrets))
	for _, name := range names {
		var s corev1.Secret
		if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &s); err != nil {
			if apierrors.IsNotFound(err) {
				return "SecretMissing", fmt.Sprintf("secret %s not found", name)
			}
			return "SecretReadFailed", fmt.Sprintf("get secret %s: %v", name, err)
		}
		for _, key := range refSecrets[name] {
			if _, ok := s.Data[key]; !ok {
				return "SecretMissing", fmt.Sprintf("secret %s key %s", name, key)
			}
		}
	}
	return "", ""
}

// apply upserts a child with an owner ref (GC on Agent delete).
func (r *AgentReconciler) apply(ctx context.Context, owner *achv1alpha1.Agent, desired client.Object) error {
	obj := desired.DeepCopyObject().(client.Object)
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, obj, func() error {
		copySpec(obj, desired)
		return controllerutil.SetControllerReference(owner, obj, r.Scheme)
	})
	return err
}

// prune deletes a child the agent no longer desires (expose.service false). Owner-ref GC only fires on Agent delete,
// so convergence needs an explicit delete. Idempotent — NotFound is a no-op.
func (r *AgentReconciler) prune(ctx context.Context, obj client.Object) error {
	if err := r.Delete(ctx, obj); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// applyPVC creates the PVC once. When retainPolicy=Retain the PVC gets NO owner-ref, so it
// survives Agent deletion (create-once; PVC spec is immutable). A changed size is logged,
// not auto-applied.
func (r *AgentReconciler) applyPVC(ctx context.Context, owner *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) error {
	pvc, err := buildPVC(owner, p)
	if err != nil {
		return err
	}
	var existing corev1.PersistentVolumeClaim
	getErr := r.Get(ctx, types.NamespacedName{Namespace: pvc.Namespace, Name: pvc.Name}, &existing)
	if getErr == nil {
		want := pvc.Spec.Resources.Requests[corev1.ResourceStorage]
		have := existing.Spec.Resources.Requests[corev1.ResourceStorage]
		if want.Cmp(have) != 0 {
			log.FromContext(ctx).Info("PVC size change is not auto-applied (create-once); expand manually if the storage class allows it", "pvc", pvc.Name, "want", want.String(), "have", have.String())
		}
		return nil
	}
	if !apierrors.IsNotFound(getErr) {
		return getErr
	}
	retain := p.Spec.Control.Persistence.RetainPolicy != nil && *p.Spec.Control.Persistence.RetainPolicy == "Retain"
	if !retain {
		if err := controllerutil.SetControllerReference(owner, pvc, r.Scheme); err != nil {
			return err
		}
	}
	if err := r.Create(ctx, pvc); err != nil && !apierrors.IsAlreadyExists(err) {
		return err
	}
	return nil
}

// replaceStaleControlPod deletes the control pod when it is stuck on an old StatefulSet
// revision and is not Ready. The StatefulSet controller (OrderedReady) never replaces a pod
// that is not Running and Ready, so fixing a bad image or config would otherwise leave the
// crash-looping pod in place forever (kubernetes/kubernetes#67250 "forced rollback"). A Ready
// pod rolls normally and is never touched here.
func (r *AgentReconciler) replaceStaleControlPod(ctx context.Context, a *achv1alpha1.Agent) error {
	key := types.NamespacedName{Namespace: a.Namespace, Name: render.ControlName(a.Name)}
	var sts appsv1.StatefulSet
	if err := r.APIReader.Get(ctx, key, &sts); err != nil {
		return client.IgnoreNotFound(err)
	}
	if sts.Status.UpdateRevision == "" {
		return nil
	}
	var pod corev1.Pod
	key.Name += "-0"
	if err := r.APIReader.Get(ctx, key, &pod); err != nil {
		return client.IgnoreNotFound(err)
	}
	if pod.Labels[appsv1.ControllerRevisionHashLabelKey] == sts.Status.UpdateRevision || !pod.DeletionTimestamp.IsZero() {
		return nil
	}
	for _, c := range pod.Status.Conditions {
		if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
			return nil
		}
	}
	log.FromContext(ctx).Info("replacing a not-Ready control pod stuck on an old revision",
		"pod", pod.Name, "podRevision", pod.Labels[appsv1.ControllerRevisionHashLabelKey], "updateRevision", sts.Status.UpdateRevision)
	return client.IgnoreNotFound(r.Delete(ctx, &pod))
}

func (r *AgentReconciler) applyFail(ctx context.Context, a *achv1alpha1.Agent, conds []metav1.Condition, what string, err error) (ctrl.Result, error) {
	setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "ApplyFailed", fmt.Sprintf("apply %s: %v", what, err), a.Generation)
	return r.finish(ctx, a, conds)
}

func (r *AgentReconciler) deriveWorkloadReady(ctx context.Context, a *achv1alpha1.Agent, configHash string, conds *[]metav1.Condition) {
	var pods corev1.PodList
	if err := r.List(ctx, &pods, client.InNamespace(a.Namespace), client.MatchingLabels(agentSelectorLabels(a.Name))); err != nil {
		setCond(conds, condWorkloadReady, metav1.ConditionFalse, "ListFailed", err.Error(), a.Generation)
		return
	}
	for i := range pods.Items {
		pod := &pods.Items[i]
		if pod.Annotations[configHashAnnotation] != configHash {
			continue // stale generation
		}
		for _, c := range pod.Status.Conditions {
			if c.Type == corev1.PodReady && c.Status == corev1.ConditionTrue {
				setCond(conds, condWorkloadReady, metav1.ConditionTrue, "PodReady", "", a.Generation)
				return
			}
		}
	}
	setCond(conds, condWorkloadReady, metav1.ConditionFalse, "PodNotReady", "no current-generation pod is Ready", a.Generation)
}

// hashSecrets returns a salted (HMAC keyed by the Agent UID) digest over the .Data of the
// referenced Secret keys. A content rotation rolls the pod; the digest
// is not reversible to the token values. Reads via APIReader (uncached).
func (r *AgentReconciler) hashSecrets(ctx context.Context, a *achv1alpha1.Agent, refSecrets map[string][]string) (string, error) {
	// name → keys to hash (identity secret's key + each channel secret's referenced keys).
	want := map[string]map[string]struct{}{}
	add := func(name, key string) {
		if want[name] == nil {
			want[name] = map[string]struct{}{}
		}
		want[name][key] = struct{}{}
	}
	for name, keys := range refSecrets {
		for _, k := range keys {
			add(name, k)
		}
	}
	names := slices.Sorted(maps.Keys(want))

	mac := hmac.New(sha256.New, []byte(a.UID))
	for _, name := range names {
		var s corev1.Secret
		if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: a.Namespace, Name: name}, &s); err != nil {
			if apierrors.IsNotFound(err) {
				mac.Write([]byte(name))
				mac.Write([]byte("\x00missing\x00"))
				continue
			}
			return "", err
		}
		keys := slices.Sorted(maps.Keys(want[name]))
		for _, k := range keys {
			mac.Write([]byte(name))
			mac.Write([]byte{0})
			mac.Write([]byte(k))
			mac.Write([]byte{0})
			mac.Write(s.Data[k]) // absent key → nil bytes; content rotation still changes the mac
			mac.Write([]byte{0})
		}
	}
	return hex.EncodeToString(mac.Sum(nil))[:16], nil
}

// finish merges this-pass conditions, forces any un-evaluated required condition to Unknown
// (so it cannot read stale-True), recomputes Ready from the merged set, and persists status.
func (r *AgentReconciler) finish(ctx context.Context, a *achv1alpha1.Agent, conds []metav1.Condition) (ctrl.Result, error) {
	for _, t := range requiredConds {
		if apimeta.FindStatusCondition(conds, t) == nil {
			setCond(&conds, t, metav1.ConditionUnknown, "NotEvaluated", "reconcile bailed before evaluating this condition", a.Generation)
		}
	}
	var fresh achv1alpha1.Agent
	if err := r.Get(ctx, types.NamespacedName{Namespace: a.Namespace, Name: a.Name}, &fresh); err != nil {
		return ctrl.Result{}, err
	}
	for _, c := range conds {
		setCond(&fresh.Status.Conditions, c.Type, c.Status, c.Reason, c.Message, c.ObservedGeneration)
	}
	if allTrue(fresh.Status.Conditions, requiredConds...) {
		setCond(&fresh.Status.Conditions, condReady, metav1.ConditionTrue, "Ready", "", a.Generation)
	} else {
		// A False condition explains the failure better than one that was not evaluated
		// because the reconcile stopped earlier: prefer the first False, then the first Unknown.
		reason, msg := "NotReady", "one or more required conditions are not satisfied"
	pick:
		for _, want := range []metav1.ConditionStatus{metav1.ConditionFalse, metav1.ConditionUnknown} {
			for _, t := range requiredConds {
				if c := apimeta.FindStatusCondition(fresh.Status.Conditions, t); c != nil && c.Status == want {
					reason, msg = c.Reason, c.Message
					break pick
				}
			}
		}
		setCond(&fresh.Status.Conditions, condReady, metav1.ConditionFalse, reason, msg, a.Generation)
	}
	fresh.Status.ObservedGeneration = fresh.Generation

	if err := r.Status().Update(ctx, &fresh); err != nil {
		return ctrl.Result{}, fmt.Errorf("update status: %w", err)
	}
	return ctrl.Result{}, nil
}

func setCond(conds *[]metav1.Condition, t string, s metav1.ConditionStatus, reason, msg string, gen int64) {
	apimeta.SetStatusCondition(conds, metav1.Condition{Type: t, Status: s, Reason: reason, Message: msg, ObservedGeneration: gen})
}

func allTrue(conds []metav1.Condition, condTypes ...string) bool {
	for _, t := range condTypes {
		c := apimeta.FindStatusCondition(conds, t)
		if c == nil || c.Status != metav1.ConditionTrue {
			return false
		}
	}
	return true
}

func (r *AgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	b := ctrl.NewControllerManagedBy(mgr).
		For(&achv1alpha1.Agent{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ServiceAccount{}).
		Watches(&achv1alpha1.AgentProfile{}, handler.EnqueueRequestsFromMapFunc(r.agentsForProfile)).
		Watches(&corev1.ServiceAccount{}, handler.EnqueueRequestsFromMapFunc(r.agentsForControlServiceAccount)).
		Watches(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(r.agentsForSecret), builder.OnlyMetadata).
		Named("agent")
	if r.HTTPRouteAvailable {
		b = b.Owns(&gatewayv1.HTTPRoute{})
	}
	return b.Complete(r)
}

// agentsForControlServiceAccount re-enqueues the agents of every profile in the SA's
// namespace that names it as controlServiceAccountName (so ControlServiceAccountResolved
// flips as soon as the SA appears or disappears).
func (r *AgentReconciler) agentsForControlServiceAccount(ctx context.Context, obj client.Object) []reconcile.Request {
	var profiles achv1alpha1.AgentProfileList
	if err := r.List(ctx, &profiles, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for i := range profiles.Items {
		if profiles.Items[i].Spec.Control.ServiceAccountName == obj.GetName() {
			reqs = append(reqs, r.agentsForProfile(ctx, &profiles.Items[i])...)
		}
	}
	return reqs
}

func (r *AgentReconciler) agentsForProfile(ctx context.Context, obj client.Object) []reconcile.Request {
	var list achv1alpha1.AgentList
	if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for i := range list.Items {
		if list.Items[i].Spec.ProfileRef.Name == obj.GetName() {
			reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: list.Items[i].Namespace, Name: list.Items[i].Name}})
		}
	}
	return reqs
}

// agentsForSecret enqueues every Agent that references a changed Secret: by identity, by
// an AgentProfile/Agent channel reference, or — only when the Secret is the operator's
// configured global storage credentials Secret AND the agent's successfully resolved Render
// config actually requires storage — the shared namespace-local credential Secret (Task2 D1
// amendment, Step 6). The Render call here is best-effort: a transient render error simply
// does not count as a match, it never blocks the identity/channel matches above.
func (r *AgentReconciler) agentsForSecret(ctx context.Context, obj client.Object) []reconcile.Request {
	var list achv1alpha1.AgentList
	if err := r.List(ctx, &list, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	profiles := map[string]achv1alpha1.AgentProfile{}
	var profileList achv1alpha1.AgentProfileList
	if err := r.List(ctx, &profileList, client.InNamespace(obj.GetNamespace())); err == nil {
		for i := range profileList.Items {
			profiles[profileList.Items[i].Name] = profileList.Items[i]
		}
	}
	name := obj.GetName()
	isGlobalStorageCredSecret := r.Storage.CredentialsSecretName != "" && name == r.Storage.CredentialsSecretName
	var reqs []reconcile.Request
	for i := range list.Items {
		a := &list.Items[i]
		hit := name == workspaceKeySecretName(string(a.UID))
		if !hit {
			if _, ok := render.ReferencedSecrets(profiles[a.Spec.ProfileRef.Name], *a)[name]; ok {
				hit = true
			}
		}
		if !hit && isGlobalStorageCredSecret {
			if cfg, _, err := render.Render(profiles[a.Spec.ProfileRef.Name], *a); err == nil && render.RuntimeRequiresStorage(cfg) {
				hit = true
			}
		}
		if hit {
			reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: a.Namespace, Name: a.Name}})
		}
	}
	return reqs
}
