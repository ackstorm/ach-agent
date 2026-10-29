// SPDX-License-Identifier: MIT

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// LocalObjectRef references a resource by name in the CR's namespace.
type LocalObjectRef struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
}

// AchSpec is the ACH platform block (config: ach{baseUrl,environment,identity,capability},
// contract §5). It is the SAME type used by both AgentProfile.spec.achagent.ach (shared
// defaults: baseUrl/environment/capability) and ACHAgent's inline AgentDefaults override
// (adds the required identity credential) — per-field deep merge via ResolveAch, exactly
// like model/engine/limits. identity is never a shared profile default: AgentProfile forbids
// setting it (object-level CEL on AgentProfileSpec); ACHAgentSpec requires it.
//
// BaseURL resolves as ACHAgent.spec.ach.baseUrl ?? AgentProfile.spec.achagent.ach.baseUrl ??
// operator ACH_BASE_URL env (agentrender.ResolveAchBaseURL). An empty result blocks the agent.
type AchSpec struct {
	// +optional
	BaseURL string `json:"baseUrl,omitempty"`
	// Environment is the ACH Hub Environment name, for documentation/intent only — see
	// CapabilitySpec.Environment's original doc: the ek already scopes the environment
	// server-side and the harness reads the hydrated environment, never this field. A
	// POINTER (unlike most other optional strings here): AgentProfile.spec.achagent.ach may
	// set a shared default (ResolveAch), and an agent needs to be able to override it with an
	// explicit empty string — indistinguishable from "unset" on a plain omitempty string.
	// +optional
	Environment *string `json:"environment,omitempty"`
	// Identity carries the ACH ek_ (injected as ACH_TOKEN env via secretKeyRef). Required
	// on the agent (ACHAgentSpec object-level CEL); forbidden on the profile (AgentProfileSpec
	// object-level CEL) — a credential is never an implicit shared profile default.
	// +optional
	Identity *IdentitySpec `json:"identity,omitempty"`
	// +optional
	Capability *CapabilitySpec `json:"capability,omitempty"`
}

// ModelSpec selects the ACH-served model (config: model{name,type,params,thinking}).
// Name/Type are optional authoring fields (a profile or an agent may supply either
// alone via per-field merge, ResolveModel) — Render2 requires the EFFECTIVE
// (post-merge) name to be nonempty and defaults an unset effective type to openai
// (ACH model aliases resolve on LiteLLM's /v1); neither is a CRD-level requirement,
// since either field may legitimately come from the sibling.
type ModelSpec struct {
	// +optional
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name,omitempty"`
	// +optional
	// +kubebuilder:validation:Enum=openai;gemini;anthropic
	Type string `json:"type,omitempty"`
	// Params is an open, unvalidated dict splatted to the model client.
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Params *apiextensionsv1.JSON `json:"params,omitempty"`
	// Thinking is the normalized model-level reasoning intent (config: model.thinking).
	// Free-form (no Enum) — ach-agent's Pydantic ThinkingBlock is the single enforcer
	// (D-2 precedent): effort one of minimal|low|medium|high|xhigh, requires enabled=true.
	// +optional
	Thinking *ThinkingSpec `json:"thinking,omitempty"`
}

// AgentDefaults is the shared set of profile defaults that an agent may override.
// AgentProfile.spec.achagent names these defaults, and ACHAgentSpec embeds this
// type inline. Resolution is a per-field deep merge: a field set on the agent
// wins, while an omitted field inherits from the profile. Slices, maps, and
// nested blocks such as engine.pi and cost are atomic and are not recursively merged.
// The resolvers are the source of truth for this behavior. Image is required on
// the profile, but optional on the agent and inherited when omitted.
type AgentDefaults struct {
	// +optional
	Image string `json:"image,omitempty"`
	// +optional
	Ach *AchSpec `json:"ach,omitempty"`
	// +optional
	Model *ModelSpec `json:"model,omitempty"`
	// +optional
	Engine *EngineSpec `json:"engine,omitempty"`
	// +optional
	Limits *LimitsSpec `json:"limits,omitempty"`
	// +optional
	Health *HealthSpec `json:"health,omitempty"`
	// Workspace is the Harness Workspace/session lifecycle policy (contract §3/§7).
	// +optional
	Workspace *WorkspaceSpec `json:"workspace,omitempty"`
	// Artifacts is the Harness Artifacts policy (contract §8).
	// +optional
	Artifacts *ArtifactsSpec `json:"artifacts,omitempty"`
}

