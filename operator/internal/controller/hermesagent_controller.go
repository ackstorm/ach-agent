// SPDX-License-Identifier: Apache-2.0

package controller

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"maps"
	"slices"
	"time"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/api/meta"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/apimachinery/pkg/util/intstr"
	ctrl "sigs.k8s.io/controller-runtime"
	"sigs.k8s.io/controller-runtime/pkg/client"
	"sigs.k8s.io/controller-runtime/pkg/controller/controllerutil"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/openshell"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=hermesagents,verbs=get;list;watch;update;patch
// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=hermesagents/finalizers,verbs=update
// +kubebuilder:rbac:groups=ach-agent.ackstorm.ai,resources=hermesagents/status,verbs=get;update;patch

const (
	hermesOwnerKind    = "HermesAgent"
	hermesLabelKey     = "ach-agent.ackstorm.ai/hermes-agent"
	componentProxy     = "credential-proxy"
	hermesProxyPort    = int32(controlServicePort)
	hermesProxyBearer  = "ACH_PROXY_TOKEN"
	hermesUserHome     = "/sandbox/ach/home"
	condHermesReady    = "Ready"
	hermesRequeueAfter = 30 * time.Second
)

// HermesAgentReconciler runs each HermesAgent as a credential-proxy StatefulSet plus one
// OpenShell sandbox. Gateway sandboxes raise no Kubernetes events, so it requeues on a timer.
type HermesAgentReconciler struct {
	Client           client.Client
	APIReader        client.Reader
	Scheme           *runtime.Scheme
	Sandboxes        *openshell.OpenShell
	SandboxNamespace string
}

func hermesProxyName(h *achv1alpha1.HermesAgent) string { return "hermes-" + h.Name }

func hermesLabels(h *achv1alpha1.HermesAgent) map[string]string {
	return map[string]string{hermesLabelKey: h.Name, componentLabelKey: componentProxy}
}

func (r *HermesAgentReconciler) Reconcile(ctx context.Context, req ctrl.Request) (ctrl.Result, error) {
	var h achv1alpha1.HermesAgent
	if err := r.Client.Get(ctx, req.NamespacedName, &h); err != nil {
		return ctrl.Result{}, client.IgnoreNotFound(err)
	}
	if !h.DeletionTimestamp.IsZero() {
		return r.finalize(ctx, &h)
	}
	if controllerutil.AddFinalizer(&h, openshell.SandboxFinalizer) {
		return ctrl.Result{Requeue: true}, r.Client.Update(ctx, &h)
	}

	if err := ensureKeySecret(ctx, r.Client, r.APIReader, r.Scheme, &h, hermesOwnerKind, hermesLabels(&h)); err != nil {
		return r.status(ctx, &h, "KeySecretFailed", err.Error())
	}
	userConfig, err := hermesUserConfig(&h)
	if err != nil {
		return r.status(ctx, &h, "InvalidConfig", err.Error())
	}
	env, err := r.sandboxEnv(ctx, &h)
	if err != nil {
		return r.status(ctx, &h, "SecretsUnresolved", err.Error())
	}
	sts, err := r.applyProxy(ctx, &h, userConfig)
	if err != nil {
		return ctrl.Result{}, err
	}
	sum := sha256.New()
	// The bundle is fetched at sandbox start: a change to what it renders from replaces the sandbox.
	_ = json.NewEncoder(sum).Encode([]any{env, h.Spec.Sandbox.OpenShell, h.Spec.Ach, h.Spec.Config})
	ready, phase, err := r.Sandboxes.EnsureHermes(ctx, r.APIReader, r.Client, openshell.HermesSandbox{
		Owner: client.ObjectKeyFromObject(&h), UID: h.UID, OpenShell: h.Spec.Sandbox.OpenShell,
		Volume: h.Spec.Persistence.Volume, Env: env,
		ProxyHost: hermesProxyHost(&h), ProxyPort: uint32(hermesProxyPort),
		Hash: hex.EncodeToString(sum.Sum(nil))[:16],
	})
	switch {
	case errors.Is(err, openshell.ErrPolicyRejected), errors.Is(err, openshell.ErrResourcesUnsupported):
		return r.status(ctx, &h, "SandboxRefused", err.Error())
	case err != nil:
		return ctrl.Result{}, err
	case sts.Status.ReadyReplicas < 1:
		return r.status(ctx, &h, "ProxyNotReady", "credential proxy is not ready")
	case !ready:
		return r.status(ctx, &h, "SandboxNotReady", phase)
	}
	return r.status(ctx, &h, "Ready", "Hermes is running")
}

func hermesProxyHost(h *achv1alpha1.HermesAgent) string {
	return fmt.Sprintf("%s.%s.svc.cluster.local", hermesProxyName(h), h.Namespace)
}

