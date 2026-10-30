// SPDX-License-Identifier: MIT

package controller

import (
	"crypto/sha256"
	"encoding/hex"
	"maps"
	"strings"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/api/resource"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/util/intstr"
	"sigs.k8s.io/controller-runtime/pkg/client"
	gatewayv1 "sigs.k8s.io/gateway-api/apis/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

const (
	agentContainerName = "agent"
	configVolumeName   = "ach-agent-config"
	configMountDir     = "/etc/ach-runtime"
	configFileName     = "config.json"
	configFilePath     = configMountDir + "/" + configFileName
	pvcVolumeName      = "ach-agent-state"
	// tmpVolumeName/tmpMountDir: the control process's single writable path under a
	// read-only root filesystem (ach-runtime: TMPDIR only; handoff/script children inherit it).
	tmpVolumeName = "ach-tmp"
	tmpMountDir   = "/tmp"
	// achTokenVolumeName holds a tokenExchange control pod's projected ServiceAccount token
	// (render.AchTokenPath); the kubelet renews it.
	achTokenVolumeName        = "ach-token"
	achTokenExpirationSeconds = int64(3600)
	configHashAnnotation      = "ach-agent.ackstorm.ai/config-hash"
	agentLabelKey             = "ach-agent.ackstorm.ai/agent"
	componentLabelKey         = "ach-agent.ackstorm.ai/component"
	componentControl          = "control"
	defaultGraceSeconds       = int64(120)
	agentUID                  = int64(10001)
	controlServicePort        = 8081

	// controlProbePort is the fixed control HTTP port: the container's health and event
	// server binds it, the probes and the -http Service target it. controlServicePort carries
	// the host API and the facades.
	controlProbePort = int32(8080)

	// ambientLabel puts the control pod in the Istio ambient mesh: the sandbox facade calls
	// and the operator's host API calls rely on mesh identity.
	ambientLabel = "istio.io/dataplane-mode"
)

// controlCommand is the fixed workspace-v1 control launch command (contract §11): the
// control pod's harness role, never an unverified arbitrary image entrypoint and never the
// legacy distributed-placement "--role" args convention.
var controlCommand = []string{"python", "-m", "ach_runtime", "control", "--config", configFilePath}

// effectiveControlServiceAccountName is what the control pod runs as: the profile's
// control.serviceAccountName when set, else ach-agent-control-<uid>.
func effectiveControlServiceAccountName(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) string {
	return render.ControlServiceAccountName(string(a.UID), p)
}

var (
	defaultCPURequest    = resource.MustParse("100m")
	defaultMemoryRequest = resource.MustParse("128Mi")
	defaultCPULimit      = resource.MustParse("1")
	defaultMemoryLimit   = resource.MustParse("1Gi")
)

// Child names derive from render.ControlName, which is bounded and DNS-safe.
func configMapName(agentName string) string   { return render.ControlName(agentName) + "-config" }
func pvcName(agentName string) string         { return render.ControlName(agentName) + "-state" }
func httpServiceName(agentName string) string { return render.ControlName(agentName) + "-http" }

func agentLabels(a *achv1alpha1.Agent) map[string]string {
	return map[string]string{
		"app.kubernetes.io/name":       "ach-agent",
		"app.kubernetes.io/managed-by": "ach-agent-operator",
		agentLabelKey:                  a.Name,
	}
}

// agentSelectorLabels selects the agent's CONTROL pod. The component label is required:
// the runtime also labels workspace pods ach-agent.ackstorm.ai/agent=<name>, and they must never
// back the control/expose Services
func agentSelectorLabels(agentName string) map[string]string {
	return map[string]string{agentLabelKey: agentName, componentLabelKey: componentControl}
}

// computeConfigHash digests every pod-template input so any change rolls the pod. secretHash is
// a salted HMAC of secret .Data computed by the reconciler (never plaintext here).
func computeConfigHash(configJSON, envJSON []byte, image, secretHash string) string {
	h := sha256.New()
	h.Write(configJSON)
	h.Write(envJSON)
	h.Write([]byte(image))
	h.Write([]byte(secretHash))
	return hex.EncodeToString(h.Sum(nil))[:16]
}

// buildAgentEnv is the SINGLE source of the agent container env. The ek is a secretKeyRef
// (never inline); profile env is merged with agent env and reserved ACH_* names are dropped.
// overrideEnv returns base with each entry in overrides replacing any existing entry of the
// same Name (global authority — the reconciler uses this to let the operator's runtime
// storage env win over anything buildAgentEnv already set at the same name), appending any
// override name not already present.
func overrideEnv(base, overrides []corev1.EnvVar) []corev1.EnvVar {
	idx := make(map[string]int, len(base))
	for i, e := range base {
		idx[e.Name] = i
	}
	out := append([]corev1.EnvVar(nil), base...)
	for _, o := range overrides {
		if i, ok := idx[o.Name]; ok {
			out[i] = o
			continue
		}
		out = append(out, o)
	}
	return out
}

// envFromBinding maps a render.EnvBinding onto a pod env var.
func envFromBinding(b render.EnvBinding) corev1.EnvVar {
	if b.SecretKeyRef != nil {
		return corev1.EnvVar{Name: b.Name, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: b.SecretKeyRef.Name},
			Key:                  b.SecretKeyRef.Key,
		}}}
	}
	e := corev1.EnvVar{Name: b.Name}
	if b.Value != nil {
		e.Value = *b.Value
	}
	return e
}

