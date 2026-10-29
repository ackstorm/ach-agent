// SPDX-License-Identifier: MIT

package controller

import (
	"strings"
	"testing"

	appsv1 "k8s.io/api/apps/v1"
	corev1 "k8s.io/api/core/v1"
	"k8s.io/apimachinery/pkg/labels"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
	"github.com/ackstorm/ach-agent/operator/internal/render"
)

func mkEnv(name, val string) []corev1.EnvVar { return []corev1.EnvVar{{Name: name, Value: val}} }

func TestComputeConfigHash_ChangesWithInputs(t *testing.T) {
	base := computeConfigHash([]byte(`{"a":1}`), []byte(`[]`), "img:1", "sec1")
	if len(base) != 16 {
		t.Fatalf("hash len = %d", len(base))
	}
	for name, h := range map[string]string{
		"config": computeConfigHash([]byte(`{"a":2}`), []byte(`[]`), "img:1", "sec1"),
		"env":    computeConfigHash([]byte(`{"a":1}`), []byte(`[{}]`), "img:1", "sec1"),
		"image":  computeConfigHash([]byte(`{"a":1}`), []byte(`[]`), "img:2", "sec1"),
		"secret": computeConfigHash([]byte(`{"a":1}`), []byte(`[]`), "img:1", "sec2"),
	} {
		if h == base {
			t.Errorf("%s change did not alter hash", name)
		}
	}
}

func TestBuildAgentEnv_RejectsReservedEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name = "d"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Env = mkEnv("ACH_TOKEN", "ek_LEAK") // reserved — must be dropped
	for _, e := range buildAgentEnv(a, p, nil) {
		if e.Name == "ACH_TOKEN" && e.Value == "ek_LEAK" {
			t.Fatal("reserved ACH_* env leaked into the pod spec")
		}
	}
}

func TestBuildAgentEnv_AgentOverridesProfileEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Spec.Env = mkEnv("SHARED", "agent")
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Env = append(mkEnv("PROFILE", "p"), mkEnv("SHARED", "profile")...)

	values := map[string]string{}
	for _, e := range buildAgentEnv(a, p, nil) {
		values[e.Name] = e.Value
	}
	if values["PROFILE"] != "p" || values["SHARED"] != "agent" {
		t.Fatalf("merged Pod env = %v", values)
	}
}

func TestBuildDeployment_MountsConfigProbesAndHash(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "ghcr.io/ackstorm/ach-agent:latest"

	env := buildAgentEnv(a, p, nil)
	dep := buildStatefulSet(a, p, "cfghash", env)

	if *dep.Spec.Replicas != 1 {
		t.Errorf("replicas = %d", *dep.Spec.Replicas)
	}
	c := dep.Spec.Template.Spec.Containers[0]
	if c.Image != p.Spec.Control.Image {
		t.Errorf("image = %q", c.Image)
	}
	if c.ReadinessProbe == nil || c.ReadinessProbe.HTTPGet == nil || c.ReadinessProbe.HTTPGet.Path != "/readyz" {
		t.Error("readinessProbe /readyz missing")
	}
	if c.LivenessProbe == nil || c.LivenessProbe.HTTPGet.Path != "/healthz" {
		t.Error("livenessProbe /healthz missing")
	}
	if c.StartupProbe == nil {
		t.Error("startupProbe missing")
	}
	// Named control port (fixed 8080, workspace-v1 contract §11) — the PodMonitor scrapes
	// /metrics on it BY NAME, so a rename here silently breaks agent metrics collection.
	var hp *corev1.ContainerPort
	for i := range c.Ports {
		if c.Ports[i].Name == "http" {
			hp = &c.Ports[i]
		}
	}
	if hp == nil || hp.ContainerPort != 8080 {
		t.Errorf("container must declare named http port 8080, got %+v", c.Ports)
	}
	// config.json is served by the directory mount at /etc/ach-runtime (alongside ca.crt),
	// not a SubPath file mount (Important review finding 9: "implement the specified
	// directory projection").
	found := false
	for _, mnt := range c.VolumeMounts {
		if mnt.MountPath == "/etc/ach-runtime" && mnt.SubPath == "" {
			found = true
		}
	}
	if !found {
		t.Error("config.json subPath mount missing")
	}
	if dep.Spec.Template.Annotations["ach-agent.ackstorm.ai/config-hash"] != "cfghash" {
		t.Error("config-hash annotation missing")
	}
}