// ThinkingSpec is the normalized reasoning intent each engine translates for itself
// (pi: models.json reasoning + --thinking; opencode: per-call providerOptions).
type ThinkingSpec struct {
	// +optional
	Enabled bool `json:"enabled,omitempty"`
	// +optional
	Effort string `json:"effort,omitempty"`
}

// EngineSpec is the harness-local engine block (config: engine.*). Unset fields are omitted
// (the harness defaults them). home/workDir/idleTtlSeconds/maxToolCalls/type/pi were retired
// for workspace-v1 (contract §10): one engine (OpenCode v2), no placements/binaries/paths to
// select, and tool-call counting is replaced by limits.maxSteps.
type EngineSpec struct {
	// ForwardEnv selects literal-valued names from the merged env to the engine subprocess
	// (contract §5). Only literal values are permitted — a name resolving to a secretKeyRef,
	// a reserved name or a name absent from the merged env is a configuration error
	// (agentrender.Render), not a silent drop.
	// +optional
	ForwardEnv []string `json:"forwardEnv,omitempty"`
	// DenyTools names native OpenCode tools (e.g. question, execute, websearch, webfetch)
	// hidden from the managed agent's model. MCP tools are governed by
	// ach.capability.filter.exclude.tools; a dotted name is MCP syntax and is rejected.
	// +optional
	// +listType=set
	// +kubebuilder:validation:items:Pattern=`^[A-Za-z0-9_-]+$`
	DenyTools []string `json:"denyTools,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	StartupTimeoutSeconds *int64 `json:"startupTimeoutSeconds,omitempty"`
	// Compaction configures OpenCode's native context compaction (contract §4). Replaces the
	// retired session.maxTokens/overflow knobs.
	// +optional
	Compaction *CompactionSpec `json:"compaction,omitempty"`
}

// CompactionSpec configures OpenCode's native context compaction (config:
// engine.compaction). Fields stay POINTERS so an agent can override one of
// auto/keep/buffer without restating the others (per-field merge, ResolveCompaction) —
// the wire schema requires all three unconditionally wherever compaction is rendered, but
// that completeness is enforced on the RESOLVED (post-merge) result (RenderEngineV1,
// profile-level required-block CEL below), never by forcing the type itself to be
// all-or-nothing. Explicit false/0 must stay distinguishable from "agent didn't touch this
// field" — a non-pointer bool/int64 cannot express that.
type CompactionSpec struct {
	// +optional
	Auto *bool `json:"auto,omitempty"`
	// Keep is resolved as one atomic sub-block (its only field, tokens, has no independent
	// meaning to override alone) — same convention as Persistence/Session below.
	// +optional
	Keep *CompactionKeepSpec `json:"keep,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=0
	Buffer *int64 `json:"buffer,omitempty"`
}

// CompactionKeepSpec is engine.compaction.keep.
type CompactionKeepSpec struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	Tokens *int64 `json:"tokens,omitempty"`
}

// LimitsSpec bounds invocations (config: limits.*, contract §3). Fields stay POINTERS
// (except MaxSteps, below) so an agent can override one limit without restating the rest —
// per-field merge via ResolveLimits. The wire schema requires every field but
// maxConcurrentScripts unconditionally, but that completeness is checked on the RESOLVED
// result (RenderLimitsV1), not by forcing the type non-pointer.
type LimitsSpec struct {
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxActiveWorkspaces *int64 `json:"maxActiveWorkspaces,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxConcurrentInvocations *int64 `json:"maxConcurrentInvocations,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxConcurrentScripts *int64 `json:"maxConcurrentScripts,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxInvocationSeconds *int64 `json:"maxInvocationSeconds,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxQueuedTotal *int64 `json:"maxQueuedTotal,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	IdempotencyWindowSeconds *int64 `json:"idempotencyWindowSeconds,omitempty"`
	// MaxSteps bounds OpenCode steps per invocation (contract §3: no unlimited value). A
	// POINTER like every other field here, so an agent can override one limit (e.g. just
	// maxSteps, or everything EXCEPT maxSteps) without being forced to restate the rest of
	// the block just to satisfy a non-pointer required field — completeness of the full
	// resolved limits block is still enforced, but at RenderLimitsV1 (the merged result),
	// not by forcing every partial override to carry every field.
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxSteps *int64 `json:"maxSteps,omitempty"`
}

