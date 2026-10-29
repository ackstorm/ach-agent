// SPDX-License-Identifier: MIT

package render

import (
	"fmt"

	corev1 "k8s.io/api/core/v1"

	v1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// This file implements the workspace-v1 wire mapping that render2.go's Render2 orchestrates
// (the retired Render()/AgentConfig agent-config-v1 shape, schemaVersion "1", is gone). Every
// mapping here is verified field-for-field against
// ../ach-agent/tests/config/fixtures/pr-review-runtime.json (vendored at
// testdata/pr-review-runtime.json) using a FULLY-RESOLVED input — i.e. every CRD field
// the wire schema requires is already set by the caller.
//
// configVersion (RFC 8785 JCS + SHA-256) is solved — see configversion.go, pinned exactly
// per config-canonicalization-proposal.md and verified against the full corpus.
//
// The vendored schema marks EVERY field of EngineBlock.compaction, LimitsBlock (all but
// maxConcurrentScripts), WorkspaceBlock, WorkspaceBlock.session, and ArtifactsBlock as
// REQUIRED with no CRD-level default. The functions below accept only fully-resolved Go
// values and error by field name when one is missing, rather than inventing a default —
// each one needs an explicit, documented default (CRD +kubebuilder:default or a required
// profile field), a product decision, not a renderer-internal guess.

// WSAgentIdentity is agent{name,namespace,uid} (schema AgentIdentity — fully required).
type WSAgentIdentity struct {
	Name      string `json:"name"`
	Namespace string `json:"namespace"`
	UID       string `json:"uid"`
}

// WSSecretAlias is the {env: NAME} alias shape used for ach.identity and secretEnv entries
// (schema SecretAlias — never a value, only the Pod env var name the operator injects).
type WSSecretAlias struct {
	Env string `json:"env"`
}

// AchKeyEnv is the fixed env var name for capability.ach.key, distinct from the per-channel
// generated aliases.
const AchKeyEnv = "ACH_SECRET_IDENTITY"

// WSFilterBlock/WSExcludeBlock mirror FilterBlock/ExcludeBlock but WITHOUT omitempty on
// the three list fields: the fixture always emits tools/mcpServers/skills explicitly
// (including an empty tools: []), matching how the Python producer serializes its model
// defaults. Neither is schema-required (AchCapabilityFilterExclude has no required list),
// so this is a byte-parity choice, not a contract requirement.
type WSFilterBlock struct {
	Exclude WSExcludeBlock `json:"exclude"`
}

type WSExcludeBlock struct {
	Tools      []string `json:"tools"`
	McpServers []string `json:"mcpServers"`
	Skills     []string `json:"skills"`
}

// emptyIfNil normalizes a nil exclusion slice to an empty (non-null) one: the vendored
// schema types tools/mcpServers/skills as arrays, never null, and the fixture always emits
// all three explicitly (byte-parity with the Python producer, WSExcludeBlock's own doc
// comment) — a filter that excludes only skills must not render tools/mcpServers as JSON
// null just because the CR left those two slices unset.
func emptyIfNil(s []string) []string {
	if s == nil {
		return []string{}
	}
	return s
}

// renderFilterV1 always renders the exclude gate with non-null arrays.
func renderFilterV1(f *v1.FilterSpec) WSFilterBlock {
	var e v1.ExcludeSpec
	if f != nil && f.Exclude != nil {
		e = *f.Exclude
	}
	return WSFilterBlock{Exclude: WSExcludeBlock{Tools: emptyIfNil(e.Tools), McpServers: emptyIfNil(e.McpServers), Skills: emptyIfNil(e.Skills)}}
}

// WSCompactionKeepBlock is engine.compaction.keep (schema CompactionKeepBlock — tokens
// required: the harness has no built-in default to fall back to on the wire).
type WSCompactionKeepBlock struct {
	Tokens int64 `json:"tokens"`
}

// WSCompactionBlock is engine.compaction (schema CompactionBlock — auto/keep/buffer all
// required).
type WSCompactionBlock struct {
	Auto   bool                  `json:"auto"`
	Keep   WSCompactionKeepBlock `json:"keep"`
	Buffer int64                 `json:"buffer"`
}

// WSEngineBlock is engine{env,startupTimeoutSeconds,compaction,denyTools} (schema EngineBlock —
// compaction required; env/startupTimeoutSeconds/denyTools optional).
type WSEngineBlock struct {
	Env                   map[string]string `json:"env,omitempty"`
	StartupTimeoutSeconds *int64            `json:"startupTimeoutSeconds,omitempty"`
	Compaction            WSCompactionBlock `json:"compaction"`
	DenyTools             []string          `json:"denyTools,omitempty"`
}

// RenderEngineV1 maps the resolved EngineSpec to the wire engine block. This is the field
// contract §11 calls out explicitly: "CR engine.forwardEnv se resuelve a JSON engine.env:
// {nombre: valorLiteral}" — a NAME LIST becomes a NAME->VALUE MAP, every value a literal
// already validated by validateEngineForwardEnv (never a secretKeyRef, never reserved,
// never absent from the merged env).
func RenderEngineV1(e *v1.EngineSpec, resolvedEnv []corev1.EnvVar) (WSEngineBlock, error) {
	if e == nil {
		return WSEngineBlock{}, fmt.Errorf("engine is required (workspace-v1: compaction has no default)")
	}
	if err := validateEngineForwardEnv(e, resolvedEnv); err != nil {
		return WSEngineBlock{}, err
	}
	if e.Compaction == nil || e.Compaction.Auto == nil || e.Compaction.Keep == nil || e.Compaction.Keep.Tokens == nil || e.Compaction.Buffer == nil {
		return WSEngineBlock{}, fmt.Errorf("engine.compaction.{auto,keep.tokens,buffer} are all required on the resolved wire value — no published harness default, so the profile (or agent) must set the whole block")
	}
	b := WSEngineBlock{
		StartupTimeoutSeconds: e.StartupTimeoutSeconds,
		Compaction:            WSCompactionBlock{Auto: *e.Compaction.Auto, Keep: WSCompactionKeepBlock{Tokens: *e.Compaction.Keep.Tokens}, Buffer: *e.Compaction.Buffer},
		DenyTools:             e.DenyTools,
	}
	if len(e.ForwardEnv) > 0 {
		env := indexEnv(resolvedEnv)
		b.Env = make(map[string]string, len(e.ForwardEnv))
		for _, name := range e.ForwardEnv {
			b.Env[name] = env[name].Value
		}
	}
	return b, nil
}

// WSLimitsBlock is limits{...} (schema LimitsBlock — every field but maxConcurrentScripts
// is required).
type WSLimitsBlock struct {
	MaxActiveWorkspaces      int64  `json:"maxActiveWorkspaces"`
	MaxConcurrentInvocations int64  `json:"maxConcurrentInvocations"`
	MaxConcurrentScripts     *int64 `json:"maxConcurrentScripts,omitempty"`
	MaxInvocationSeconds     int64  `json:"maxInvocationSeconds"`
	MaxQueuedTotal           int64  `json:"maxQueuedTotal"`
	IdempotencyWindowSeconds int64  `json:"idempotencyWindowSeconds"`
	MaxSteps                 int64  `json:"maxSteps"`
}

// RenderLimitsV1 maps the resolved LimitsSpec to the wire limits block. Every field but
// maxConcurrentScripts is required on the wire, but the CRD keeps them as pointers (an
// agent may override one limit without restating the rest — ResolveLimits per-field
// merge), so completeness is checked HERE, on the resolved result, by exact field name.
func RenderLimitsV1(l *v1.LimitsSpec) (WSLimitsBlock, error) {
	if l == nil {
		return WSLimitsBlock{}, fmt.Errorf("limits is required")
	}
	missing := map[string]bool{
		"maxActiveWorkspaces":      l.MaxActiveWorkspaces == nil,
		"maxConcurrentInvocations": l.MaxConcurrentInvocations == nil,
		"maxInvocationSeconds":     l.MaxInvocationSeconds == nil,
		"maxQueuedTotal":           l.MaxQueuedTotal == nil,
		"idempotencyWindowSeconds": l.IdempotencyWindowSeconds == nil,
		"maxSteps":                 l.MaxSteps == nil,
	}
	for name, isMissing := range missing {
		if isMissing {
			return WSLimitsBlock{}, fmt.Errorf("limits.%s is required on the resolved wire value (set it on the profile or the agent)", name)
		}
	}
	return WSLimitsBlock{
		MaxActiveWorkspaces: *l.MaxActiveWorkspaces, MaxConcurrentInvocations: *l.MaxConcurrentInvocations,
		MaxConcurrentScripts: l.MaxConcurrentScripts, MaxInvocationSeconds: *l.MaxInvocationSeconds,
		MaxQueuedTotal: *l.MaxQueuedTotal, IdempotencyWindowSeconds: *l.IdempotencyWindowSeconds, MaxSteps: *l.MaxSteps,
	}, nil
}

// WSPersistenceBlock is workspace.persistence / workspace.session.persistence (schema
// PersistenceBlock — enabled/retentionDays both required).
type WSPersistenceBlock struct {
	Enabled       bool  `json:"enabled"`
	RetentionDays int64 `json:"retentionDays"`
}

// WSSessionPolicyBlock is workspace.session (schema SessionPolicyBlock).
type WSSessionPolicyBlock struct {
	IdleTimeoutSeconds int64              `json:"idleTimeoutSeconds"`
	Persistence        WSPersistenceBlock `json:"persistence"`
}

// WSWorkspaceBlock is workspace{...} (schema WorkspaceBlock — fully required).
type WSWorkspaceBlock struct {
	IdleTimeoutSeconds     int64                `json:"idleTimeoutSeconds"`
	ShutdownTimeoutSeconds int64                `json:"shutdownTimeoutSeconds"`
	ReadyTimeoutSeconds    *int64               `json:"readyTimeoutSeconds,omitempty"`
	MaxConcurrentSessions  int64                `json:"maxConcurrentSessions"`
	Persistence            WSPersistenceBlock   `json:"persistence"`
	Session                WSSessionPolicyBlock `json:"session"`
}

func renderPersistenceV1(p *v1.WorkspacePersistenceSpec, field string) (WSPersistenceBlock, error) {
	if p == nil || p.Enabled == nil {
		return WSPersistenceBlock{}, fmt.Errorf("%s.enabled is required on the resolved wire value", field)
	}
	if p.RetentionDays == nil {
		return WSPersistenceBlock{}, fmt.Errorf("%s.retentionDays is required on the resolved wire value", field)
	}
	return WSPersistenceBlock{Enabled: *p.Enabled, RetentionDays: *p.RetentionDays}, nil
}

// RenderWorkspaceV1 maps the resolved WorkspaceSpec to the wire workspace block. The CRD
// keeps every field a pointer (per-field merge via ResolveWorkspace), so completeness of
// the wire-required fields is checked HERE, on the resolved result, by exact field name.
func RenderWorkspaceV1(w *v1.WorkspaceSpec) (WSWorkspaceBlock, error) {
	if w == nil {
		return WSWorkspaceBlock{}, fmt.Errorf("workspace is required")
	}
	if w.IdleTimeoutSeconds == nil || w.ShutdownTimeoutSeconds == nil || w.MaxConcurrentSessions == nil {
		return WSWorkspaceBlock{}, fmt.Errorf("workspace.{idleTimeoutSeconds,shutdownTimeoutSeconds,maxConcurrentSessions} are all required on the resolved wire value")
	}
	persist, err := renderPersistenceV1(w.Persistence, "workspace.persistence")
	if err != nil {
		return WSWorkspaceBlock{}, err
	}
	if w.Session == nil || w.Session.IdleTimeoutSeconds == nil {
		return WSWorkspaceBlock{}, fmt.Errorf("workspace.session.idleTimeoutSeconds is required on the resolved wire value")
	}
	sessPersist, err := renderPersistenceV1(w.Session.Persistence, "workspace.session.persistence")
	if err != nil {
		return WSWorkspaceBlock{}, err
	}
	return WSWorkspaceBlock{
		IdleTimeoutSeconds: *w.IdleTimeoutSeconds, ShutdownTimeoutSeconds: *w.ShutdownTimeoutSeconds,
		MaxConcurrentSessions: *w.MaxConcurrentSessions, Persistence: persist,
		Session: WSSessionPolicyBlock{IdleTimeoutSeconds: *w.Session.IdleTimeoutSeconds, Persistence: sessPersist},
	}, nil
}

// WSArtifactsBlock is artifacts{...} (schema ArtifactsBlock — fully required).
type WSArtifactsBlock struct {
	Enabled          bool  `json:"enabled"`
	MaxArtifactBytes int64 `json:"maxArtifactBytes"`
	RetentionDays    int64 `json:"retentionDays"`
}

// RenderArtifactsV1 maps the resolved ArtifactsSpec to the wire artifacts block. The CRD
// keeps maxArtifactBytes/retentionDays as pointers (per-field merge via ResolveArtifacts),
// so completeness is checked here, on the resolved result.
func RenderArtifactsV1(a *v1.ArtifactsSpec) (WSArtifactsBlock, error) {
	if a == nil || a.Enabled == nil || a.MaxArtifactBytes == nil || a.RetentionDays == nil {
		return WSArtifactsBlock{}, fmt.Errorf("artifacts.{enabled,maxArtifactBytes,retentionDays} are required on the resolved wire value")
	}
	return WSArtifactsBlock{Enabled: *a.Enabled, MaxArtifactBytes: *a.MaxArtifactBytes, RetentionDays: *a.RetentionDays}, nil
}

// WSRoutingBlock is channels[].routing (schema RoutingBlock — both fields optional; the
// channel's routing KEY itself is always present on the wire, null when no override, per
// the fixture).
type WSRoutingBlock struct {
	WorkspaceKey *string `json:"workspaceKey,omitempty"`
	SessionKey   *string `json:"sessionKey,omitempty"`
}

// RenderRoutingV1 maps RoutingSpec verbatim — no interpolation (contract §2: the operator
// never renders {{ }} templates, the Harness does).
func RenderRoutingV1(r *v1.RoutingSpec) *WSRoutingBlock {
	if r == nil {
		return nil
	}
	return &WSRoutingBlock{WorkspaceKey: r.WorkspaceKey, SessionKey: r.SessionKey}
}

// WSHandoffBlock is channels[].handoff (schema HandoffBlock — script+destination required).
type WSHandoffBlock struct {
	Script         string                   `json:"script"`
	Env            map[string]string        `json:"env,omitempty"`
	SecretEnv      map[string]WSSecretAlias `json:"secretEnv,omitempty"`
	TimeoutSeconds *int64                   `json:"timeoutSeconds,omitempty"`
	Destination    string                   `json:"destination"`
}

// renderPrepareV1 is the workspace-v1 strict counterpart to the retired legacy renderHook
// (render.go, removed with the old Render() path): a forwardEnv name absent from the merged
// environment is a configuration error (contract §11: check missing names before producing
// JSON), never a silent drop. (Not named renderHookV1: that name already denotes the
// unrelated hooks.sessionStart/.../sessionSuspend mapper.)
func renderPrepareV1(ch *v1.ChannelSpec, hook *v1.PrepareSpec, resolvedEnv []corev1.EnvVar, phase string) (*PrepareBlock, error) {
	if hook == nil {
		return nil, nil
	}
	out := &PrepareBlock{Script: hook.Script, TimeoutSeconds: hook.TimeoutSeconds}
	env := indexEnv(resolvedEnv)
	for _, name := range hook.ForwardEnv {
		e, ok := env[name]
		if !ok {
			return nil, fmt.Errorf("channels[%s].forwardEnv: %q is not set in the merged environment", ch.Name, name)
		}
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			if out.SecretEnv == nil {
				out.SecretEnv = map[string]SecretSourceBlock{}
			}
			out.SecretEnv[name] = SecretSourceBlock{Env: hookSecretEnvName(ch, phase, name)}
		} else {
			if out.Env == nil {
				out.Env = map[string]string{}
			}
			out.Env[name] = e.Value
		}
	}
	return out, nil
}