func TestBuildAgentEnv_ChannelSecretInjectedAsEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	a.Spec.Channels = []achv1alpha1.ChannelSpec{{
		Name: "gitlab-mr-review", Type: "webhook",
		Webhook: &achv1alpha1.WebhookSpec{Auth: achv1alpha1.WebhookAuthSpec{Type: "gitlab_token", SecretRef: &achv1alpha1.SecretKeyRef{Name: "gl-hook", Key: "secret"}}},
	}}
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"

	// Auth secret must be a secretKeyRef env var, never an inline value.
	var found bool
	for _, e := range buildAgentEnv(a, p, nil) {
		if e.Name != "ACH_SECRET_GITLAB_MR_REVIEW_WEBHOOK" {
			continue
		}
		found = true
		if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil ||
			e.ValueFrom.SecretKeyRef.Name != "gl-hook" || e.ValueFrom.SecretKeyRef.Key != "secret" {
			t.Errorf("channel secret env must be a secretKeyRef to gl-hook/secret, got %+v", e)
		}
	}
	if !found {
		t.Fatal("channel auth secret not injected as env var")
	}

	// And it must NOT be mounted as a file (fsGroup exists for the PVC, not for secrets).
	dep := buildStatefulSet(a, p, "h", buildAgentEnv(a, p, nil))
	for _, v := range dep.Spec.Template.Spec.Volumes {
		if v.Secret != nil {
			t.Errorf("secret volume present; auth secrets are env-injected now: %+v", v)
		}
	}
}

func TestBuildAgentEnv_HandoffSecretGetsGeneratedAlias(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Spec.Env = []corev1.EnvVar{{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{SecretKeyRef: &corev1.SecretKeySelector{
		LocalObjectReference: corev1.LocalObjectReference{Name: "gl-clone"}, Key: "token",
	}}}}
	a.Spec.Channels = []achv1alpha1.ChannelSpec{{
		Name: "gitlab-mr-review", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"},
		Handoff: &achv1alpha1.HandoffSpec{PrepareSpec: achv1alpha1.PrepareSpec{Script: "true", ForwardEnv: []string{"GITLAB_TOKEN"}}, Destination: "handoff"},
	}}
	p := &achv1alpha1.AgentProfile{}

	var original, handoffAlias *corev1.EnvVar
	env := buildAgentEnv(a, p, nil)
	for i := range env {
		e := &env[i]
		switch e.Name {
		case "GITLAB_TOKEN":
			original = e
		case "ACH_SECRET_GITLAB_MR_REVIEW_HANDOFF_GITLAB_TOKEN":
			handoffAlias = e
		}
	}
	if handoffAlias == nil || handoffAlias.ValueFrom == nil || handoffAlias.ValueFrom.SecretKeyRef == nil ||
		handoffAlias.ValueFrom.SecretKeyRef.Name != "gl-clone" ||
		handoffAlias.ValueFrom.SecretKeyRef.Key != "token" {
		t.Fatalf("handoff alias=%+v original=%+v", handoffAlias, original)
	}
}

// POD_NAMESPACE feeds the ach-memory project slug ({POD_NAMESPACE}-{agent.name}).
// Without it the harness degrades to the bare agent name — same bank, different
// key, no error anywhere — so assert the downward-API ref explicitly.
func TestBuildAgentEnv_PodNamespaceFromDownwardAPI(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"

	for _, e := range buildAgentEnv(a, p, nil) {
		if e.Name != "POD_NAMESPACE" {
			continue
		}
		if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.FieldRef == nil ||
			e.ValueFrom.FieldRef.FieldPath != "metadata.namespace" {
			t.Fatalf("POD_NAMESPACE must be a downward-API fieldRef to metadata.namespace, got %+v", e)
		}
		return
	}
	t.Fatal("POD_NAMESPACE not injected")
}

