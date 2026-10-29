// SPDX-License-Identifier: MIT

package render

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// WSAgentBlock is agent{name,namespace,uid} (schema AgentIdentity — fully required).
type WSAgentBlock struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	UID       string `json:"uid"`
}

// WSQueueBlock is channels[].queue (schema QueueBlock — key required; ackMode was retired,
// unlike the agent-config-v1 QueueBlock which always rendered it).
type WSQueueBlock struct {
	Type string `json:"type,omitempty"`
	Key  string `json:"key"`
}

// WSA2AAuthBlock is channels[].a2a.auth (schema A2AAuthBlock) — FLAT like WSWebhookAuthBlock:
// env rides directly on the auth object, not nested under a "secret" key.
type WSA2AAuthBlock struct {
	Header string `json:"header,omitempty"`
	Env    string `json:"env"`
}

// WSA2ABlock is channels[].a2a (schema A2ABlock — auth required; mode async-only in v1).
type WSA2ABlock struct {
	Mode string         `json:"mode,omitempty"`
	Auth WSA2AAuthBlock `json:"auth"`
}

// RenderQueueV1/RenderA2AV1 map their specs to the wire shape.
func RenderQueueV1(q *achv1alpha1.QueueSpec) *WSQueueBlock {
	if q == nil {
		return nil
	}
	return &WSQueueBlock{Type: "redis", Key: q.Key}
}

func RenderA2AV1(ch *achv1alpha1.ChannelSpec) *WSA2ABlock {
	if ch.A2A == nil {
		return nil
	}
	return &WSA2ABlock{Mode: "async", Auth: WSA2AAuthBlock{Header: ch.A2A.Auth.Header, Env: channelSecretEnvName(ch)}}
}

// RenderScriptV1 maps channels[].script (schema ScriptBlock — shape-compatible with the
// existing PrepareBlock: script/env/secretEnv/timeoutSeconds, no scope or destination).
// Strict (renderHookV1): a forwardEnv name absent from the merged environment errors
// rather than silently vanishing from the rendered script block.
func RenderScriptV1(ch *achv1alpha1.ChannelSpec, resolvedEnv []corev1.EnvVar) (*PrepareBlock, error) {
	if ch.Script == nil {
		return nil, nil
	}
	return renderPrepareV1(ch, ch.Script, resolvedEnv, "SCRIPT")
}

// WSChannelBlock is one channels[] entry (schema ChannelConfig — name/type required;
// routing is always present, explicit null when there is no override).
type WSChannelBlock struct {
	Name        string          `json:"name"`
	Type        string          `json:"type"`
	Concurrency *int64          `json:"concurrency,omitempty"`
	Source      string          `json:"source,omitempty"`
	Routing     *WSRoutingBlock `json:"routing"`
	Prompt      string          `json:"prompt,omitempty"`
	Webhook     *WSWebhookBlock `json:"webhook,omitempty"`
	Handoff     *WSHandoffBlock `json:"handoff,omitempty"`
	Script      *PrepareBlock   `json:"script,omitempty"`
	Cron        *CronBlock      `json:"cron,omitempty"`
	Queue       *WSQueueBlock   `json:"queue,omitempty"`
	A2A         *WSA2ABlock     `json:"a2a,omitempty"`
}

// RenderChannelV1 maps one ChannelSpec to the wire shape, reusing the legacy CronBlock
// (unchanged) and the handoff/webhook/routing/script mappers above.
func RenderChannelV1(ch *achv1alpha1.ChannelSpec, resolvedEnv []corev1.EnvVar) (WSChannelBlock, error) {
	handoff, err := RenderHandoffV1(ch, resolvedEnv)
	if err != nil {
		return WSChannelBlock{}, err
	}
	script, err := RenderScriptV1(ch, resolvedEnv)
	if err != nil {
		return WSChannelBlock{}, err
	}
	cb := WSChannelBlock{
		Name: ch.Name, Type: ch.Type, Concurrency: ch.Concurrency, Source: ch.Source,
		Routing: RenderRoutingV1(ch.Routing), Prompt: ch.Prompt,
		Webhook: RenderWebhookV1(ch), Handoff: handoff, Script: script,
		Queue: RenderQueueV1(ch.Queue), A2A: RenderA2AV1(ch),
	}
	if ch.Cron != nil {
		cb.Cron = &CronBlock{Schedule: ch.Cron.Schedule, Timezone: ch.Cron.Timezone}
	}
	return cb, nil
}