// RenderHandoffV1 reuses the strict env-forwarding resolution (renderHookV1) and adds the
// now-required destination.
func RenderHandoffV1(ch *v1.ChannelSpec, resolvedEnv []corev1.EnvVar) (*WSHandoffBlock, error) {
	if ch.Handoff == nil {
		return nil, nil
	}
	if ch.Handoff.Destination == "" {
		return nil, fmt.Errorf("channels[%s].handoff.destination is required", ch.Name)
	}
	legacy, err := renderPrepareV1(ch, &ch.Handoff.PrepareSpec, resolvedEnv, "HANDOFF")
	if err != nil {
		return nil, err
	}
	b := &WSHandoffBlock{Script: legacy.Script, Env: legacy.Env, TimeoutSeconds: legacy.TimeoutSeconds, Destination: ch.Handoff.Destination}
	if len(legacy.SecretEnv) > 0 {
		b.SecretEnv = make(map[string]WSSecretAlias, len(legacy.SecretEnv))
		for k, v := range legacy.SecretEnv {
			b.SecretEnv[k] = WSSecretAlias{Env: v.Env}
		}
	}
	return b, nil
}

// WSWebhookAuthBlock is channels[].webhook.auth (schema WebhookAuthBlock) — FLAT: env
// rides directly on the auth object, not nested under a "secret" key like the retired
// agent-config-v1 shape.
type WSWebhookAuthBlock struct {
	Type string `json:"type,omitempty"`
	Env  string `json:"env,omitempty"`
	// Header has no omitempty: the fixture always emits it (empty string when the auth
	// type carries no header name), matching the Python producer's style — neither is
	// schema-required, so this is byte-parity, not a contract requirement.
	Header string `json:"header"`
}

