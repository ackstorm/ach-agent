// SPDX-License-Identifier: MIT

// Package controller — ACHAgentReconciler renders an ACHAgent (+ its AgentProfile) into the
// workspace-v2 ConfigMap and a single-replica control StatefulSet (runtime contract v2), plus
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

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
)

const achAgentOwnerKind = "ACHAgent"

const (
	condReady                  = "Ready"
	condProfileResolved        = "ProfileResolved"
	condIdentityResolved       = "IdentityResolved"
	condChannelSecretsResolved = "ChannelSecretsResolved"
	condWorkloadApplied        = "WorkloadApplied"
	condWorkloadReady          = "WorkloadReady"
	// condControlServiceAccountResolved is False when the profile's controlServiceAccountName
	// names a ServiceAccount that does not exist (the control pod could never start).
	condControlServiceAccountResolved = "ControlServiceAccountResolved"
)

var requiredConds = []string{condProfileResolved, condControlServiceAccountResolved, condIdentityResolved, condChannelSecretsResolved, condWorkloadApplied, condWorkloadReady}

// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=achagents,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=achagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=achagents/status,verbs=get;update;patch
// +kubebuilder:rbac:groups=ach.ackstorm.ai,resources=agentprofiles,verbs=get;list;watch
// +kubebuilder:rbac:groups=apps,resources=statefulsets,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=configmaps,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=services,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=serviceaccounts,verbs=get;list;watch;create;update;patch;delete
// +kubebuilder:rbac:groups="",resources=persistentvolumeclaims,verbs=get;list;watch;create;update;patch;delete
// identity/channel/runtime-storage-credential Secrets are read-only. The sole write is create
// for the UID-owned legacy workspace key Secret; existing keys are never updated or deleted.
// No Secret verbs are ever granted to the Harness or execution ServiceAccounts.
// +kubebuilder:rbac:groups="",resources=secrets,verbs=get;list;watch;create
// +kubebuilder:rbac:groups="",resources=pods,verbs=get;list;watch;delete