// WSHooksBlockFull is the top-level hooks{...} — WSHooksBlock already defined above covers
// the shape; this alias documents it is used at the root, not just per-agent.

// WSInfrastructureBlock is the root infrastructure{facadeEndpoint} of workspace-v2. The
// runtime carries no Kubernetes or execution-unit field any more: the operator, as the
// host (contract v2 section 5), owns the control and execution infrastructure itself.
type WSInfrastructureBlock struct {
	FacadeEndpoint string `json:"facadeEndpoint"`
}

// controlPort is the fixed contract §11 control Service port. achagent_workload.go derives
// the same value independently (controlServicePort) since both must agree with the rendered
// controlEndpoint.
const controlPort = 8081

// HarnessName is the per-agent ach-harness-<uid> name: the default control ServiceAccount
// and, always, the per-agent execution ServiceAccount. The one place that prefix
// is spelled.
func HarnessName(agentUID string) string { return "ach-harness-" + agentUID }

// ExecutionServiceAccountName is ach-execution-<uid> (contract §11; the runtime schema
// requires exactly this value in infrastructure.execution.serviceAccount). Also the name of
// the execution bootstrap ConfigMap.
func ExecutionServiceAccountName(agentUID string) string { return "ach-execution-" + agentUID }

// ControlServiceAccountName is the effective control (Harness) ServiceAccount: the profile's
// stable controlServiceAccountName when set, else the per-agent HarnessName.
func ControlServiceAccountName(agentUID string, p *achv1alpha1.AgentProfile) string {
	if p.Spec.ControlServiceAccountName != "" {
		return p.Spec.ControlServiceAccountName
	}
	return HarnessName(agentUID)
}

// controlNameMaxPart bounds the agent-name part of ControlName: "agent-" (6) + 46 = 52, so
// the pod name (+"-0") and the StatefulSet's controller-revision-hash label value (+"-" +
// 10-char hash) both stay within the 63-char label limit.
const controlNameMaxPart = 46

// ControlName is the control StatefulSet, its governing Service and its pod-name stem:
// agent-<agent name>. A name longer than controlNameMaxPart (agent names may be up to 253
// chars) — or one carrying '.', valid in a metadata.name but not in a Service name — becomes
// its first 37 chars (dots as '-', trailing '-' dropped) + "-" + the first 8 hex of
// sha256(full name): deterministic, and two names sharing a 37-char prefix still differ.
// "agent-" never collides with the operator's achagent-<name> Service nor the ach-* platform
// names (different leading bytes). The runtime's workspace names agent-<name part>-<20 hex>
// share the stem; the CRD rejects agent names ending in -<20 hex>, so they never coincide.
func ControlName(agentName string) string {
	part := agentName
	if len(part) > controlNameMaxPart || strings.Contains(part, ".") {
		sum := sha256.Sum256([]byte(agentName))
		part = strings.ReplaceAll(part, ".", "-")
		if len(part) > 37 {
			part = part[:37]
		}
		part = strings.TrimRight(part, "-") + "-" + hex.EncodeToString(sum[:])[:8]
	}
	return "agent-" + part
}

// RenderInfrastructureV2 returns the URL by which execution units reach the control unit's
// 8081/facades. FQDN form on purpose: short names fail under some sandbox proxies' name
// resolution (OpenShell PoC). ControlName hashes and truncates long or dotted agent names,
// so the host is never built by hand.
func RenderInfrastructureV2(agentName, namespace string) WSInfrastructureBlock {
	return WSInfrastructureBlock{
		FacadeEndpoint: fmt.Sprintf("http://%s.%s.svc.cluster.local:%d/facades", ControlName(agentName), namespace, controlPort),
	}
}