func TestBuildAgentEnv_MemoryAuthInjectedAsEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	a.Spec.Channels = []achv1alpha1.ChannelSpec{{Name: "c", Type: "cron", Cron: &achv1alpha1.CronSpec{Schedule: "* * * * *"}}}
	a.Spec.Memory = &achv1alpha1.MemorySpec{Type: "ach-memory", AchMemory: &achv1alpha1.AchMemorySpec{
		Endpoint: "http://ach-memory.ach.svc:8000/mcp/", Auth: &achv1alpha1.AchMemoryAuthSpec{Type: "bearer", SecretRef: &achv1alpha1.SecretKeyRef{Name: "hs-admin", Key: "token"}},
	}}
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"

	// The ach-memory user key rides in env (secretKeyRef), never inline, never a file.
	var found bool
	for _, e := range buildAgentEnv(a, p, nil) {
		if e.Name != "ACH_SECRET_MEMORY_AUTH" {
			continue
		}
		found = true
		if e.Value != "" || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil ||
			e.ValueFrom.SecretKeyRef.Name != "hs-admin" || e.ValueFrom.SecretKeyRef.Key != "token" {
			t.Errorf("memory secret env must be a secretKeyRef to hs-admin/token, got %+v", e)
		}
	}
	if !found {
		t.Fatal("memory auth secret not injected as env var")
	}

	// No auth → no such env var.
	a.Spec.Memory.AchMemory.Auth = nil
	for _, e := range buildAgentEnv(a, p, nil) {
		if e.Name == "ACH_SECRET_MEMORY_AUTH" {
			t.Errorf("no-auth ach-memory must not inject ACH_SECRET_MEMORY_AUTH")
		}
	}
}

func TestBuildDeployment_Shape(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "ghcr.io/ackstorm/ach-agent:role"
	p.Spec.Control.Persistence = &achv1alpha1.PersistenceSpec{Enabled: true, Size: "1Gi", MountPath: "/var/lib/ach-agent"}

	dep := buildStatefulSet(a, p, "h", buildAgentEnv(a, p, nil))
	ps := dep.Spec.Template.Spec
	if len(ps.Containers) != 1 || ps.Containers[0].Name != agentContainerName || ps.Containers[0].Args != nil {
		t.Fatalf("must render the single %q container with no args: %+v", agentContainerName, ps.Containers)
	}
	if ps.Containers[0].StartupProbe.HTTPGet == nil || ps.Containers[0].StartupProbe.HTTPGet.Port.IntVal != 8080 {
		t.Error("probes must stay HTTP on the fixed control port 8080 (workspace-v1 contract §11)")
	}
	// fsGroup: a fresh root-owned cloud PVC is otherwise unwritable by uid 10001
	// (ach-agent finding, 2026-09-15).
	for name, got := range map[string]*int64{"runAsUser": ps.SecurityContext.RunAsUser, "runAsGroup": ps.SecurityContext.RunAsGroup, "fsGroup": ps.SecurityContext.FSGroup} {
		if got == nil || *got != agentUID {
			t.Errorf("pod %s = %v, want %d", name, got, agentUID)
		}
	}
	var pvcMnt *corev1.VolumeMount
	for i := range ps.Containers[0].VolumeMounts {
		if ps.Containers[0].VolumeMounts[i].Name == pvcVolumeName {
			pvcMnt = &ps.Containers[0].VolumeMounts[i]
		}
	}
	if got := pvcMnt; got == nil || got.MountPath != "/var/lib/ach-agent" || got.SubPath != "" {
		t.Errorf("PVC mount must stay the whole base dir, got %+v", got)
	}
	if tp := buildService(a, p).Spec.Ports[0].TargetPort.IntVal; tp != 8080 {
		t.Errorf("Service targetPort must stay the fixed control port 8080, got %d", tp)
	}
}

func ptrInt64(v int64) *int64 { return &v }

// buildTestControlSTS is the shared fixture for the control-pod naming/volume regression
// tests below — split out of one over-complex test into several focused ones.
func buildTestControlSTS(t *testing.T) (*appsv1.StatefulSet, *achv1alpha1.Agent) {
	t.Helper()
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	a.UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"

	sts := buildStatefulSet(a, p, "h", buildAgentEnv(a, p, nil))
	return sts, a
}

// TestBuildStatefulSet_ControlNaming is the contract §11 regression guard found by root's
// review of f66d476: the control StatefulSet's own metadata.name must be agent-<agent name>
// (matching ServiceName).
func TestBuildStatefulSet_ControlNaming(t *testing.T) {
	sts, a := buildTestControlSTS(t)
	wantName := "agent-" + a.Name
	if sts.Name != wantName {
		t.Errorf("StatefulSet name = %q, want %q (contract §11)", sts.Name, wantName)
	}
	if sts.Spec.ServiceName != wantName {
		t.Errorf("StatefulSet.ServiceName = %q, want %q (must match metadata.name)", sts.Spec.ServiceName, wantName)
	}
}

