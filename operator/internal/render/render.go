// SPDX-License-Identifier: MIT

package render

import (
	"encoding/json"
	"fmt"
	"maps"
	"slices"
	"strings"

	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"

	achv1alpha1 "github.com/ackstorm/ach-agent/operator/api/v1alpha1"
)

// Inbound channel types that carry an auth secret.
const (
	channelTypeWebhook       = "webhook"
	channelTypeWebhookScript = "webhook-script"
	channelTypeA2A           = "a2a"
)

// memoryAuthSecretEnvName is the fixed env var carrying the memory-backend auth
// secret. In the ACH_SECRET_ namespace, so validateEngineForwardEnv rejects it as a
// runtime-owned reserved name if an agent tries to forward it via engine.forwardEnv
// (sanitizeForwardEnv, which used to strip it automatically, is removed); collision-free
// vs ACH_SECRET_<CH>_<TYPE> (TYPE is never HINDSIGHT).
// memory.achMemory.auth arms. Textually identical to the prompt system type
// "ach" (promptSystemTypeAch) but a different domain — kept separate on purpose.
const (
	memoryTypeAchMemory  = "ach-memory"
	memoryAuthTypeAch    = "ach"
	memoryAuthTypeBearer = "bearer"
)

// promptSystemTypeAch is prompt.system.type=="ach" (an ACH-hosted Prompt CR).
const promptSystemTypeAch = "ach"

const memoryAuthSecretEnvName = "ACH_SECRET_MEMORY_AUTH" // #nosec G101 -- env var NAME, not a credential value

// channelSecretEnvName is the deterministic env var name carrying a channel's
// inbound-auth secret (webhook/a2a). Named ACH_SECRET_<CHANNEL>_<TYPE> (upper-
// snake, non-alnum → _). The name is NOT sensitive (only the value is, and the
// agent can't read the harness env); it just has to be a valid C identifier,
// unique (channel name is unique via listMapKey), and never collide with the
// reserved ACH_* vars (the ACH_SECRET_ prefix guarantees that).
func channelSecretEnvName(ch *achv1alpha1.ChannelSpec) string {
	return "ACH_SECRET_" + sanitizeEnvSegment(ch.Name) + "_" + sanitizeEnvSegment(ch.Type)
}

// hookSecretEnvName is the deterministic env var name carrying one rendered
// channel hook secretEnv entry. varName already passed the shell-env-name CRD
// pattern, so preserve its case: `token` and `TOKEN` are distinct variables.
func hookSecretEnvName(ch *achv1alpha1.ChannelSpec, phase, varName string) string {
	return "ACH_SECRET_" + sanitizeEnvSegment(ch.Name) + "_" + phase + "_" + varName
}

func sanitizeEnvSegment(s string) string {
	var b strings.Builder
	for _, r := range strings.ToUpper(s) {
		if (r >= 'A' && r <= 'Z') || (r >= '0' && r <= '9') {
			b.WriteRune(r)
		} else {
			b.WriteByte('_')
		}
	}
	return b.String()
}

// ChannelSecretEnvRef is one inbound-auth secret → container env var (secretKeyRef).
type ChannelSecretEnvRef struct {
	EnvName    string
	SecretName string
	Key        string
}

// ChannelSecretEnv returns generated aliases for channel auth and hook secrets.
// The operator wires each via secretKeyRef; rendered config references only EnvName.
func ChannelSecretEnv(p achv1alpha1.AgentProfile, a achv1alpha1.ACHAgent) []ChannelSecretEnvRef {
	var out []ChannelSecretEnvRef
	env := indexEnv(ResolveEnv(a.Spec.Env, p.Spec.Env))
	for i := range a.Spec.Channels {
		ch := &a.Spec.Channels[i]
		switch ch.Type {
		case channelTypeWebhook, channelTypeWebhookScript:
			if ch.Webhook != nil && ch.Webhook.Auth.SecretRef != nil {
				out = append(out, ChannelSecretEnvRef{EnvName: channelSecretEnvName(ch), SecretName: ch.Webhook.Auth.SecretRef.Name, Key: ch.Webhook.Auth.SecretRef.Key})
			}
		case channelTypeA2A:
			if ch.A2A != nil {
				out = append(out, ChannelSecretEnvRef{EnvName: channelSecretEnvName(ch), SecretName: ch.A2A.Auth.SecretRef.Name, Key: ch.A2A.Auth.SecretRef.Key})
			}
		}
		// Hook credentials need a generated alias so harness secret redaction cannot
		// strip an independently engine-forwarded original name.
		type namedHook struct {
			phase string
			spec  *achv1alpha1.PrepareSpec
		}
		hooks := []namedHook{{phase: "SCRIPT", spec: ch.Script}}
		if ch.Handoff != nil {
			hooks = append(hooks, namedHook{phase: "HANDOFF", spec: &ch.Handoff.PrepareSpec})
		}
		for _, hook := range hooks {
			if hook.spec == nil {
				continue
			}
			for _, name := range slices.Sorted(slices.Values(hook.spec.ForwardEnv)) {
				e, ok := env[name]
				if !ok || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
					continue
				}
				ref := e.ValueFrom.SecretKeyRef
				out = append(out, ChannelSecretEnvRef{EnvName: hookSecretEnvName(ch, hook.phase, name), SecretName: ref.Name, Key: ref.Key})
			}
		}
	}
	return out
}