func buildAgentEnv(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile, bindings []render.EnvBinding) []corev1.EnvVar {
	env := []corev1.EnvVar{
		{Name: "ACH_CONFIG_PATH", Value: configFilePath},
		// POD_NAMESPACE feeds the ach-memory project slug ({POD_NAMESPACE}-{agent.name}).
		// Without it the harness silently degrades to the bare agent name — the same bank
		// under a different key, with no error anywhere.
		{Name: "POD_NAMESPACE", ValueFrom: &corev1.EnvVarSource{FieldRef: &corev1.ObjectFieldSelector{FieldPath: "metadata.namespace"}}},
	}
	// Model, MCP and ACH key aliases the rendered config names.
	for _, b := range bindings {
		env = append(env, envFromBinding(b))
	}
	for _, e := range render.ResolveEnv(a.Spec.Env, p.Spec.Control.Env) {
		if strings.HasPrefix(e.Name, "ACH_") {
			continue // reserved — defense-in-depth behind the CEL marker
		}
		env = append(env, e)
	}
	// TMPDIR is pinned to the writable emptyDir: anywhere else is read-only.
	env = overrideEnv(env, []corev1.EnvVar{{Name: "TMPDIR", Value: tmpMountDir}})
	// Channel auth and generated handoff/script aliases are injected as env vars, NOT
	// mounted files: the agent runs same-uid as the harness and can read mounted
	// secret files, but not the harness process env (PR_SET_DUMPABLE=0). Value via
	// secretKeyRef only — never an inline literal in the PodSpec.
	for _, ref := range render.ChannelSecretEnv(*p, *a) {
		env = append(env, corev1.EnvVar{Name: ref.EnvName, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: ref.SecretName},
			Key:                  ref.Key,
		}}})
	}
	// ach-memory user-key secret (memory.achMemory.auth) — same env-not-file wiring.
	if ref := render.MemorySecretEnv(*a); ref != nil {
		env = append(env, corev1.EnvVar{Name: ref.EnvName, ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
			LocalObjectReference: corev1.LocalObjectReference{Name: ref.SecretName},
			Key:                  ref.Key,
		}}})
	}
	return env
}

// buildConfigMap carries config.json only (contract §11 scope reset — no broker/TLS
// material rides alongside it any more).
func buildConfigMap(a *achv1alpha1.Agent, configJSON []byte) *corev1.ConfigMap {
	return &corev1.ConfigMap{
		ObjectMeta: metav1.ObjectMeta{Name: configMapName(a.Name), Namespace: a.Namespace, Labels: agentLabels(a)},
		Data:       map[string]string{configFileName: string(configJSON)},
	}
}

// buildControlServiceAccount is the control pod's identity (ach-agent-control-<uid>). It has
// no Kubernetes access (no token, no Role): the operator owns the sandboxes. Not applied when
// the profile sets control.serviceAccountName (that SA is external).
func buildControlServiceAccount(a *achv1alpha1.Agent) *corev1.ServiceAccount {
	falseVal := false
	return &corev1.ServiceAccount{
		ObjectMeta:                   metav1.ObjectMeta{Name: render.ControlServiceAccountName(string(a.UID), &achv1alpha1.AgentProfile{}), Namespace: a.Namespace, Labels: agentLabels(a)},
		AutomountServiceAccountToken: &falseVal,
	}
}

