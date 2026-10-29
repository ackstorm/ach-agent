// SPDX-License-Identifier: MIT

package render

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"strings"

	corev1 "k8s.io/api/core/v1"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
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
func RenderQueueV1(q *v1.QueueSpec) *WSQueueBlock {
	if q == nil {
		return nil
	}
	return &WSQueueBlock{Type: "redis", Key: q.Key}
}

func RenderA2AV1(ch *v1.ChannelSpec) *WSA2ABlock {
	if ch.A2A == nil {
		return nil
	}
	return &WSA2ABlock{Mode: "async", Auth: WSA2AAuthBlock{Header: ch.A2A.Auth.Header, Env: channelSecretEnvName(ch)}}
}

// RenderScriptV1 maps channels[].script (schema ScriptBlock — shape-compatible with the
// existing PrepareBlock: script/env/secretEnv/timeoutSeconds, no scope or destination).
// Strict (renderHookV1): a forwardEnv name absent from the merged environment errors
// rather than silently vanishing from the rendered script block.
func RenderScriptV1(ch *v1.ChannelSpec, resolvedEnv []corev1.EnvVar) (*PrepareBlock, error) {
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
func RenderChannelV1(ch *v1.ChannelSpec, resolvedEnv []corev1.EnvVar) (WSChannelBlock, error) {
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

// WSInfrastructureBlock is the root infrastructure{facadeEndpoint} of workspace-v3. The
// runtime carries no Kubernetes or execution-unit field any more: the operator, as the
// host (contract v2 section 5), owns the control and execution infrastructure itself.
type WSInfrastructureBlock struct {
	FacadeEndpoint string `json:"facadeEndpoint"`
}

// controlPort is the fixed contract §11 control Service port. achagent_workload.go derives
// the same value independently (controlServicePort) since both must agree with the rendered
// controlEndpoint.
const controlPort = 8081

// ControlServiceAccountName is the effective control ServiceAccount: the profile's
// control.serviceAccountName when set, else ach-agent-control-<uid>.
func ControlServiceAccountName(agentUID string, p *v1.AgentProfile) string {
	if p.Spec.Control.ServiceAccountName != "" {
		return p.Spec.Control.ServiceAccountName
	}
	return "ach-agent-control-" + agentUID
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
// "agent-" never collides with the ach-agent-* names the operator derives (different leading
// bytes). The runtime's workspace names agent-<name part>-<20 hex>
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

// WSCapabilityBlock is capability{server, standalone|ach, filter} (schema CapabilityBlock).
type WSCapabilityBlock struct {
	Server     string        `json:"server"`
	Standalone *WSStandalone `json:"standalone,omitempty"`
	Ach        *WSAchServer  `json:"ach,omitempty"`
	Filter     WSFilterBlock `json:"filter"`
}

// WSStandalone is capability.standalone (schema StandaloneServer).
type WSStandalone struct {
	Model      WSStandaloneModel `json:"model"`
	McpServers []WSMcpServer     `json:"mcpServers"`
}

// WSHeader is one upstream header of the standalone model or an MCP server: exactly one of
// an env alias (a secret) or a literal value.
type WSHeader struct {
	Env   string  `json:"env,omitempty"`
	Value *string `json:"value,omitempty"`
}

// WSStandaloneModel is capability.standalone.model (schema StandaloneModel).
type WSStandaloneModel struct {
	Name     string              `json:"name"`
	Type     string              `json:"type"`
	BaseURL  string              `json:"baseUrl"`
	Headers  map[string]WSHeader `json:"headers"`
	Params   map[string]any      `json:"params"`
	Thinking *ThinkingBlock      `json:"thinking,omitempty"`
	Limits   *WSModelLimits      `json:"limits,omitempty"`
}

// WSModelLimits is capability.standalone.model.limits (schema ModelLimits).
type WSModelLimits struct {
	Context int64 `json:"context"`
	Output  int64 `json:"output"`
}

// WSMcpServer is one capability.standalone.mcpServers entry (schema StandaloneMcpServer).
type WSMcpServer struct {
	Name    string              `json:"name"`
	URL     string              `json:"url"`
	Headers map[string]WSHeader `json:"headers"`
}

// WSAchServer is capability.ach (schema AchServer).
type WSAchServer struct {
	BaseURL     string        `json:"baseUrl"`
	Environment string        `json:"environment"`
	Key         WSSecretAlias `json:"key"`
	Model       ModelBlock    `json:"model"`
}

// WSConfig is the full workspace-v3 wire document (schema root).
type WSConfig struct {
	SchemaVersion  string                `json:"schemaVersion"`
	Agent          WSAgentBlock          `json:"agent"`
	ConfigVersion  string                `json:"configVersion"`
	Capability     WSCapabilityBlock     `json:"capability"`
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

// renderMemoryV1 restricts the memory mapping to the workspace-v3 union: ach-memory or
// absent. In standalone mode the ach auth arm has no key to send and mcpServerId must name a
// standalone MCP server.
func renderMemoryV1(m *v1.MemorySpec, a *v1.Agent) (*MemoryBlock, error) {
	if m == nil {
		return nil, nil
	}
	if m.Type != memoryTypeAchMemory {
		return nil, fmt.Errorf("memory.type %q is not supported by workspace-v3 (only %q)", m.Type, memoryTypeAchMemory)
	}
	if st := a.Spec.Standalone; st != nil && m.AchMemory != nil {
		if m.AchMemory.Auth != nil && m.AchMemory.Auth.Type == memoryAuthTypeAch {
			return nil, fmt.Errorf("memory.achMemory.auth.type %q needs capabilityServer ach", memoryAuthTypeAch)
		}
		if id := m.AchMemory.McpServerID; id != "" {
			found := false
			for _, s := range st.McpServers {
				found = found || s.Name == id
			}
			if !found {
				return nil, fmt.Errorf("memory.achMemory.mcpServerId %q is not a standalone.mcpServers entry", id)
			}
		}
	}
	return renderMemory(m), nil
}

// DefaultModelType is the model wire format when none is stated.
const DefaultModelType = "openai"

func renderThinking(t *v1.ThinkingSpec) *ThinkingBlock {
	if t == nil {
		return nil
	}
	return &ThinkingBlock{Enabled: t.Enabled, Effort: t.Effort}
}

// renderCapability builds capability{} and the env bindings its header and key aliases need.
func renderCapability(a *v1.Agent) (WSCapabilityBlock, []EnvBinding, error) {
	cap := WSCapabilityBlock{Server: a.Spec.CapabilityServer, Filter: renderFilterV1(a.Spec.Filter)}
	switch a.Spec.CapabilityServer {
	case "ach":
		ach := a.Spec.Ach
		if ach == nil {
			return cap, nil, fmt.Errorf("capabilityServer ach needs spec.ach")
		}
		params, err := decodeParams(ach.Model.Params)
		if err != nil {
			return cap, nil, err
		}
		api := ach.Model.API
		if api == "" {
			api = DefaultModelType
		}
		cap.Ach = &WSAchServer{
			BaseURL: ach.BaseURL, Environment: ach.Environment, Key: WSSecretAlias{Env: AchKeyEnv},
			Model: ModelBlock{Name: ach.Model.Name, Type: api, Params: params, Thinking: renderThinking(ach.Model.Thinking)},
		}
		key := ach.KeySecretRef
		return cap, []EnvBinding{{Name: AchKeyEnv, SecretKeyRef: &key}}, nil
	case "standalone", "":
		cap.Server = "standalone"
		st := a.Spec.Standalone
		if st == nil {
			return cap, nil, fmt.Errorf("capabilityServer standalone needs spec.standalone")
		}
		m := st.Model
		if err := checkHeaderNames("standalone.model", m.Headers); err != nil {
			return cap, nil, err
		}
		params, err := decodeParams(m.Params)
		if err != nil {
			return cap, nil, err
		}
		if params == nil {
			params = map[string]any{}
		}
		headers, env := HeaderEnv("ACH_AGENT_MODEL", m.Headers)
		model := WSStandaloneModel{Name: m.Name, Type: m.API, BaseURL: m.BaseURL, Headers: headers, Params: params, Thinking: renderThinking(m.Thinking)}
		if model.Type == "" {
			model.Type = DefaultModelType
		}
		if m.Limits != nil {
			model.Limits = &WSModelLimits{Context: m.Limits.Context, Output: m.Limits.Output}
		}
		servers := make([]WSMcpServer, 0, len(st.McpServers))
		prefixes := map[string]string{}
		for _, srv := range st.McpServers {
			prefix := McpEnvPrefix(srv.Name)
			if other, ok := prefixes[prefix]; ok {
				return cap, nil, fmt.Errorf("mcp server %q and %q map to the same env prefix %s", other, srv.Name, prefix)
			}
			prefixes[prefix] = srv.Name
			if err := checkHeaderNames("standalone.mcpServers["+srv.Name+"]", srv.Headers); err != nil {
				return cap, nil, err
			}
			aliases, bindings := HeaderEnv(prefix, srv.Headers)
			servers = append(servers, WSMcpServer{Name: srv.Name, URL: srv.URL, Headers: aliases})
			env = append(env, bindings...)
		}
		cap.Standalone = &WSStandalone{Model: model, McpServers: servers}
		return cap, env, nil
	}
	return cap, nil, fmt.Errorf("unknown capabilityServer %q", a.Spec.CapabilityServer)
}

// Render collapses profile + agent into the full workspace-v3 WSConfig, including
// configVersion, plus the env bindings the control pod needs for header and key aliases.
// Fails closed (an error, never a fabricated value) on any wire-required policy block left
// unresolved (limits/engine.compaction/workspace/artifacts — CRD admission should already
// prevent this for any admitted object, this is defense in depth). Global Storage backend
// validation (ValidateRuntimeStorage) is a separate check the controller runs against the
// resolved WSConfig.
func Render(p v1.AgentProfile, a v1.Agent) (WSConfig, []EnvBinding, error) {
	capability, bindings, err := renderCapability(&a)
	if err != nil {
		return WSConfig{}, nil, err
	}
	resolvedEnv := ResolveEnv(a.Spec.Env, p.Spec.Control.Env)
	engine, err := RenderEngineV1(ResolveEngine(a.Spec.Engine, p.Spec.Defaults.Engine), resolvedEnv)
	if err != nil {
		return WSConfig{}, nil, err
	}
	limits, err := RenderLimitsV1(ResolveLimits(a.Spec.Limits, p.Spec.Defaults.Limits))
	if err != nil {
		return WSConfig{}, nil, err
	}
	workspace, err := RenderWorkspaceV1(ResolveWorkspace(a.Spec.Workspace, p.Spec.Defaults.Workspace))
	if err != nil {
		return WSConfig{}, nil, err
	}
	workspace.ReadyTimeoutSeconds = p.Spec.Sandbox.OpenShell.ReadyTimeoutSeconds // nil: runtime default
	artifacts, err := RenderArtifactsV1(ResolveArtifacts(a.Spec.Artifacts, p.Spec.Defaults.Artifacts))
	if err != nil {
		return WSConfig{}, nil, err
	}

	aliases := make(map[string]struct{})
	for _, ref := range ChannelSecretEnv(p, a) {
		if _, found := aliases[ref.EnvName]; found {
			return WSConfig{}, nil, fmt.Errorf("duplicate generated channel secret env alias %q", ref.EnvName)
		}
		aliases[ref.EnvName] = struct{}{}
	}
	memory, err := renderMemoryV1(a.Spec.Memory, &a)
	if err != nil {
		return WSConfig{}, nil, err
	}

	cfg := WSConfig{
		SchemaVersion:  "workspace-v3",
		Agent:          WSAgentBlock{Name: a.Name, Namespace: a.Namespace, UID: string(a.UID)},
		Capability:     capability,
		Engine:         engine,
		Limits:         limits,
		Workspace:      workspace,
		Artifacts:      artifacts,
		Prompt:         renderPrompt(a.Spec.Prompt),
		Hooks:          RenderHooksV1(a.Spec.Hooks),
		Memory:         memory,
		Infrastructure: RenderInfrastructureV2(a.Name, a.Namespace),
	}
	for i := range a.Spec.Channels {
		cb, err := RenderChannelV1(&a.Spec.Channels[i], resolvedEnv)
		if err != nil {
			return WSConfig{}, nil, err
		}
		cfg.Channels = append(cfg.Channels, cb)
	}

	version, err := ComputeConfigVersion(cfg)
	if err != nil {
		return WSConfig{}, nil, fmt.Errorf("compute configVersion: %w", err)
	}
	cfg.ConfigVersion = version

	// Output-validation gate: defense in depth behind every individual Render*V1 field
	// check above, against the exact vendored schema. Fail closed.
	if err := validateWorkspaceOutput(cfg); err != nil {
		return WSConfig{}, nil, err
	}
	return cfg, bindings, nil
}