// HealthSpec is the harness HTTP surface (config: health{host,port}). In standalone
// placement it drives the Service targetPort and the container probes. In distributed
// placement it is NOT used by the operator: the Service targets channels on 8080 and every
// role is probed over HTTP on a fixed port (channels 8080, harness 8090, engine 8081)
// bound by ach-agent. Harness default port is 8080.
type HealthSpec struct {
	// +optional
	Host string `json:"host,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=65535
	Port int32 `json:"port,omitempty"`
}

// WorkspacePersistenceSpec is a Workspace or session durability switch (contract §7:
// workspace.persistence, workspace.session.persistence). This is the Harness/Storage logical
// policy — distinct from AgentProfileSpec.Persistence, which is the operator's ephemeral
// control/execution-pod active-volume infrastructure.
// WorkspacePersistenceSpec is a Workspace or session durability switch (contract §7:
// workspace.persistence, workspace.session.persistence). This is the Harness/Storage logical
// policy — distinct from AgentProfileSpec.Persistence, which is the operator's ephemeral
// control/execution-pod active-volume infrastructure. Both fields are required: the wire
// schema requires retentionDays unconditionally, even when enabled=false.
// Both Enabled and RetentionDays are pointers so an agent can override either one alone
// (ResolvePersistence per-field merge) without restating the other just to satisfy a
// non-pointer required field — e.g. `persistence: {enabled: false}` must inherit the
// profile's retentionDays, not null it out. Completeness of the resolved block (both
// fields set) is enforced at RenderWorkspaceV1/RenderArtifactsV1 time, same as every other
// policy block.
type WorkspacePersistenceSpec struct {
	// +optional
	Enabled *bool `json:"enabled,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	RetentionDays *int64 `json:"retentionDays,omitempty"`
}

// WorkspaceSessionSpec is workspace.session (contract §7). Pointer fields: per-field merge.
type WorkspaceSessionSpec struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	IdleTimeoutSeconds *int64 `json:"idleTimeoutSeconds,omitempty"`
	// Persistence is resolved as one atomic sub-block (same convention as
	// CompactionSpec.Keep): its two fields are only meaningful together.
	// +optional
	Persistence *WorkspacePersistenceSpec `json:"persistence,omitempty"`
}

// WorkspaceSpec is the Harness Workspace lifecycle policy (config: workspace.*, contract
// §3/§7). Pointer fields (except where noted) so an agent can override one setting without
// restating the rest — per-field merge via ResolveWorkspace. Completeness of the wire-
// required fields is checked on the RESOLVED result (RenderWorkspaceV1), not by forcing the
// type non-pointer.
type WorkspaceSpec struct {
	// +optional
	// +kubebuilder:validation:Minimum=0
	IdleTimeoutSeconds *int64 `json:"idleTimeoutSeconds,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=0
	ShutdownTimeoutSeconds *int64 `json:"shutdownTimeoutSeconds,omitempty"`
	// MaxConcurrentSessions bounds sessions executing work simultaneously in a Workspace.
	// A handoff with a shared destination requires this to be 1 (contract §6).
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxConcurrentSessions *int64 `json:"maxConcurrentSessions,omitempty"`
	// +optional
	Persistence *WorkspacePersistenceSpec `json:"persistence,omitempty"`
	// +optional
	Session *WorkspaceSessionSpec `json:"session,omitempty"`
}

// ArtifactsSpec is the Harness Artifacts policy (config: artifacts.*, contract §8). Enabled
// is a pointer, same rationale as WorkspacePersistenceSpec.Enabled: an agent overriding just
// maxArtifactBytes/retentionDays must not be forced to also restate enabled. All three merge
// per-field (ResolveArtifacts); completeness of the resolved block is enforced at
// RenderArtifactsV1.
type ArtifactsSpec struct {
	// +optional
	Enabled *bool `json:"enabled,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	MaxArtifactBytes *int64 `json:"maxArtifactBytes,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	RetentionDays *int64 `json:"retentionDays,omitempty"`
}