// ResolveEnv merges profile defaults with agent overrides by name. Replacements stay
// in profile position and agent-only names append in agent order.
func ResolveEnv(agent, profile []corev1.EnvVar) []corev1.EnvVar {
	out := append([]corev1.EnvVar(nil), profile...)
	positions := make(map[string]int, len(out))
	for i := range out {
		positions[out[i].Name] = i
	}
	for _, e := range agent {
		if i, ok := positions[e.Name]; ok {
			out[i] = e
			continue
		}
		positions[e.Name] = len(out)
		out = append(out, e)
	}
	return out
}

func indexEnv(env []corev1.EnvVar) map[string]corev1.EnvVar {
	out := make(map[string]corev1.EnvVar, len(env))
	for _, e := range env {
		out[e.Name] = e
	}
	return out
}

// MemorySecretEnv returns the ach-memory user-key secret to inject via secretKeyRef.
// Bearer arm ONLY: the ach arm authenticates with the harness's own ek_, so there is no
// second secret to inject.
// or nil when the agent has no memory auth. Same wiring as channel secrets (env, not file).
func MemorySecretEnv(a achv1alpha1.ACHAgent) *ChannelSecretEnvRef {
	m := a.Spec.Memory
	if m == nil || m.Type != memoryTypeAchMemory || m.AchMemory == nil ||
		m.AchMemory.Auth == nil || m.AchMemory.Auth.Type != memoryAuthTypeBearer || m.AchMemory.Auth.SecretRef == nil {
		return nil
	}
	return &ChannelSecretEnvRef{EnvName: memoryAuthSecretEnvName, SecretName: m.AchMemory.Auth.SecretRef.Name, Key: m.AchMemory.Auth.SecretRef.Key}
}

// decodeParams decodes the CR-supplied raw model.params bytes with ordinary encoding/json
// object decoding — a duplicate key resolves to its last value, matching the Python
// producer's ordinary json.loads. The shared numeric profile (contract §5) is enforced once,
// later, when ComputeConfigVersion hashes the fully-resolved wire object this map becomes
// part of.
func decodeParams(raw *apiextensionsv1.JSON) (map[string]any, error) {
	if raw == nil || len(raw.Raw) == 0 {
		return nil, nil
	}
	var m map[string]any
	if err := json.Unmarshal(raw.Raw, &m); err != nil {
		return nil, fmt.Errorf("model.params: %w", err)
	}
	return m, nil
}

// ResolveAch deep-merges the agent's Ach block over the profile's shared ach defaults
// (AgentProfileSpec.Achagent.Ach: "shared defaults: baseUrl/environment/capability" per
// AgentDefaults' doc comment). Identity is NEVER taken from the profile — object-level CEL
// on AgentProfileSpec already forbids a profile from setting it, and this merge preserves
// that: the result always carries the AGENT's own Identity (nil when the agent itself has
// none, never backfilled from profile). BaseURL is resolved separately by
// ResolveAchBaseURL (which also consults the operator default) and is not part of this
// merge. Environment/Capability merge per field: the agent's value wins when explicitly
// set (Environment is a *string so an explicit empty string can clear an inherited
// non-empty value — distinct from "the agent didn't touch this field"), else the result
// inherits the profile's.
func ResolveAch(agent, profile *achv1alpha1.AchSpec) *achv1alpha1.AchSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *agent
	if agent.Environment == nil {
		out.Environment = profile.Environment
	}
	if agent.Capability == nil {
		out.Capability = profile.Capability
	}
	return &out
}