// ACHAgentReconciler reconciles ACHAgent objects. APIReader is the UNCACHED client used for
// Secret content reads (so Secret bodies are never cached in this shared binary).
type ACHAgentReconciler struct {
	client.Client
	APIReader client.Reader
	Scheme    *runtime.Scheme

	// DefaultAchBaseURL is the operator-level fallback ACH base URL (from
	// ACH_BASE_URL) used when neither ACHAgent.spec.ach nor AgentProfile.spec.achagent.ach
	// sets baseUrl. Empty + no per-object override => the agent is blocked (Render
	// errors) because it has no ACH to hydrate against.
	DefaultAchBaseURL string

	// RuntimeStorageOptions is the operator/chart-level global Storage backend config (root
	// D1), sourced from operator env/Helm (cmd/ach/cmd/operator.go) — never per-agent. An
	// empty Bucket means the backend is unconfigured: ValidateRuntimeStorage fails closed
	// (StorageUnavailable condition) only for an ACHAgent whose resolved config actually
	// requires persistence/artifacts; an all-disabled agent is unaffected.
	RuntimeStorageOptions render.RuntimeStorageOptions

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
func ensureExecutionFinalizer(a *achv1alpha1.ACHAgent) bool {
	return controllerutil.AddFinalizer(a, executionFinalizer)
}

// finalizeExecutionUnits deletes the agent's OpenShell sandboxes (they cannot carry an owner
// reference) and then lets the deletion proceed. Selection is by agent UID, so a newer
// ACHAgent with the same name is never touched. Gateway errors requeue with backoff.
func (r *ACHAgentReconciler) finalizeExecutionUnits(ctx context.Context, a *achv1alpha1.ACHAgent) (ctrl.Result, error) {
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

//nolint:gocyclo // Single linear resolve→render→apply→status flow; splitting scatters status ordering.
func (r *ACHAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	logger := log.FromContext(ctx).WithValues("achagent", req.NamespacedName)

	var agent achv1alpha1.ACHAgent
	if err := r.Get(ctx, req.NamespacedName, &agent); err != nil {
		if apierrors.IsNotFound(err) {
			return ctrl.Result{}, nil
		}
		return ctrl.Result{}, fmt.Errorf("get achagent: %w", err)
	}
	if !agent.DeletionTimestamp.IsZero() {
		return r.finalizeExecutionUnits(ctx, &agent)
	}
	logger.Info("reconciling achagent")

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
	setCond(&conds, condProfileResolved, metav1.ConditionTrue, "ProfileFound", "", agent.Generation)

	// The sandbox finalizer is added before anything can create a sandbox (the control pod
	// and so any host pass come later in this reconcile).
	if ensureExecutionFinalizer(&agent) {
		if err := r.Update(ctx, &agent); err != nil {
			return ctrl.Result{}, err
		}
	}

	// 1b. A profile-named control SA is external: it must already exist. Created later, the
	// ServiceAccount watch (agentsForControlServiceAccount) re-enqueues this agent.
	if name := profile.Spec.ControlServiceAccountName; name != "" {
		var sa corev1.ServiceAccount
		if err := r.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: name}, &sa); err != nil {
			if apierrors.IsNotFound(err) {
				setCond(&conds, condControlServiceAccountResolved, metav1.ConditionFalse, "ServiceAccountNotFound",
					fmt.Sprintf("ServiceAccount %q named by AgentProfile %q spec.controlServiceAccountName not found in namespace %q", name, profile.Name, agent.Namespace), agent.Generation)
				return r.finish(ctx, &agent, conds)
			}
			return ctrl.Result{}, fmt.Errorf("get control serviceaccount: %w", err)
		}
		setCond(&conds, condControlServiceAccountResolved, metav1.ConditionTrue, "ServiceAccountFound", "", agent.Generation)
	} else {
		setCond(&conds, condControlServiceAccountResolved, metav1.ConditionTrue, "PerAgentServiceAccount", "", agent.Generation)
	}

	// 2. Identity Secret + key (APIReader — uncached). Object-level CEL on ACHAgentSpec
	// requires spec.ach.identity on any NEWLY admitted object, but a CR stored before that
	// CEL rule was added is never retroactively validated — dereferencing Ach/Identity
	// without this guard panics the reconciler on such a stored legacy-shape object
	// (Important review finding 10). Surface it as an ordinary unresolved condition instead.
	if agent.Spec.Ach == nil || agent.Spec.Ach.Identity == nil {
		setCond(&conds, condIdentityResolved, metav1.ConditionFalse, "IdentityMissing", "spec.ach.identity is required — this object predates admission validation and must be updated", agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	var ekSecret corev1.Secret
	identity := agent.Spec.Ach.Identity
	if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: agent.Namespace, Name: identity.SecretRef.Name}, &ekSecret); err != nil {
		if apierrors.IsNotFound(err) {
			setCond(&conds, condIdentityResolved, metav1.ConditionFalse, "IdentitySecretNotFound", fmt.Sprintf("identity secret %q not found", identity.SecretRef.Name), agent.Generation)
			return r.finish(ctx, &agent, conds)
		}
		return ctrl.Result{}, fmt.Errorf("get identity secret: %w", err)
	}
	if _, ok := ekSecret.Data[identity.SecretRef.Key]; !ok {
		setCond(&conds, condIdentityResolved, metav1.ConditionFalse, "IdentityKeyMissing", fmt.Sprintf("secret %q has no key %q", identity.SecretRef.Name, identity.SecretRef.Key), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	setCond(&conds, condIdentityResolved, metav1.ConditionTrue, "IdentityFound", "", agent.Generation)

	// 3. Render (workspace-v2).
	cfg, err := render.Render2(profile, agent, r.DefaultAchBaseURL)
	if err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "RenderFailed", err.Error(), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	configJSON, err := json.Marshal(cfg)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("marshal config: %w", err)
	}

	// 4. Global runtime Storage: required configuration only, never cloud reachability (root
	// D1). All-disabled policies skip this dependency entirely (RuntimeRequiresStorage false).
	requiresStorage := render.RuntimeRequiresStorage(cfg)
	if err := render.ValidateRuntimeStorage(cfg, r.RuntimeStorageOptions); err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "StorageUnavailable", err.Error(), agent.Generation)
		return r.finish(ctx, &agent, conds)
	}

	// 5. Channel Secrets + keys, merged with the global storage credential Secret's required
	// keys when this agent's resolved config actually requires storage (Task2 D1 amendment).
	refSecrets := render.ReferencedSecrets(profile, agent)
	checkSecrets, hashSecretsMap := refSecrets, refSecrets
	if requiresStorage && r.RuntimeStorageOptions.CredentialsSecretName != "" {
		checkSecrets = mergeSecretKeys(refSecrets, r.RuntimeStorageOptions.CredentialsSecretName, requiredStorageCredentialKeys)
		hashSecretsMap = mergeSecretKeys(refSecrets, r.RuntimeStorageOptions.CredentialsSecretName, allStorageCredentialKeys)
	}
	if failReason, failMsg := r.checkChannelSecrets(ctx, agent.Namespace, checkSecrets); failReason != "" {
		setCond(&conds, condChannelSecretsResolved, metav1.ConditionFalse, failReason, failMsg, agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	setCond(&conds, condChannelSecretsResolved, metav1.ConditionTrue, "ChannelSecretsFound", "", agent.Generation)

	// The private workspace key is created only after render and authored Secret checks pass.
	// Errors are deliberately sanitized so the key can never appear in status or logs.
	if err := r.ensureWorkspaceKey(ctx, &agent); err != nil {
		setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "WorkspaceKeyUnavailable", "workspace key Secret is unavailable or invalid", agent.Generation)
		return r.finish(ctx, &agent, conds)
	}
	hashSecretsMap = mergeSecretKeys(hashSecretsMap, workspaceKeySecretName(string(agent.UID)), []string{workspaceKeyDataKey})

	// 6. Env (once, with the private storage env merged in when required) + salted secret
	// hash + config hash.
	env := buildAgentEnv(&agent, &profile, r.DefaultAchBaseURL)
	env = overrideEnv(env, []corev1.EnvVar{{Name: "ACH_SANDBOX_KEY", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: workspaceKeySecretName(string(agent.UID))}, Key: workspaceKeyDataKey,
	}}}})
	if requiresStorage {
		env = overrideEnv(env, render.RuntimeStorageEnv(r.RuntimeStorageOptions))
	}
	envJSON, err := json.Marshal(env)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("marshal env: %w", err)
	}
	secretHash, err := r.hashSecrets(ctx, &agent, hashSecretsMap)
	if err != nil {
		return ctrl.Result{}, fmt.Errorf("hash secrets: %w", err)
	}
	resolvedImage := render.ResolveImage(agent.Spec.Image, profile.Spec.Achagent.Image)
	configHash := computeConfigHash(configJSON, envJSON, resolvedImage, secretHash)

	sts := buildStatefulSet(&agent, &profile, configHash, env)
	// 7. Apply children.
	if err := r.apply(ctx, &agent, buildConfigMap(&agent, configJSON)); err != nil {
		return r.applyFail(ctx, &agent, conds, "ConfigMap", err)
	}
	// A profile-named control SA is pre-existing and external: never created/owned here.
	if profile.Spec.ControlServiceAccountName == "" {
		if err := r.apply(ctx, &agent, buildControlServiceAccount(&agent)); err != nil {
			return r.applyFail(ctx, &agent, conds, "ServiceAccount", err)
		}
	}
	if err := r.apply(ctx, &agent, buildControlService(&agent)); err != nil {
		return r.applyFail(ctx, &agent, conds, "Service", err)
	}
	if profile.Spec.Persistence != nil && profile.Spec.Persistence.Enabled {
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
	} else if err := r.prune(ctx, &corev1.Service{ObjectMeta: metav1.ObjectMeta{Name: agentResourceName(agent.Name), Namespace: agent.Namespace}}); err != nil {
		// Converge expose.service true→false (and pre-feat orphans): owner-ref GC
		// only fires on ACHAgent delete, not when the owner stops desiring the child.
		return r.applyFail(ctx, &agent, conds, "Service", err)
	}
	setCond(&conds, condWorkloadApplied, metav1.ConditionTrue, "WorkloadApplied", "", agent.Generation)

	// 8. WorkloadReady from pod.status (probe-backed).
	r.deriveWorkloadReady(ctx, &agent, configHash, &conds)

	// 9. Ready is aggregated in finish() every pass.
	return r.finish(ctx, &agent, conds)
}