// buildControlService is the control pod's headless governing Service (agent-<agent name>,
// port 8081) — infrastructure.facadeEndpoint is built from this exact DNS name by
// RenderInfrastructureV2. ClusterIP: None since the control StatefulSet has a single replica
// and callers address the Service name directly.
func buildControlService(a *achv1alpha1.Agent) *corev1.Service {
	name := render.ControlName(a.Name)
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: name, Namespace: a.Namespace, Labels: agentLabels(a)},
		Spec: corev1.ServiceSpec{
			ClusterIP: corev1.ClusterIPNone,
			// The host API and the inventory that control's readiness waits for go through this
			// Service, so its DNS record must exist before the pod is ready. The 8080 event
			// Service (buildService) stays readiness-gated.
			PublishNotReadyAddresses: true,
			Selector:                 agentSelectorLabels(a.Name),
			Ports:                    []corev1.ServicePort{{Name: "control", Port: controlServicePort, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt(controlServicePort)}},
		},
	}
}

func buildPVC(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile) (*corev1.PersistentVolumeClaim, error) {
	qty, err := resource.ParseQuantity(p.Spec.Control.Persistence.Size)
	if err != nil {
		return nil, err
	}
	var scName *string
	if p.Spec.Control.Persistence.StorageClassName != "" {
		scName = &p.Spec.Control.Persistence.StorageClassName
	}
	return &corev1.PersistentVolumeClaim{
		ObjectMeta: metav1.ObjectMeta{Name: pvcName(a.Name), Namespace: a.Namespace, Labels: agentLabels(a)},
		Spec: corev1.PersistentVolumeClaimSpec{
			AccessModes:      []corev1.PersistentVolumeAccessMode{corev1.ReadWriteOnce},
			Resources:        corev1.VolumeResourceRequirements{Requests: corev1.ResourceList{corev1.ResourceStorage: qty}},
			StorageClassName: scName,
		},
	}, nil
}

// buildService fronts the pod on the fixed control port 8080.
func buildService(a *achv1alpha1.Agent, _ *achv1alpha1.AgentProfile) *corev1.Service {
	return &corev1.Service{
		ObjectMeta: metav1.ObjectMeta{Name: httpServiceName(a.Name), Namespace: a.Namespace, Labels: agentLabels(a)},
		Spec: corev1.ServiceSpec{
			Type:     corev1.ServiceTypeClusterIP,
			Selector: agentSelectorLabels(a.Name),
			Ports:    []corev1.ServicePort{{Name: "http", Port: 8080, Protocol: corev1.ProtocolTCP, TargetPort: intstr.FromInt(int(controlProbePort))}},
		},
	}
}

// needsService reports whether the operator creates the ClusterIP Service.
// Now an explicit opt-in (expose.service) rather than inferred from channel
// type: an inbound HTTP channel is only reachable when the author asks for a
// Service. Fully private by default.
func needsService(a *achv1alpha1.Agent) bool {
	return a.Spec.Expose != nil && a.Spec.Expose.Service
}