// WSWebhookBlock is channels[].webhook (schema WebhookBlock).
type WSWebhookBlock struct {
	Auth              WSWebhookAuthBlock `json:"auth"`
	GitlabEvents      []string           `json:"gitlabEvents,omitempty"`
	BotUsername       *string            `json:"botUsername,omitempty"`
	TriggerUsers      []string           `json:"triggerUsers,omitempty"`
	MergeRequestsOnly *bool              `json:"mergeRequestsOnly,omitempty"`
}

// RenderWebhookV1 maps WebhookSpec to the wire shape. ch is used only to derive the
// generated channelSecretEnvName alias (same deterministic name as the legacy renderer).
func RenderWebhookV1(ch *v1.ChannelSpec) *WSWebhookBlock {
	if ch.Webhook == nil {
		return nil
	}
	w := ch.Webhook
	b := &WSWebhookBlock{
		Auth:              WSWebhookAuthBlock{Type: w.Auth.Type, Header: w.Auth.Header},
		GitlabEvents:      w.GitlabEvents,
		BotUsername:       w.BotUsername,
		TriggerUsers:      w.TriggerUsers,
		MergeRequestsOnly: w.MergeRequestsOnly,
	}
	if w.Auth.SecretRef != nil {
		b.Auth.Env = channelSecretEnvName(ch)
	}
	return b
}