// requiredStorageCredentialKeys/allStorageCredentialKeys are the AWS SecretKeyRef key names
// RuntimeStorageEnv wires into the global credentials Secret: the first two are required
// (merged into the checkChannelSecrets gate), the third (session token) is optional and
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

func (r *ACHAgentReconciler) checkChannelSecrets(ctx context.Context, ns string, refSecrets map[string][]string) (reason, msg string) {
	names := slices.Sorted(maps.Keys(refSecrets))
	for _, name := range names {
		var s corev1.Secret
		if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: ns, Name: name}, &s); err != nil {
			if apierrors.IsNotFound(err) {
				return "ChannelSecretNotFound", fmt.Sprintf("channel secret %q not found", name)
			}
			// Transient — surface as a non-terminal error via the caller returning it.
			return "ChannelSecretError", fmt.Sprintf("get channel secret %q: %v", name, err)
		}
		for _, key := range refSecrets[name] {
			if _, ok := s.Data[key]; !ok {
				return "ChannelSecretKeyMissing", fmt.Sprintf("secret %q has no key %q", name, key)
			}
		}
	}
	return "", ""
}

// apply upserts a child with an owner ref (GC on ACHAgent delete).
func (r *ACHAgentReconciler) apply(ctx context.Context, owner *achv1alpha1.ACHAgent, desired client.Object) error {
	obj := desired.DeepCopyObject().(client.Object)
	_, err := controllerutil.CreateOrUpdate(ctx, r.Client, obj, func() error {
		copySpec(obj, desired)
		return controllerutil.SetControllerReference(owner, obj, r.Scheme)
	})
	return err
}