// PersistenceSpec configures PVC-backed durable state (config: persistence{enabled,mountPath}).
// +kubebuilder:validation:XValidation:rule="!self.enabled || (has(self.size) && size(self.size) > 0)",message="persistence.size is required when persistence.enabled=true"
type PersistenceSpec struct {
	// +kubebuilder:validation:Required
	Enabled bool `json:"enabled"`
	// +optional
	Size string `json:"size,omitempty"`
	// +optional
	StorageClassName string `json:"storageClassName,omitempty"`
	// +optional
	// +kubebuilder:default="/var/lib/ach-agent"
	MountPath string `json:"mountPath,omitempty"`
	// RetainPolicy controls PVC lifecycle on ACHAgent deletion. Retain → the PVC is created
	// WITHOUT a controller owner-ref, so it survives agent deletion (operator-managed cleanup).
	// +optional
	// +kubebuilder:validation:Enum=Retain;Delete
	RetainPolicy *string `json:"retainPolicy,omitempty"`
}

// AgentProfileSpec is the reusable infra + defaults half. Agent-scoped defaults
// (image/ach/model/engine/limits/health/workspace/artifacts) live under the named achagent
// block and deep-merge with an ACHAgent's inline AgentDefaults (agent field wins);
// everything else here is profile-only infrastructure an agent cannot override.
// +kubebuilder:validation:XValidation:rule="!has(self.achagent.ach) || !has(self.achagent.ach.identity)",message="spec.achagent.ach.identity is forbidden on a profile — a credential is never a shared implicit default (contract §5)"
type AgentProfileSpec struct {
	// Achagent holds the agent-overridable defaults. image is required here
	// (object-level CEL); the other fields are optional defaults.
	// +kubebuilder:validation:Required
	Achagent AgentDefaults `json:"achagent"`
	// +optional
	ImagePullSecrets []corev1.LocalObjectReference `json:"imagePullSecrets,omitempty"`
	// ControlServiceAccountName, when set, is an existing ServiceAccount that the control
	// (Harness) pods of every ACHAgent using this profile run as, instead of the per-agent
	// ach-harness-<uid>. The operator does not create, own or delete it: it must already
	// exist in the agent's namespace (e.g. one bound to a cloud identity such as EKS Pod
	// Identity, which has no wildcards). Each agent still gets its own Role/RoleBinding,
	// bound to this ServiceAccount. Empty keeps the per-agent ach-harness-<uid>.
	// +optional
	// +kubebuilder:validation:MaxLength=63
	// +kubebuilder:validation:Pattern=`^[a-z0-9]([-a-z0-9]*[a-z0-9])?$`
	ControlServiceAccountName string `json:"controlServiceAccountName,omitempty"`
	// +optional
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`
	// Env are pod-level environment variables inherited by ACHAgents using this profile.
	// Reserved ACH_* names are forbidden because the operator owns that namespace. Only
	// literal values and secretKeyRef sources are supported.
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:XValidation:rule="self.all(e, !e.name.startsWith('ACH_'))",message="env must not set reserved ACH_* vars"
	// +kubebuilder:validation:XValidation:rule="self.all(e, !has(e.valueFrom) || (has(e.valueFrom.secretKeyRef) && !has(e.valueFrom.configMapKeyRef) && !has(e.valueFrom.fieldRef) && !has(e.valueFrom.resourceFieldRef) && !has(e.valueFrom.fileKeyRef)))",message="env valueFrom supports only secretKeyRef"
	// +kubebuilder:validation:XValidation:rule="self.all(e, !has(e.valueFrom) || !has(e.value) || e.value == '')",message="env value and valueFrom are mutually exclusive"
	Env []corev1.EnvVar `json:"env,omitempty"`
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
	// +optional
	Tolerations []corev1.Toleration `json:"tolerations,omitempty"`
	// +optional
	Persistence *PersistenceSpec `json:"persistence,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=0
	TerminationGracePeriodSeconds *int64 `json:"terminationGracePeriodSeconds,omitempty"`
	// Execution is the contract §11 execution-role infrastructure (the mini-harness pod
	// Harness creates per Workspace): image, resources, ephemeral storage and scheduling for
	// THAT role, distinct from achagent.image/spec.resources above, which remain the
	// CONTROL pod's (Channels+Harness) settings. Root ruling 2026-10-01: a profile-only
	// section, not a new CR/WorkspaceProfile kind.
	// +kubebuilder:validation:Required
	Execution ExecutionInfraSpec `json:"execution"`
}