// reservedForwardEnvNames/reservedForwardEnvPrefixes mirror, name-for-name, the final
// corrected vendored schema policy (testdata/ach-workspace-config-v2.schema.json,
// EngineBlock.properties.env.propertyNames, ach-agent commits 3da2c6e/ceee54d): only names
// the runtime concretely owns — HOME, the five pinned OPENCODE_* bootstrap vars, and the
// ACH_/XDG_/OPENCODE_CONFIG prefixes. Root's binding correction rejected both an earlier
// broad schema-parity expansion (AWS/Azure/GCP/TLS/loader/proxy denylist) and the
// then-current vendored schema that still had it: a generic cloud-credential, TLS, loader,
// or proxy variable is now explicitly ALLOWED via forwardEnv — an operator's deliberate
// choice, not something this renderer blocks on the runtime's behalf. "No automatic
// inheritance" is enforced upstream (only literal values already present in the merged env
// can ever be selected; never a secretKeyRef), not by guessing sensitive names.
// render_test.go's TestReservedForwardEnvDenylist_MatchesVendoredSchema cross-checks both
// lists against the schema file so they cannot silently drift apart again.
var reservedForwardEnvNames = map[string]struct{}{
	"HOME":         {},
	"OPENCODE_BIN": {}, "OPENCODE_DISABLE_MODELS_FETCH": {}, "OPENCODE_DISABLE_PROJECT_CONFIG": {},
	"OPENCODE_MODELS_URL": {}, "OPENCODE_SERVER_PASSWORD": {},
}

var reservedForwardEnvPrefixes = []string{"ACH_", "XDG_", "OPENCODE_CONFIG"}

// validateEngineForwardEnv enforces contract §5: engine.forwardEnv selects ONLY literal
// values already present in the merged env — never a secretKeyRef source, never a reserved
// name/prefix, never a name absent from the merge. A violation is a configuration error, not
// a silent drop.
func validateEngineForwardEnv(e *achv1alpha1.EngineSpec, resolvedEnv []corev1.EnvVar) error {
	if e == nil || len(e.ForwardEnv) == 0 {
		return nil
	}
	env := indexEnv(resolvedEnv)
	for _, name := range e.ForwardEnv {
		for _, prefix := range reservedForwardEnvPrefixes {
			if strings.HasPrefix(name, prefix) {
				return fmt.Errorf("engine.forwardEnv: %q is reserved (%s* namespace)", name, prefix)
			}
		}
		if _, reserved := reservedForwardEnvNames[name]; reserved {
			return fmt.Errorf("engine.forwardEnv: %q is reserved", name)
		}
		e, ok := env[name]
		if !ok {
			return fmt.Errorf("engine.forwardEnv: %q is not set in spec.env", name)
		}
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			return fmt.Errorf("engine.forwardEnv: %q is a secretKeyRef — only literal values may be forwarded to the engine (contract §5)", name)
		}
	}
	return nil
}

func renderPrompt(p *achv1alpha1.AgentPromptSpec) *PromptBlock {
	if p == nil {
		return nil
	}
	sys := PromptSystemBlock{Type: p.System.Type}
	switch p.System.Type {
	case "text":
		sys.Text = p.System.Text
	case "file":
		sys.File = p.System.File
	case promptSystemTypeAch:
		sys.Ach = p.System.Ach
		sys.File = p.System.AchFile // legal: SystemAch allows optional file subpath
	}
	return &PromptBlock{System: sys, Compose: p.Compose}
}

func renderMemory(m *achv1alpha1.MemorySpec) *MemoryBlock {
	if m == nil {
		return nil
	}
	out := &MemoryBlock{Type: m.Type}
	switch m.Type {
	case memoryTypeAchMemory:
		if m.AchMemory != nil {
			ab := &AchMemoryBlock{
				Endpoint:    m.AchMemory.Endpoint,
				McpServerID: m.AchMemory.McpServerID,
				Project:     m.AchMemory.Project,
			}
			if auth := m.AchMemory.Auth; auth != nil {
				// env is the bearer arm's only field; the ach arm renders {type: ach} alone.
				ab.Auth = &AchMemoryAuthBlock{Type: auth.Type}
				if auth.Type == memoryAuthTypeBearer {
					ab.Auth.Env = memoryAuthSecretEnvName
					ab.Auth.Header = auth.Header
				}
			}
			out.AchMemory = ab
		}
	case "codemem":
		// codemem block is optional per schema; {"type":"codemem"} is valid.
		if m.Codemem != nil && (m.Codemem.DBPath != "" || m.Codemem.Project != "") {
			out.Codemem = &CodememBlock{DBPath: m.Codemem.DBPath, Project: m.Codemem.Project}
		}
	}
	return out
}