// prune deletes a child the agent no longer desires (expose.service false). Owner-ref GC only fires on ACHAgent delete,
// so convergence needs an explicit delete. Idempotent — NotFound is a no-op.
func (r *ACHAgentReconciler) prune(ctx context.Context, obj client.Object) error {
	if err := r.Delete(ctx, obj); err != nil && !apierrors.IsNotFound(err) {
		return err
	}
	return nil
}

// applyPVC creates the PVC once. When retainPolicy=Retain the PVC gets NO owner-ref, so it
// survives ACHAgent deletion (create-once; PVC spec is immutable). A changed size is logged,
// not auto-applied.
func (r *ACHAgentReconciler) applyPVC(ctx context.Context, owner *achv1alpha1.ACHAgent, p *achv1alpha1.AgentProfile) error {
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
	retain := p.Spec.Persistence.RetainPolicy != nil && *p.Spec.Persistence.RetainPolicy == "Retain"
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
func (r *ACHAgentReconciler) replaceStaleControlPod(ctx context.Context, a *achv1alpha1.ACHAgent) error {
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

func (r *ACHAgentReconciler) applyFail(ctx context.Context, a *achv1alpha1.ACHAgent, conds []metav1.Condition, what string, err error) (ctrl.Result, error) {
	setCond(&conds, condWorkloadApplied, metav1.ConditionFalse, "ApplyFailed", fmt.Sprintf("apply %s: %v", what, err), a.Generation)
	return r.finish(ctx, a, conds)
}

func (r *ACHAgentReconciler) deriveWorkloadReady(ctx context.Context, a *achv1alpha1.ACHAgent, configHash string, conds *[]metav1.Condition) {
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

// hashSecrets returns a salted (HMAC keyed by the ACHAgent UID) digest over the .Data of the
// identity secret + referenced channel-secret keys. A content rotation rolls the pod; the digest
// is not reversible to the token values. Reads via APIReader (uncached).
func (r *ACHAgentReconciler) hashSecrets(ctx context.Context, a *achv1alpha1.ACHAgent, refSecrets map[string][]string) (string, error) {
	// name → keys to hash (identity secret's key + each channel secret's referenced keys).
	want := map[string]map[string]struct{}{}
	add := func(name, key string) {
		if want[name] == nil {
			want[name] = map[string]struct{}{}
		}
		want[name][key] = struct{}{}
	}
	add(a.Spec.Ach.Identity.SecretRef.Name, a.Spec.Ach.Identity.SecretRef.Key)
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
func (r *ACHAgentReconciler) finish(ctx context.Context, a *achv1alpha1.ACHAgent, conds []metav1.Condition) (ctrl.Result, error) {
	for _, t := range requiredConds {
		if apimeta.FindStatusCondition(conds, t) == nil {
			setCond(&conds, t, metav1.ConditionUnknown, "NotEvaluated", "reconcile bailed before evaluating this condition", a.Generation)
		}
	}
	var fresh achv1alpha1.ACHAgent
	if err := r.Get(ctx, types.NamespacedName{Namespace: a.Namespace, Name: a.Name}, &fresh); err != nil {
		return ctrl.Result{}, err
	}
	for _, c := range conds {
		setCond(&fresh.Status.Conditions, c.Type, c.Status, c.Reason, c.Message, c.ObservedGeneration)
	}
	if allTrue(fresh.Status.Conditions, requiredConds...) {
		setCond(&fresh.Status.Conditions, condReady, metav1.ConditionTrue, "Ready", "", a.Generation)
	} else {
		reason, msg := "NotReady", "one or more required conditions are not satisfied"
		for _, t := range requiredConds {
			if c := apimeta.FindStatusCondition(fresh.Status.Conditions, t); c == nil || c.Status != metav1.ConditionTrue {
				if c != nil {
					reason, msg = c.Reason, c.Message
				}
				break
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

func (r *ACHAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&achv1alpha1.ACHAgent{}).
		Owns(&appsv1.StatefulSet{}).
		Owns(&corev1.ConfigMap{}).
		Owns(&corev1.Service{}).
		Owns(&corev1.PersistentVolumeClaim{}).
		Owns(&corev1.ServiceAccount{}).
		Watches(&achv1alpha1.AgentProfile{}, handler.EnqueueRequestsFromMapFunc(r.agentsForProfile)).
		Watches(&corev1.ServiceAccount{}, handler.EnqueueRequestsFromMapFunc(r.agentsForControlServiceAccount)).
		Watches(&corev1.Secret{}, handler.EnqueueRequestsFromMapFunc(r.agentsForSecret), builder.OnlyMetadata).
		Named("achagent").
		Complete(r)
}

// agentsForControlServiceAccount re-enqueues the agents of every profile in the SA's
// namespace that names it as controlServiceAccountName (so ControlServiceAccountResolved
// flips as soon as the SA appears or disappears).
func (r *ACHAgentReconciler) agentsForControlServiceAccount(ctx context.Context, obj client.Object) []reconcile.Request {
	var profiles achv1alpha1.AgentProfileList
	if err := r.List(ctx, &profiles, client.InNamespace(obj.GetNamespace())); err != nil {
		return nil
	}
	var reqs []reconcile.Request
	for i := range profiles.Items {
		if profiles.Items[i].Spec.ControlServiceAccountName == obj.GetName() {
			reqs = append(reqs, r.agentsForProfile(ctx, &profiles.Items[i])...)
		}
	}
	return reqs
}

func (r *ACHAgentReconciler) agentsForProfile(ctx context.Context, obj client.Object) []reconcile.Request {
	var list achv1alpha1.ACHAgentList
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

// agentsForSecret enqueues every ACHAgent that references a changed Secret: by identity, by
// an AgentProfile/ACHAgent channel reference, or — only when the Secret is the operator's
// configured global storage credentials Secret AND the agent's successfully resolved Render2
// config actually requires storage — the shared namespace-local credential Secret (Task2 D1
// amendment, Step 6). The Render2 call here is best-effort: a transient render error simply
// does not count as a match, it never blocks the identity/channel matches above.
func (r *ACHAgentReconciler) agentsForSecret(ctx context.Context, obj client.Object) []reconcile.Request {
	var list achv1alpha1.ACHAgentList
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
	isGlobalStorageCredSecret := r.RuntimeStorageOptions.CredentialsSecretName != "" && name == r.RuntimeStorageOptions.CredentialsSecretName
	var reqs []reconcile.Request
	for i := range list.Items {
		a := &list.Items[i]
		hit := name == workspaceKeySecretName(string(a.UID)) || (a.Spec.Ach != nil && a.Spec.Ach.Identity != nil && a.Spec.Ach.Identity.SecretRef.Name == name)
		if !hit {
			if _, ok := render.ReferencedSecrets(profiles[a.Spec.ProfileRef.Name], *a)[name]; ok {
				hit = true
			}
		}
		if !hit && isGlobalStorageCredSecret {
			if cfg, err := render.Render2(profiles[a.Spec.ProfileRef.Name], *a, r.DefaultAchBaseURL); err == nil && render.RuntimeRequiresStorage(cfg) {
				hit = true
			}
		}
		if hit {
			reqs = append(reqs, reconcile.Request{NamespacedName: types.NamespacedName{Namespace: a.Namespace, Name: a.Name}})
		}
	}
	return reqs
}