// ExecutionInfraSpec is AgentProfileSpec.Execution (config: infrastructure.execution,
// contract §11). image and ephemeralStorage are required — no operator-wide default
// (root ruling: explicit required effective policy, not a guessed renderer default).
type ExecutionInfraSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Image string `json:"image"`
	// +optional
	Resources *corev1.ResourceRequirements `json:"resources,omitempty"`
	// EphemeralStorage is a Kubernetes resource.Quantity string (e.g. "2Gi") for the
	// execution pod's ephemeral-storage resource request/limit.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	EphemeralStorage string `json:"ephemeralStorage"`
	// +optional
	ImagePullSecrets []corev1.LocalObjectReference `json:"imagePullSecrets,omitempty"`
	// +optional
	NodeSelector map[string]string `json:"nodeSelector,omitempty"`
	// +optional
	Tolerations []corev1.Toleration `json:"tolerations,omitempty"`
	// +optional
	// +kubebuilder:validation:Minimum=0
	TerminationGracePeriodSeconds *int64 `json:"terminationGracePeriodSeconds,omitempty"`
	// ReadyTimeoutSeconds is how long a new execution unit has to become ready, rendered
	// verbatim as the control config's workspace.readyTimeoutSeconds. Omitted: the runtime
	// default (30). Raise it for backends that start slowly (OpenShell: 20-30 s). Needs an
	// ach-runtime release that knows the field.
	// +kubebuilder:validation:Minimum=1
	// +optional
	ReadyTimeoutSeconds *int64 `json:"readyTimeoutSeconds,omitempty"`
}

// AgentProfileStatus is minimal — profiles are read by ACHAgent; they have no side effects.
type AgentProfileStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced,shortName=aprofile
// +kubebuilder:printcolumn:name="Image",type=string,JSONPath=".spec.achagent.image"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 50",message="AgentProfile name must be <= 50 chars (operator derives <=63-char child names)"
// +kubebuilder:validation:XValidation:rule="has(self.spec.achagent) && has(self.spec.achagent.image) && size(self.spec.achagent.image) > 0",message="spec.achagent.image is required (nonempty)"
// +kubebuilder:validation:XValidation:rule="has(self.spec.achagent) && has(self.spec.achagent.limits)",message="spec.achagent.limits is required — it is the only place a profile is guaranteed to set limits.maxSteps, which contract §3 requires on every agent (positive, never unlimited)"
// +kubebuilder:validation:XValidation:rule="has(self.spec.achagent) && has(self.spec.achagent.engine) && has(self.spec.achagent.engine.compaction)",message="spec.achagent.engine.compaction is required — the wire schema requires it unconditionally and no harness default is published"
// +kubebuilder:validation:XValidation:rule="has(self.spec.achagent) && has(self.spec.achagent.workspace)",message="spec.achagent.workspace is required — the wire schema requires every workspace.* field unconditionally"
// +kubebuilder:validation:XValidation:rule="has(self.spec.achagent) && has(self.spec.achagent.artifacts)",message="spec.achagent.artifacts is required — the wire schema requires every artifacts.* field unconditionally"
// +kubebuilder:validation:XValidation:rule="has(self.spec.execution) && has(self.spec.execution.image) && size(self.spec.execution.image) > 0",message="spec.execution.image is required (nonempty)"
// +kubebuilder:validation:XValidation:rule="has(self.spec.execution) && has(self.spec.execution.ephemeralStorage) && size(self.spec.execution.ephemeralStorage) > 0",message="spec.execution.ephemeralStorage is required (nonempty)"

// AgentProfile is the reusable infra + defaults for a class of agents.
type AgentProfile struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentProfileSpec   `json:"spec,omitempty"`
	Status AgentProfileStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentProfileList contains a list of AgentProfile.
type AgentProfileList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []AgentProfile `json:"items"`
}

func init() {
	SchemeBuilder.Register(&AgentProfile{}, &AgentProfileList{})
}