// ResolveImage is the per-agent image resolution: agent wins when set, else the
// profile's spec.achagent.image. Empty result blocks the agent (Render2 errors).
func ResolveImage(agent, profile string) string {
	if agent != "" {
		return agent
	}
	return profile
}

// ResolveModel deep-merges the agent model over the profile model per field.
// Params and Thinking are atomic: present on the agent → replace as a whole;
// omitted → inherit the profile's (deliberate: an agent that changes name/type
// but omits params inherits profile params). Result may alias profile memory —
// read-only.
func ResolveModel(agent, profile *achv1alpha1.ModelSpec) *achv1alpha1.ModelSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.Name != "" {
		out.Name = agent.Name
	}
	if agent.Type != "" {
		out.Type = agent.Type
	}
	if agent.Params != nil {
		out.Params = agent.Params
	}
	if agent.Thinking != nil {
		out.Thinking = agent.Thinking
	}
	return &out
}

// ResolveEngine deep-merges the agent engine over the profile engine per field. ForwardEnv
// is atomic (replace as a whole when present on the agent); Compaction merges per-field via
// ResolveCompaction, so an agent can flip e.g. just compaction.buffer. Result may alias
// profile memory — read-only.
func ResolveEngine(agent, profile *achv1alpha1.EngineSpec) *achv1alpha1.EngineSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.ForwardEnv != nil {
		out.ForwardEnv = agent.ForwardEnv
	}
	if agent.DenyTools != nil {
		out.DenyTools = agent.DenyTools
	}
	if agent.StartupTimeoutSeconds != nil {
		out.StartupTimeoutSeconds = agent.StartupTimeoutSeconds
	}
	out.Compaction = ResolveCompaction(agent.Compaction, profile.Compaction)
	return &out
}

// ResolveCompaction deep-merges per field; Keep is one atomic sub-block (its only field,
// tokens, has no independent meaning to override alone).
func ResolveCompaction(agent, profile *achv1alpha1.CompactionSpec) *achv1alpha1.CompactionSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.Auto != nil {
		out.Auto = agent.Auto
	}
	if agent.Keep != nil {
		out.Keep = agent.Keep
	}
	if agent.Buffer != nil {
		out.Buffer = agent.Buffer
	}
	return &out
}

// ResolveLimits deep-merges the agent limits over the profile limits per field, so an agent
// can override one limit (e.g. maxSteps) without restating the rest. MaxSteps is the one
// required-non-pointer field (a deliberate, separately-blessed exception): the agent's
// value wins whenever it is non-zero, since admission guarantees a present limits block
// always carries a positive maxSteps — a zero on the agent side means "the agent's Limits
// literal didn't set it", not an explicit override. Result may alias profile memory —
// read-only.
func ResolveLimits(agent, profile *achv1alpha1.LimitsSpec) *achv1alpha1.LimitsSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.MaxActiveWorkspaces != nil {
		out.MaxActiveWorkspaces = agent.MaxActiveWorkspaces
	}
	if agent.MaxConcurrentInvocations != nil {
		out.MaxConcurrentInvocations = agent.MaxConcurrentInvocations
	}
	if agent.MaxConcurrentScripts != nil {
		out.MaxConcurrentScripts = agent.MaxConcurrentScripts
	}
	if agent.MaxInvocationSeconds != nil {
		out.MaxInvocationSeconds = agent.MaxInvocationSeconds
	}
	if agent.MaxQueuedTotal != nil {
		out.MaxQueuedTotal = agent.MaxQueuedTotal
	}
	if agent.IdempotencyWindowSeconds != nil {
		out.IdempotencyWindowSeconds = agent.IdempotencyWindowSeconds
	}
	if agent.MaxSteps != nil {
		out.MaxSteps = agent.MaxSteps
	}
	return &out
}

// ResolvePersistence deep-merges per field: an agent overriding just Enabled must not lose
// the profile's RetentionDays (and vice versa) — the wholesale sub-block swap this replaces
// was exactly the Important review finding ("workspace.persistence: {enabled: false} loses
// inherited retentionDays").
func ResolvePersistence(agent, profile *achv1alpha1.WorkspacePersistenceSpec) *achv1alpha1.WorkspacePersistenceSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.Enabled != nil {
		out.Enabled = agent.Enabled
	}
	if agent.RetentionDays != nil {
		out.RetentionDays = agent.RetentionDays
	}
	return &out
}