// sandboxEnv is everything Hermes gets: its home, the proxy and its bearer, and every key of
// spec.secretRef. Secret values are part of the spec hash, so a rotated token replaces the sandbox.
func (r *HermesAgentReconciler) sandboxEnv(ctx context.Context, h *achv1alpha1.HermesAgent) (map[string]string, error) {
	env := map[string]string{}
	if ref := h.Spec.SecretRef; ref != nil {
		var s corev1.Secret
		if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: h.Namespace, Name: ref.Name}, &s); err != nil {
			return nil, fmt.Errorf("secretRef %s: %w", ref.Name, err)
		}
		for k, v := range s.Data {
			env[k] = string(v)
		}
	}
	var key corev1.Secret
	if err := r.APIReader.Get(ctx, types.NamespacedName{Namespace: h.Namespace, Name: workspaceKeySecretName(string(h.UID))}, &key); err != nil {
		return nil, err
	}
	maps.Copy(env, map[string]string{
		"HERMES_HOME":     openshell.HermesHome,
		"HOME":            hermesUserHome,
		"ACH_PROXY_URL":   fmt.Sprintf("http://%s:%d", hermesProxyHost(h), hermesProxyPort),
		hermesProxyBearer: string(key.Data[workspaceKeyDataKey]),
	})
	return env, nil
}

// hermesUserConfig is spec.config as compact JSON for the proxy, "{}" when unset.
func hermesUserConfig(h *achv1alpha1.HermesAgent) (string, error) {
	if h.Spec.Config == nil || len(h.Spec.Config.Raw) == 0 {
		return "{}", nil
	}
	var cfg bytes.Buffer
	if err := json.Compact(&cfg, h.Spec.Config.Raw); err != nil {
		return "", fmt.Errorf("spec.config: %w", err)
	}
	return cfg.String(), nil
}

func (r *HermesAgentReconciler) applyProxy(ctx context.Context, h *achv1alpha1.HermesAgent, userConfig string) (*appsv1.StatefulSet, error) {
	svc := &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: hermesProxyName(h), Namespace: h.Namespace, Labels: hermesLabels(h)},
		Spec: corev1.ServiceSpec{Selector: hermesLabels(h), Ports: []corev1.ServicePort{{
			Name: "http", Port: hermesProxyPort, TargetPort: intstr.FromInt32(hermesProxyPort),
		}}},
	}
	sts := buildHermesProxy(h, userConfig)
	for _, desired := range []client.Object{svc, sts} {
		existing := desired.DeepCopyObject().(client.Object)
		if _, err := controllerutil.CreateOrUpdate(ctx, r.Client, existing, func() error {
			copySpec(existing, desired)
			return controllerutil.SetControllerReference(h, existing, r.Scheme)
		}); err != nil {
			return nil, fmt.Errorf("apply %T %s: %w", desired, desired.GetName(), err)
		}
		if desired == sts {
			sts = existing.(*appsv1.StatefulSet)
		}
	}
	return sts, nil
}