// TestBuildStatefulSet_ControlPodHardening: the control pod keeps its SA token (the Harness
// talks to the Kubernetes API) but is otherwise locked down: non-root uid 10001,
// RuntimeDefault seccomp, no privilege escalation, no capabilities, and no service-link env
// (every Service in the namespace would otherwise land in the container env — ach-* Services
// even as ACH_* names). The kubernetes.default master env is injected regardless.
func TestBuildStatefulSet_ControlPodHardening(t *testing.T) {
	sts, _ := buildTestControlSTS(t)
	ps := sts.Spec.Template.Spec
	if ps.EnableServiceLinks == nil || *ps.EnableServiceLinks {
		t.Error("enableServiceLinks must be false")
	}
	if sc := ps.SecurityContext; sc == nil || sc.RunAsNonRoot == nil || !*sc.RunAsNonRoot || sc.SeccompProfile == nil || sc.SeccompProfile.Type != corev1.SeccompProfileTypeRuntimeDefault {
		t.Errorf("pod securityContext = %+v, want runAsNonRoot + RuntimeDefault seccomp", sc)
	}
	csc := ps.Containers[0].SecurityContext
	if csc == nil || csc.AllowPrivilegeEscalation == nil || *csc.AllowPrivilegeEscalation {
		t.Errorf("container must set allowPrivilegeEscalation=false: %+v", csc)
	}
	if csc == nil || csc.ReadOnlyRootFilesystem == nil || !*csc.ReadOnlyRootFilesystem {
		t.Errorf("container must set readOnlyRootFilesystem=true: %+v", csc)
	}
	if csc == nil || csc.Capabilities == nil || len(csc.Capabilities.Drop) != 1 || csc.Capabilities.Drop[0] != "ALL" || len(csc.Capabilities.Add) != 0 {
		t.Errorf("container must drop ALL capabilities and add none: %+v", csc)
	}
}

// TestBuildStatefulSet_WritableTmp: with a read-only root the control process's only writable
// path is an emptyDir at /tmp, and TMPDIR points at it explicitly — even over a profile env
// that tries to move it (ach-runtime: exactly one writable path, children inherit TMPDIR).
func TestBuildStatefulSet_WritableTmp(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"
	p.Spec.Control.Env = mkEnv("TMPDIR", "/var/tmp")
	sts := buildStatefulSet(a, p, "h", buildAgentEnv(a, p, nil))
	ps := sts.Spec.Template.Spec
	var vol *corev1.Volume
	for i := range ps.Volumes {
		if ps.Volumes[i].Name == tmpVolumeName {
			vol = &ps.Volumes[i]
		}
	}
	if vol == nil || vol.EmptyDir == nil {
		t.Fatalf("want emptyDir volume %q, got %+v", tmpVolumeName, ps.Volumes)
	}
	var mnt *corev1.VolumeMount
	for i := range ps.Containers[0].VolumeMounts {
		if ps.Containers[0].VolumeMounts[i].Name == tmpVolumeName {
			mnt = &ps.Containers[0].VolumeMounts[i]
		}
	}
	if mnt == nil || mnt.MountPath != "/tmp" || mnt.ReadOnly {
		t.Fatalf("want writable mount at /tmp, got %+v", mnt)
	}
	var tmpdir []string
	for _, e := range ps.Containers[0].Env {
		if e.Name == "TMPDIR" {
			tmpdir = append(tmpdir, e.Value)
		}
	}
	if len(tmpdir) != 1 || tmpdir[0] != "/tmp" {
		t.Fatalf("TMPDIR entries = %v, want exactly [/tmp]", tmpdir)
	}
}

// TestBuildStatefulSet_ConfigMount: the control pod must mount the config ConfigMap as a
// single read-only directory at configMountDir (contract §11 scope reset — no broker-token
// or leaf-TLS volumes exist any more). Token automount is pinned off in
// TestBuildStatefulSet_ControlHasNoTokenAndProbesHealthzFirst.
func TestBuildStatefulSet_ConfigMount(t *testing.T) {
	sts, _ := buildTestControlSTS(t)
	ps := sts.Spec.Template.Spec
	var mnt *corev1.VolumeMount
	for i := range ps.Containers[0].VolumeMounts {
		if ps.Containers[0].VolumeMounts[i].Name == configVolumeName {
			mnt = &ps.Containers[0].VolumeMounts[i]
		}
	}
	if mnt == nil || mnt.MountPath != configMountDir || !mnt.ReadOnly || mnt.SubPath != "" {
		t.Errorf("config directory mount = %+v, want MountPath %q read-only, no SubPath", mnt, configMountDir)
	}
}