// ResolveSession deep-merges workspace.session per field; Persistence resolves recursively
// via ResolvePersistence (same rationale: `session: {idleTimeoutSeconds: 0}` must not lose
// the profile's session.persistence).
func ResolveSession(agent, profile *achv1alpha1.WorkspaceSessionSpec) *achv1alpha1.WorkspaceSessionSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.IdleTimeoutSeconds != nil {
		out.IdleTimeoutSeconds = agent.IdleTimeoutSeconds
	}
	out.Persistence = ResolvePersistence(agent.Persistence, profile.Persistence)
	return &out
}

// ResolveWorkspace deep-merges per field, including the nested Persistence/Session
// sub-blocks (ResolvePersistence/ResolveSession) — NOT a wholesale sub-block swap, so a
// partial agent override of one leaf field inherits every sibling field from the profile.
func ResolveWorkspace(agent, profile *achv1alpha1.WorkspaceSpec) *achv1alpha1.WorkspaceSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.IdleTimeoutSeconds != nil {
		out.IdleTimeoutSeconds = agent.IdleTimeoutSeconds
	}
	if agent.ShutdownTimeoutSeconds != nil {
		out.ShutdownTimeoutSeconds = agent.ShutdownTimeoutSeconds
	}
	if agent.MaxConcurrentSessions != nil {
		out.MaxConcurrentSessions = agent.MaxConcurrentSessions
	}
	out.Persistence = ResolvePersistence(agent.Persistence, profile.Persistence)
	out.Session = ResolveSession(agent.Session, profile.Session)
	return &out
}

// ResolveArtifacts deep-merges per field, Enabled included (now a pointer): an agent
// overriding just MaxArtifactBytes/RetentionDays must not lose the profile's Enabled.
func ResolveArtifacts(agent, profile *achv1alpha1.ArtifactsSpec) *achv1alpha1.ArtifactsSpec {
	if agent == nil {
		return profile
	}
	if profile == nil {
		return agent
	}
	out := *profile
	if agent.Enabled != nil {
		out.Enabled = agent.Enabled
	}
	if agent.MaxArtifactBytes != nil {
		out.MaxArtifactBytes = agent.MaxArtifactBytes
	}
	if agent.RetentionDays != nil {
		out.RetentionDays = agent.RetentionDays
	}
	return &out
}

// ResolveAchBaseURL is the SINGLE ACH base-URL resolution: ACHAgent.spec.ach.baseUrl ??
// AgentProfile.spec.achagent.ach.baseUrl ?? operator default (ACH_BASE_URL). Empty
// result => the agent has no ACH to hydrate against and Render2 blocks it. Used
// for both the config capability.ach.baseUrl and the container ACH_BASE_URL env.
func ResolveAchBaseURL(agentAch, profileAch *achv1alpha1.AchSpec, envDefault string) string {
	if agentAch != nil && agentAch.BaseURL != "" {
		return agentAch.BaseURL
	}
	if profileAch != nil && profileAch.BaseURL != "" {
		return profileAch.BaseURL
	}
	return envDefault
}

// ReferencedSecrets returns env/channel-secret NAME → sorted KEYS for key checks,
// salted content hashing, and Secret watches. The ek identity Secret is handled separately.
func ReferencedSecrets(p achv1alpha1.AgentProfile, a achv1alpha1.ACHAgent) map[string][]string {
	set := map[string]map[string]struct{}{}
	add := func(name, key string) {
		if set[name] == nil {
			set[name] = map[string]struct{}{}
		}
		set[name][key] = struct{}{}
	}
	for _, e := range ResolveEnv(a.Spec.Env, p.Spec.Env) {
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			add(e.ValueFrom.SecretKeyRef.Name, e.ValueFrom.SecretKeyRef.Key)
		}
	}
	for i := range a.Spec.Channels {
		ch := &a.Spec.Channels[i]
		switch ch.Type {
		case channelTypeWebhook, channelTypeWebhookScript:
			if ch.Webhook != nil && ch.Webhook.Auth.SecretRef != nil {
				add(ch.Webhook.Auth.SecretRef.Name, ch.Webhook.Auth.SecretRef.Key)
			}
		case channelTypeA2A:
			if ch.A2A != nil {
				add(ch.A2A.Auth.SecretRef.Name, ch.A2A.Auth.SecretRef.Key)
			}
		}
	}
	if ref := MemorySecretEnv(a); ref != nil {
		add(ref.SecretName, ref.Key)
	}
	out := make(map[string][]string, len(set))
	for name, keys := range set {
		out[name] = slices.Sorted(maps.Keys(keys))
	}
	return out
}