func buildHermesProxy(h *achv1alpha1.HermesAgent, userConfig string) *appsv1.StatefulSet {
	one, uid := int32(1), agentUID
	falseVal, trueVal := false, true
	secretEnv := func(name, secret, key string) corev1.EnvVar {
		return corev1.EnvVar{Name: name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: secret}, Key: key}}}
	}
	env := []corev1.EnvVar{
		{Name: "ACH_BASE_URL", Value: h.Spec.Ach.BaseURL},
		{Name: "ACH_ENVIRONMENT", Value: h.Spec.Ach.Environment},
		{Name: "ACH_AGENT_NAME", Value: h.Name},
		{Name: "HERMES_MODEL", Value: h.Spec.Ach.Model.Name},
		{Name: "HERMES_CONFIG", Value: userConfig},
		secretEnv(hermesProxyBearer, workspaceKeySecretName(string(h.UID)), workspaceKeyDataKey),
	}
	volumes := []corev1.Volume{{Name: tmpVolumeName, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}}}
	mounts := []corev1.VolumeMount{{Name: tmpVolumeName, MountPath: tmpMountDir}}
	if x := h.Spec.Ach.KeyFrom.TokenExchange; x != nil {
		exp := achTokenExpirationSeconds
		env = append(env, corev1.EnvVar{Name: "ACH_EXCHANGE_IDENTITY", Value: x.Identity},
			corev1.EnvVar{Name: "ACH_EXCHANGE_TOKEN_PATH", Value: render.AchTokenPath})
		volumes = append(volumes, corev1.Volume{Name: achTokenVolumeName, VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
				ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
					Audience: render.ExchangeAudience(h.Spec.Ach.BaseURL), ExpirationSeconds: &exp, Path: render.AchTokenFile,
				},
			}}},
		}})
		mounts = append(mounts, corev1.VolumeMount{Name: achTokenVolumeName, MountPath: render.AchTokenDir, ReadOnly: true})
	} else if ref := h.Spec.Ach.KeyFrom.SecretKeyRef; ref != nil {
		env = append(env, secretEnv("ACH_KEY", ref.Name, ref.Key))
	}
	probe := &corev1.Probe{ProbeHandler: corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{
		Path: "/healthz", Port: intstr.FromInt32(hermesProxyPort)}}, PeriodSeconds: 5}
	podLabels := hermesLabels(h)
	podLabels[ambientLabel] = "ambient"
	return &appsv1.StatefulSet{
		ObjectMeta: metav1.ObjectMeta{Name: hermesProxyName(h), Namespace: h.Namespace, Labels: hermesLabels(h)},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    &one,
			ServiceName: hermesProxyName(h),
			Selector:    &metav1.LabelSelector{MatchLabels: hermesLabels(h)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{Labels: podLabels},
				Spec: corev1.PodSpec{
					ServiceAccountName:           h.Spec.ServiceAccountName,
					AutomountServiceAccountToken: &falseVal,
					EnableServiceLinks:           &falseVal,
					SecurityContext: &corev1.PodSecurityContext{
						RunAsNonRoot: &trueVal, RunAsUser: &uid, RunAsGroup: &uid,
						SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
					},
					Volumes: volumes,
					Containers: []corev1.Container{{
						Name:    componentProxy,
						Image:   h.Spec.Proxy.Image,
						Command: []string{"python", "-m", "ach_runtime", "credential-proxy", "--port", fmt.Sprint(hermesProxyPort)},
						Ports:   []corev1.ContainerPort{{Name: "http", ContainerPort: hermesProxyPort, Protocol: corev1.ProtocolTCP}},
						Env:     env, VolumeMounts: mounts,
						ReadinessProbe: probe, LivenessProbe: probe,
						Resources: corev1.ResourceRequirements{
							Requests: corev1.ResourceList{corev1.ResourceCPU: defaultCPURequest.DeepCopy(), corev1.ResourceMemory: defaultMemoryRequest.DeepCopy()},
							Limits:   corev1.ResourceList{corev1.ResourceCPU: defaultCPULimit.DeepCopy(), corev1.ResourceMemory: defaultMemoryLimit.DeepCopy()},
						},
						SecurityContext: &corev1.SecurityContext{
							ReadOnlyRootFilesystem: &trueVal, AllowPrivilegeEscalation: &falseVal,
							Capabilities: &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
						},
					}},
				},
			},
		},
	}
}

// finalize removes the sandbox through the gateway and the PVC (neither has an owner
// reference), then the finalizer; the proxy objects go by owner reference.
func (r *HermesAgentReconciler) finalize(ctx context.Context, h *achv1alpha1.HermesAgent) (ctrl.Result, error) {
	if !controllerutil.ContainsFinalizer(h, openshell.SandboxFinalizer) {
		return ctrl.Result{}, nil
	}
	sandboxLeft, err := r.Sandboxes.RemoveAgentSandboxes(ctx, r.APIReader, string(h.UID))
	if err != nil {
		return ctrl.Result{}, err
	}
	volumeLeft, err := removeVolumes(ctx, r.APIReader, r.Client, r.SandboxNamespace, string(h.UID))
	if err != nil {
		return ctrl.Result{}, err
	}
	if sandboxLeft || volumeLeft {
		return ctrl.Result{RequeueAfter: 5 * time.Second}, nil
	}
	controllerutil.RemoveFinalizer(h, openshell.SandboxFinalizer)
	return ctrl.Result{}, r.Client.Update(ctx, h)
}

func (r *HermesAgentReconciler) status(ctx context.Context, h *achv1alpha1.HermesAgent, reason, msg string) (ctrl.Result, error) {
	cond := metav1.Condition{Type: condHermesReady, Status: metav1.ConditionFalse, Reason: reason, Message: msg, ObservedGeneration: h.Generation}
	if reason == "Ready" {
		cond.Status = metav1.ConditionTrue
	}
	before := slices.Clone(h.Status.Conditions)
	meta.SetStatusCondition(&h.Status.Conditions, cond)
	if h.Status.ObservedGeneration != h.Generation || !conditionsEqual(before, h.Status.Conditions) {
		h.Status.ObservedGeneration = h.Generation
		if err := r.Client.Status().Update(ctx, h); err != nil && !apierrors.IsConflict(err) {
			return ctrl.Result{}, err
		}
	}
	return ctrl.Result{RequeueAfter: hermesRequeueAfter}, nil
}

func conditionsEqual(a, b []metav1.Condition) bool {
	return slices.EqualFunc(a, b, func(x, y metav1.Condition) bool {
		return x.Type == y.Type && x.Status == y.Status && x.Reason == y.Reason && x.Message == y.Message && x.ObservedGeneration == y.ObservedGeneration
	})
}

func (r *HermesAgentReconciler) SetupWithManager(mgr ctrl.Manager) error {
	return ctrl.NewControllerManagedBy(mgr).
		For(&achv1alpha1.HermesAgent{}).
		Owns(&appsv1.StatefulSet{}).
		Named("hermesagent").
		Complete(r)
}