// buildStatefulSet builds the single-replica, single-container control StatefulSet (contract
// §11: "control one-container StatefulSet"). env is built once by the caller (buildAgentEnv)
// so what's hashed equals what's deployed. Inbound channel-auth secrets ride in env
// (secretKeyRef), never as mounted files.
//
// Per-Workspace execution StatefulSets are NOT built here: the operator's host loop
// (internal/host) creates them from the control unit's /host/v1 listing; this pod
// has no Kubernetes access.
// ServiceName is the control pod's own headless Service (buildControlService) — required by
// StatefulSet and also the exact DNS name RenderInfrastructureV2 embeds in facadeEndpoint.
func buildStatefulSet(a *achv1alpha1.Agent, p *achv1alpha1.AgentProfile, configHash string, env []corev1.EnvVar) *appsv1.StatefulSet {
	one := int32(1)
	falseVal, trueVal := false, true

	volumes := buildReservedInfraVolumes(a)
	mounts := buildReservedInfraMounts()

	if p.Spec.Control.Persistence != nil && p.Spec.Control.Persistence.Enabled {
		volumes = append(volumes, corev1.Volume{Name: pvcVolumeName, VolumeSource: corev1.VolumeSource{PersistentVolumeClaim: &corev1.PersistentVolumeClaimVolumeSource{ClaimName: pvcName(a.Name)}}})
		mounts = append(mounts, corev1.VolumeMount{Name: pvcVolumeName, MountPath: p.Spec.Control.Persistence.MountPath})
	}

	if render.TokenExchange(a) != nil {
		exp := achTokenExpirationSeconds
		volumes = append(volumes, corev1.Volume{Name: achTokenVolumeName, VolumeSource: corev1.VolumeSource{
			Projected: &corev1.ProjectedVolumeSource{Sources: []corev1.VolumeProjection{{
				ServiceAccountToken: &corev1.ServiceAccountTokenProjection{
					Audience: render.ExchangeAudience(a.Spec.Ach.BaseURL), ExpirationSeconds: &exp, Path: render.AchTokenFile,
				},
			}}},
		}})
		mounts = append(mounts, corev1.VolumeMount{Name: achTokenVolumeName, MountPath: render.AchTokenDir, ReadOnly: true})
	}

	grace := defaultGraceSeconds
	if p.Spec.Control.TerminationGracePeriodSeconds != nil {
		grace = *p.Spec.Control.TerminationGracePeriodSeconds
	}

	resources := corev1.ResourceRequirements{
		Requests: corev1.ResourceList{corev1.ResourceCPU: defaultCPURequest.DeepCopy(), corev1.ResourceMemory: defaultMemoryRequest.DeepCopy()},
		Limits:   corev1.ResourceList{corev1.ResourceCPU: defaultCPULimit.DeepCopy(), corev1.ResourceMemory: defaultMemoryLimit.DeepCopy()},
	}
	if p.Spec.Control.Resources != nil {
		resources = *p.Spec.Control.Resources.DeepCopy()
	}

	probe := func(path string) corev1.ProbeHandler {
		return corev1.ProbeHandler{HTTPGet: &corev1.HTTPGetAction{Path: path, Port: intstr.FromInt(int(controlProbePort))}}
	}
	// startupProbe budget tracks engine.startupTimeoutSeconds; liveness only arms after startup
	// succeeds, so a long hydration is not killed.
	startupFail := int32(30) // 30 * 5s = 150s
	if eng := render.ResolveEngine(a.Spec.Engine, p.Spec.Defaults.Engine); eng != nil && eng.StartupTimeoutSeconds != nil && *eng.StartupTimeoutSeconds > 0 {
		startupFail = int32(*eng.StartupTimeoutSeconds/5) + 1
	}

	podLabels := agentLabels(a)
	maps.Copy(podLabels, agentSelectorLabels(a.Name))
	podLabels[ambientLabel] = "ambient"

	// Pin uid/gid/fsGroup 10001: the image runs as that uid and a fresh cloud PVC (EBS,
	// root-owned 0755) is unwritable without fsGroup — found by ach-agent on a persistent
	// standalone pod (2026-09-15; kind's local-path provisioner hands out 0777 dirs and
	// never showed it).
	uid := agentUID
	podSC := &corev1.PodSecurityContext{
		RunAsNonRoot: &trueVal, RunAsUser: &uid, RunAsGroup: &uid, FSGroup: &uid,
		SeccompProfile: &corev1.SeccompProfile{Type: corev1.SeccompProfileTypeRuntimeDefault},
	}
	containers := []corev1.Container{{
		Name:    agentContainerName,
		Image:   p.Spec.Control.Image,
		Command: controlCommand,
		// The health port doubles as the harness /metrics port (same server as
		// /healthz + /readyz). Declared named so the PodMonitor
		// (monitoring.coreos.com/v1) can reference it by name for scraping.
		Ports: []corev1.ContainerPort{
			{Name: "http", ContainerPort: controlProbePort, Protocol: corev1.ProtocolTCP},
			{Name: "control", ContainerPort: controlServicePort, Protocol: corev1.ProtocolTCP},
		},
		Env:            env,
		VolumeMounts:   mounts,
		Resources:      resources,
		StartupProbe:   &corev1.Probe{ProbeHandler: probe("/healthz"), PeriodSeconds: 5, FailureThreshold: startupFail},
		ReadinessProbe: &corev1.Probe{ProbeHandler: probe("/readyz"), PeriodSeconds: 10, FailureThreshold: 3},
		LivenessProbe:  &corev1.Probe{ProbeHandler: probe("/healthz"), PeriodSeconds: 20, FailureThreshold: 3},
		SecurityContext: &corev1.SecurityContext{
			ReadOnlyRootFilesystem:   &trueVal,
			AllowPrivilegeEscalation: &falseVal,
			Capabilities:             &corev1.Capabilities{Drop: []corev1.Capability{"ALL"}},
		},
	}}

	sts := &appsv1.StatefulSet{
		// Name is the contract §11 control name (agent-<agent name>, render.ControlName), matching ServiceName
		// below verbatim.
		ObjectMeta: metav1.ObjectMeta{Name: render.ControlName(a.Name), Namespace: a.Namespace, Labels: agentLabels(a)},
		Spec: appsv1.StatefulSetSpec{
			Replicas:    &one,
			ServiceName: render.ControlName(a.Name),
			Selector:    &metav1.LabelSelector{MatchLabels: agentSelectorLabels(a.Name)},
			Template: corev1.PodTemplateSpec{
				ObjectMeta: metav1.ObjectMeta{
					Labels:      podLabels,
					Annotations: map[string]string{configHashAnnotation: configHash},
				},
				Spec: corev1.PodSpec{
					ServiceAccountName:           effectiveControlServiceAccountName(a, p),
					AutomountServiceAccountToken: &falseVal,
					// No service-link env: the control unit needs no Kubernetes API access,
					// and nothing else in the namespace belongs in its environment.
					EnableServiceLinks:            &falseVal,
					TerminationGracePeriodSeconds: &grace,
					ImagePullSecrets:              p.Spec.Control.ImagePullSecrets,
					NodeSelector:                  p.Spec.Control.NodeSelector,
					Tolerations:                   p.Spec.Control.Tolerations,
					SecurityContext:               podSC,
					Volumes:                       volumes,
					Containers:                    containers,
				},
			},
		},
	}

	return sts
}