// TestControlSelectors_IgnoreWorkspacePods: the runtime labels workspace pods
// ach-agent.ackstorm.ai/agent=<name> too, so every operator selector aimed at the control pod
// (StatefulSet, control + expose Services) must also require the control
// component label — a workspace pod must never become a control endpoint.
func TestControlSelectors_IgnoreWorkspacePods(t *testing.T) {
	sts, a := buildTestControlSTS(t)
	p := &achv1alpha1.AgentProfile{}
	workspacePod := labels.Set{agentLabelKey: a.Name, "runtime.ach-agent.ackstorm.ai/agent-uid": string(a.UID), "runtime.ach-agent.ackstorm.ai/workspace-name": "ach-ws-demo-0123456789abcdef0123"}
	controlPod := labels.Set(sts.Spec.Template.Labels)
	selectors := map[string]map[string]string{
		"StatefulSet":     sts.Spec.Selector.MatchLabels,
		"control Service": buildControlService(a).Spec.Selector,
		"expose Service":  buildService(a, p).Spec.Selector,
	}
	for name, sel := range selectors {
		s := labels.SelectorFromSet(sel)
		if s.Matches(workspacePod) {
			t.Errorf("%s selector %v matches a workspace pod", name, sel)
		}
		if !s.Matches(controlPod) {
			t.Errorf("%s selector %v does not match the control pod %v", name, sel, controlPod)
		}
	}
}

func TestBuildStatefulSet_ControlHasNoTokenAndProbesHealthzFirst(t *testing.T) {
	sts, _ := buildTestControlSTS(t)
	ps := sts.Spec.Template.Spec
	if ps.AutomountServiceAccountToken == nil || *ps.AutomountServiceAccountToken {
		t.Fatal("control pod must set automountServiceAccountToken=false (it has no Kubernetes access)")
	}
	c := ps.Containers[0]
	if got := c.StartupProbe.HTTPGet.Path; got != "/healthz" {
		t.Errorf("startup probe path = %q, want /healthz (readiness waits for the host's inventory, so a /readyz startup probe would restart control)", got)
	}
	if got := c.ReadinessProbe.HTTPGet.Path; got != "/readyz" {
		t.Errorf("readiness probe path = %q, want /readyz", got)
	}
	if got := c.LivenessProbe.HTTPGet.Path; got != "/healthz" {
		t.Errorf("liveness probe path = %q, want /healthz", got)
	}
}

func TestBuildControlService_PublishesNotReadyAddressesButEventServiceStaysGated(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	if !buildControlService(a).Spec.PublishNotReadyAddresses {
		t.Error("8081 control Service must publish not-ready addresses: the host API and the inventory that readiness waits for go through it")
	}
	if buildService(a, &achv1alpha1.AgentProfile{}).Spec.PublishNotReadyAddresses {
		t.Error("8080 event Service must stay readiness-gated: events wait for readiness")
	}
}

func TestBuildControlServiceAccount_NoTokenAutomount(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	a.UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
	if sa := buildControlServiceAccount(a); sa.AutomountServiceAccountToken == nil || *sa.AutomountServiceAccountToken {
		t.Error("control ServiceAccount must not automount a token")
	}
}

func TestCopySpec_ServiceUpdatesPublishNotReadyAddresses(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	existing := buildControlService(a)
	existing.Spec.PublishNotReadyAddresses = false // a Service created by an older operator
	copySpec(existing, buildControlService(a))
	if !existing.Spec.PublishNotReadyAddresses {
		t.Error("an existing control Service must converge to publishNotReadyAddresses=true")
	}
}