// WSConfig is the full workspace-v2 wire document (schema root). McpServers has no home in
// this schema yet (the root has no mcpServers property at all — contract §10: "MCP
// adicional... soporte planificado", not yet migrated), so ACHAgentSpec.MCPServers is not
// rendered here; that is an existing, separately tracked gap, not an oversight.
type WSConfig struct {
	SchemaVersion  string                `json:"schemaVersion"`
	Agent          WSAgentBlock          `json:"agent"`
	ConfigVersion  string                `json:"configVersion"`
	Ach            WSAchBlock            `json:"ach"`
	Model          ModelBlock            `json:"model"`
	Engine         WSEngineBlock         `json:"engine"`
	Limits         WSLimitsBlock         `json:"limits"`
	Workspace      WSWorkspaceBlock      `json:"workspace"`
	Artifacts      WSArtifactsBlock      `json:"artifacts"`
	Channels       []WSChannelBlock      `json:"channels,omitempty"`
	Prompt         *PromptBlock          `json:"prompt,omitempty"`
	Hooks          WSHooksBlock          `json:"hooks"`
	Memory         *MemoryBlock          `json:"memory,omitempty"`
	Infrastructure WSInfrastructureBlock `json:"infrastructure"`
}

// renderMemoryV1 restricts the legacy renderMemory mapping (render.go, shared struct shape)
// to the workspace-v2 memory union: ach-memory or absent — never the retired "codemem"
// variant the legacy agent-config-v1 schema also accepted. Rejecting here (not silently
// delegating) matches contract §11 fail-closed and the vendored schema's MemoryBlock
// anyOf[AchMemoryMemory, null].
func renderMemoryV1(m *achv1alpha1.MemorySpec) (*MemoryBlock, error) {
	if m == nil {
		return nil, nil
	}
	if m.Type != memoryTypeAchMemory {
		return nil, fmt.Errorf("memory.type %q is not supported by workspace-v2 (only %q)", m.Type, memoryTypeAchMemory)
	}
	return renderMemory(m), nil
}

// DefaultModelType is the effective model.type when neither the agent nor the profile
// sets one: ACH model aliases resolve through LiteLLM's model_group_alias on /v1.
const DefaultModelType = "openai"