// WSHookBlock is one hooks.* entry (schema HookBlock — script required).
type WSHookBlock struct {
	Script         string `json:"script"`
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`
}

// WSHooksBlock is hooks{...} (schema HooksBlock). The three session keys are always present
// on the wire — the fixture renders unset hooks as explicit nulls, not an omitted key — so
// they intentionally have no omitempty. The two workspace keys are emitted only when set:
// runtimes <= 0.1.12 validate with extra="forbid", so even a null key would make them
// reject the config and crash-loop.
type WSHooksBlock struct {
	WorkspaceStart *WSHookBlock `json:"workspaceStart,omitempty"`
	WorkspaceStop  *WSHookBlock `json:"workspaceStop,omitempty"`
	SessionStart   *WSHookBlock `json:"sessionStart"`
	SessionRestore *WSHookBlock `json:"sessionRestore"`
	SessionSuspend *WSHookBlock `json:"sessionSuspend"`
}

func renderHookV1(h *v1.HookSpec) *WSHookBlock {
	if h == nil {
		return nil
	}
	return &WSHookBlock{Script: h.Script, TimeoutSeconds: h.TimeoutSeconds}
}

// RenderHooksV1 maps HooksSpec verbatim.
func RenderHooksV1(h *v1.HooksSpec) WSHooksBlock {
	if h == nil {
		return WSHooksBlock{}
	}
	return WSHooksBlock{
		WorkspaceStart: renderHookV1(h.WorkspaceStart),
		WorkspaceStop:  renderHookV1(h.WorkspaceStop),
		SessionStart:   renderHookV1(h.SessionStart),
		SessionRestore: renderHookV1(h.SessionRestore),
		SessionSuspend: renderHookV1(h.SessionSuspend),
	}
}