func TestBuildStatefulSet_AmbientLabelPortsAndHeaderEnv(t *testing.T) {
	a := &achv1alpha1.Agent{}
	a.Name, a.Namespace = "demo", "ns"
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.Image = "img"
	bindings := []render.EnvBinding{{Name: "ACH_AGENT_MODEL_H0", SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "litellm", Key: "apiKey"}}}
	sts := buildStatefulSet(a, p, "h", buildAgentEnv(a, p, bindings))

	if got := sts.Spec.Template.Labels["istio.io/dataplane-mode"]; got != "ambient" {
		t.Fatalf("labels = %v, want istio.io/dataplane-mode=ambient", sts.Spec.Template.Labels)
	}
	c := sts.Spec.Template.Spec.Containers[0]
	ports := map[int32]bool{}
	for _, p := range c.Ports {
		ports[p.ContainerPort] = true
	}
	if !ports[8080] || !ports[8081] || len(ports) != 2 {
		t.Fatalf("ports = %v, want exactly 8080 and 8081", c.Ports)
	}
	var found bool
	for _, e := range c.Env {
		if e.Name == "ACH_AGENT_MODEL_H0" {
			found = e.ValueFrom != nil && e.ValueFrom.SecretKeyRef.Name == "litellm" && e.ValueFrom.SecretKeyRef.Key == "apiKey"
		}
	}
	if !found {
		t.Fatalf("ACH_AGENT_MODEL_H0 not wired from Secret litellm/apiKey: %v", c.Env)
	}
	if sts.Spec.Template.Spec.AutomountServiceAccountToken == nil || *sts.Spec.Template.Spec.AutomountServiceAccountToken {
		t.Fatal("token automount must be off")
	}
}

func TestBuildAgentEnv_LiteralHeaderBecomesPlainEnv(t *testing.T) {
	a, p := &achv1alpha1.Agent{}, &achv1alpha1.AgentProfile{}
	v := "en"
	env := buildAgentEnv(a, p, []render.EnvBinding{{Name: "ACH_AGENT_MODEL_H0", Value: &v}})
	for _, e := range env {
		if e.Name == "ACH_AGENT_MODEL_H0" {
			if e.Value != "en" || e.ValueFrom != nil {
				t.Fatalf("literal header env = %+v, want plain value", e)
			}
			return
		}
	}
	t.Fatal("ACH_AGENT_MODEL_H0 missing")
}

func TestBuildAgentEnv_AchKeyComesFromTheBinding(t *testing.T) {
	a, p := &achv1alpha1.Agent{}, &achv1alpha1.AgentProfile{}
	env := buildAgentEnv(a, p, []render.EnvBinding{{Name: render.AchKeyEnv, SecretKeyRef: &achv1alpha1.SecretKeyRef{Name: "ek", Key: "token"}}})
	var n int
	for _, e := range env {
		if e.Name == render.AchKeyEnv {
			n++
			if e.ValueFrom == nil || e.ValueFrom.SecretKeyRef.Name != "ek" || e.ValueFrom.SecretKeyRef.Key != "token" {
				t.Fatalf("%s = %+v", render.AchKeyEnv, e)
			}
		}
	}
	if n != 1 {
		t.Fatalf("%s appears %d times, want 1", render.AchKeyEnv, n)
	}
}

func TestChildNamesDeriveFromControlName(t *testing.T) {
	for _, name := range []string{"researcher", "team.reviewer"} {
		base := render.ControlName(name)
		if configMapName(name) != base+"-config" || pvcName(name) != base+"-state" || httpServiceName(name) != base+"-http" {
			t.Errorf("child names for %q = %s %s %s", name, configMapName(name), pvcName(name), httpServiceName(name))
		}
	}
}

func TestGraceBelowDrainPlus15IsRejected(t *testing.T) {
	a := &achv1alpha1.Agent{}
	p := &achv1alpha1.AgentProfile{}
	p.Spec.Control.TerminationGracePeriodSeconds = ptrInt64(30)
	a.Spec.Workspace = &achv1alpha1.WorkspaceSpec{ShutdownTimeoutSeconds: ptrInt64(20)}
	if err := checkGracePeriod(p, a); err == nil || !strings.Contains(err.Error(), "35") {
		t.Fatalf("err = %v, want a rejection naming the minimum 35", err)
	}
	p.Spec.Control.TerminationGracePeriodSeconds = ptrInt64(35)
	if err := checkGracePeriod(p, a); err != nil {
		t.Fatalf("grace == drain + 15 must pass: %v", err)
	}
	// The profile default workspace applies when the agent sets none.
	a.Spec.Workspace = nil
	p.Spec.Defaults.Workspace = &achv1alpha1.WorkspaceSpec{ShutdownTimeoutSeconds: ptrInt64(300)}
	if err := checkGracePeriod(p, a); err == nil {
		t.Fatal("a 35s grace must be rejected against the profile's 300s drain")
	}
	p.Spec.Control.TerminationGracePeriodSeconds = nil // default 120
	if err := checkGracePeriod(p, a); err == nil {
		t.Fatal("the 120s default grace must be rejected against a 300s drain")
	}
}