// Render2 collapses profile + agent into the full workspace-v2 WSConfig, including
// configVersion. This is the orchestrator task-2-ach-report.md names as the pending
// cutover target — now implemented, wired by the controller in place of the retired
// Render(). Fails closed (returns an error, never a fabricated value) on: missing
// identity/model/image/baseURL (same checks as the retired Render()), any wire-required
// policy block left unresolved (limits/engine.compaction/workspace/artifacts — CRD
// admission should already prevent this for any admitted object, this is defense in
// depth). Global Storage backend/credential validation (ValidateRuntimeStorage) is a
// separate, later check the controller runs against the resolved WSConfig — not part of
// this pure render.
func Render2(p achv1alpha1.AgentProfile, a achv1alpha1.ACHAgent, defaultBaseURL string) (WSConfig, error) {
	model := ResolveModel(a.Spec.Model, p.Spec.Achagent.Model)
	if model == nil {
		return WSConfig{}, fmt.Errorf("no model: set ACHAgent.spec.model or AgentProfile.spec.achagent.model")
	}
	if model.Name == "" {
		return WSConfig{}, fmt.Errorf("no effective model.name: set it on the agent or the profile")
	}
	if model.Type == "" {
		// Copy before defaulting: ResolveModel may return the profile/agent pointer itself.
		m := *model
		m.Type = DefaultModelType
		model = &m
	}
	if ResolveImage(a.Spec.Image, p.Spec.Achagent.Image) == "" {
		return WSConfig{}, fmt.Errorf("no image: set ACHAgent.spec.image or AgentProfile.spec.achagent.image")
	}
	baseURL := ResolveAchBaseURL(a.Spec.Ach, p.Spec.Achagent.Ach, defaultBaseURL)
	if baseURL == "" {
		return WSConfig{}, fmt.Errorf("no ACH base URL: set ACHAgent.spec.ach.baseUrl, AgentProfile.spec.achagent.ach.baseUrl, or operator ACH_BASE_URL")
	}
	ach, err := RenderAchV1(ResolveAch(a.Spec.Ach, p.Spec.Achagent.Ach), baseURL)
	if err != nil {
		return WSConfig{}, err
	}
	resolvedEnv := ResolveEnv(a.Spec.Env, p.Spec.Env)
	engine, err := RenderEngineV1(ResolveEngine(a.Spec.Engine, p.Spec.Achagent.Engine), resolvedEnv)
	if err != nil {
		return WSConfig{}, err
	}
	limits, err := RenderLimitsV1(ResolveLimits(a.Spec.Limits, p.Spec.Achagent.Limits))
	if err != nil {
		return WSConfig{}, err
	}
	workspace, err := RenderWorkspaceV1(ResolveWorkspace(a.Spec.Workspace, p.Spec.Achagent.Workspace))
	if err != nil {
		return WSConfig{}, err
	}
	workspace.ReadyTimeoutSeconds = p.Spec.Execution.ReadyTimeoutSeconds // nil: runtime default
	artifacts, err := RenderArtifactsV1(ResolveArtifacts(a.Spec.Artifacts, p.Spec.Achagent.Artifacts))
	if err != nil {
		return WSConfig{}, err
	}
	infra := RenderInfrastructureV2(a.Name, a.Namespace)

	aliases := make(map[string]struct{})
	for _, ref := range ChannelSecretEnv(p, a) {
		if _, found := aliases[ref.EnvName]; found {
			return WSConfig{}, fmt.Errorf("duplicate generated channel secret env alias %q", ref.EnvName)
		}
		aliases[ref.EnvName] = struct{}{}
	}

	params, err := decodeParams(model.Params)
	if err != nil {
		return WSConfig{}, fmt.Errorf("model.params: %w", err)
	}
	var thinking *ThinkingBlock
	if model.Thinking != nil {
		thinking = &ThinkingBlock{Enabled: model.Thinking.Enabled, Effort: model.Thinking.Effort}
	}
	memory, err := renderMemoryV1(a.Spec.Memory)
	if err != nil {
		return WSConfig{}, err
	}

	cfg := WSConfig{
		SchemaVersion:  "workspace-v2",
		Agent:          WSAgentBlock{Name: a.Name, Namespace: a.Namespace, UID: string(a.UID)},
		Ach:            ach,
		Model:          ModelBlock{Name: model.Name, Type: model.Type, Params: params, Thinking: thinking},
		Engine:         engine,
		Limits:         limits,
		Workspace:      workspace,
		Artifacts:      artifacts,
		Prompt:         renderPrompt(a.Spec.Prompt),
		Hooks:          RenderHooksV1(a.Spec.Hooks),
		Memory:         memory,
		Infrastructure: infra,
	}
	for i := range a.Spec.Channels {
		cb, err := RenderChannelV1(&a.Spec.Channels[i], resolvedEnv)
		if err != nil {
			return WSConfig{}, err
		}
		cfg.Channels = append(cfg.Channels, cb)
	}

	version, err := ComputeConfigVersion(cfg)
	if err != nil {
		return WSConfig{}, fmt.Errorf("compute configVersion: %w", err)
	}
	cfg.ConfigVersion = version

	// Output-validation gate (Important review finding): defense in depth behind every
	// individual Render*V1 field check above — catches any wire-shape defect (null vs
	// array, an unsupported union variant, a missing required key) an individual mapper
	// missed, against the exact vendored schema. Fail closed: never apply an invalid config.
	if err := validateWorkspaceV2Output(cfg); err != nil {
		return WSConfig{}, err
	}
	return cfg, nil
}