// buildReservedInfraVolumes/buildReservedInfraMounts are the single source of truth for the
// fixed config plumbing (contract §11 scope reset — broker-token/leaf-TLS plumbing is gone,
// not deferred) — so there is exactly one place that knows their shape.
func buildReservedInfraVolumes(a *achv1alpha1.Agent) []corev1.Volume {
	return []corev1.Volume{
		{
			Name:         configVolumeName,
			VolumeSource: corev1.VolumeSource{ConfigMap: &corev1.ConfigMapVolumeSource{LocalObjectReference: corev1.LocalObjectReference{Name: configMapName(a.Name)}}},
		},
		{Name: tmpVolumeName, VolumeSource: corev1.VolumeSource{EmptyDir: &corev1.EmptyDirVolumeSource{}}},
	}
}

// buildReservedInfraMounts mounts the config ConfigMap as a single read-only DIRECTORY at
// configMountDir (no SubPath) — a SubPath mount never live-updates on a ConfigMap change,
// unlike a directory mount — plus the writable /tmp emptyDir the read-only root needs.
func buildReservedInfraMounts() []corev1.VolumeMount {
	return []corev1.VolumeMount{
		{Name: configVolumeName, MountPath: configMountDir, ReadOnly: true},
		{Name: tmpVolumeName, MountPath: tmpMountDir},
	}
}

// copySpec copies desired's mutable fields onto the fetched existing inside CreateOrUpdate
// (never the whole object — that would clobber resourceVersion / immutable fields).
func copySpec(existing, desired client.Object) {
	switch e := existing.(type) {
	case *corev1.ConfigMap:
		d := desired.(*corev1.ConfigMap)
		e.Labels, e.Data = d.Labels, d.Data
	case *corev1.ServiceAccount:
		d := desired.(*corev1.ServiceAccount)
		e.Labels, e.AutomountServiceAccountToken = d.Labels, d.AutomountServiceAccountToken
	case *appsv1.StatefulSet:
		d := desired.(*appsv1.StatefulSet)
		e.Labels = d.Labels
		if e.Spec.Selector == nil { // immutable — set only on create
			e.Spec.Selector = d.Spec.Selector
		}
		e.Spec.ServiceName = d.Spec.ServiceName // immutable post-create; same value every pass, harmless to resend
		e.Spec.Replicas = d.Spec.Replicas
		e.Spec.Template = d.Spec.Template
	case *gatewayv1.HTTPRoute:
		d := desired.(*gatewayv1.HTTPRoute)
		e.Labels, e.Spec = d.Labels, d.Spec
	case *corev1.Service:
		d := desired.(*corev1.Service)
		e.Labels = d.Labels
		e.Spec.Type = d.Spec.Type
		e.Spec.Selector = d.Spec.Selector
		e.Spec.PublishNotReadyAddresses = d.Spec.PublishNotReadyAddresses
		if len(e.Spec.Ports) != len(d.Spec.Ports) {
			e.Spec.Ports = d.Spec.Ports
		} else {
			for i := range e.Spec.Ports {
				e.Spec.Ports[i].Name = d.Spec.Ports[i].Name
				e.Spec.Ports[i].Port = d.Spec.Ports[i].Port
				e.Spec.Ports[i].Protocol = d.Spec.Ports[i].Protocol
				e.Spec.Ports[i].TargetPort = d.Spec.Ports[i].TargetPort
			}
		}
	}
}
