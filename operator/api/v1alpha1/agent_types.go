// SPDX-License-Identifier: MIT

package v1alpha1

import (
	corev1 "k8s.io/api/core/v1"
	apiextensionsv1 "k8s.io/apiextensions-apiserver/pkg/apis/apiextensions/v1"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
)

// ExcludeSpec is the governance gate ABOVE the model (config: capability.filter.exclude).
type ExcludeSpec struct {
	// +optional
	Tools []string `json:"tools,omitempty"`
	// +optional
	McpServers []string `json:"mcpServers,omitempty"`
	// +optional
	Skills []string `json:"skills,omitempty"`
}

// FilterSpec wraps the exclude gate.
type FilterSpec struct {
	// +optional
	Exclude *ExcludeSpec `json:"exclude,omitempty"`
}

// PromptSystemSpec is a discriminated persona source (config: prompt.system).
// The ach form MAY carry an optional achFile (rendered as system.file — the schema's SystemAch
// allows `file` as an optional subpath within the named prompt).
// +kubebuilder:validation:XValidation:rule="(self.type=='text' && has(self.text)) || (self.type=='file' && has(self.file)) || (self.type=='ach' && has(self.ach))",message="prompt.system: the block matching type is required"
type PromptSystemSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=text;file;ach
	Type string `json:"type"`
	// +optional
	Text string `json:"text,omitempty"`
	// +optional
	File string `json:"file,omitempty"`
	// +optional
	Ach string `json:"ach,omitempty"`
	// AchFile is an optional subpath within an `ach` prompt (rendered as prompt.system.file).
	// +optional
	AchFile string `json:"achFile,omitempty"`
}

// AgentPromptSpec configures the system prompt (config: prompt).
type AgentPromptSpec struct {
	// +kubebuilder:validation:Required
	System PromptSystemSpec `json:"system"`
	// +optional
	// +kubebuilder:validation:Enum=replace;append
	// +kubebuilder:default=append
	Compose string `json:"compose,omitempty"`
}

// AchMemoryAuthSpec selects HOW the harness authenticates to ach-memory
// (config: memory.achMemory.auth). Omit the whole block for an internal URL that
// needs no auth header at all.
//
// The two arms carry different credentials, and the choice is not cosmetic:
//   - ach    → the harness sends its OWN ek_ as ACH's `x-ach-key` to ACH's MCP
//     gateway, which forwards to LiteLLM and resolves the principal. There is no
//     second credential, so this arm takes no secretRef. `Authorization: Bearer`
//     is NOT interchangeable — ACH's scheme is the header, and a Bearer 401s.
//   - bearer → a token in an env var, for talking to ach-memory directly. Needs
//     the usual secretKeyRef plumbing, plus a `header` naming WHICH of
//     ach-memory's two identity providers the token is for.
//
// +kubebuilder:validation:XValidation:rule="self.type!='bearer' || has(self.secretRef)",message="memory.achMemory.auth.secretRef is required when type=bearer"
// +kubebuilder:validation:XValidation:rule="self.type!='ach' || !has(self.secretRef)",message="memory.achMemory.auth.secretRef is meaningless when type=ach (the harness sends its own ek_)"
// +kubebuilder:validation:XValidation:rule="self.type!='ach' || !has(self.header)",message="memory.achMemory.auth.header is meaningless when type=ach (the harness sends its own ek_ as x-ach-key)"
type AchMemoryAuthSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=ach;bearer
	Type string `json:"type"`
	// SecretRef is the ach-memory user key, bearer arm only. Same env-only mechanism
	// as webhook/a2a: the operator injects the value into the pod from this Secret and
	// renders only the env NAME. NOTE: the key that first bootstraps a project OWNS it —
	// rotating this to a DIFFERENT ach-memory user orphans the bank.
	// +optional
	SecretRef *SecretKeyRef `json:"secretRef,omitempty"`
	// Header names the request header the token rides on, bearer arm only. It picks
	// WHICH of ach-memory's two identity providers you are talking to, and therefore
	// what the token must BE — ach-memory mints no credentials of its own:
	//   - "Authorization" (the harness default when this is empty) → the JWT provider.
	//     The token must be a JWT that provider's issuer signed. Sent as `Bearer <token>`.
	//   - anything else, e.g. "x-litellm-api-key" → the platform provider, whose header
	//     name that deployment sets. The token is whatever its resolver can name, and it
	//     is sent RAW — the `Bearer` scheme word belongs to Authorization, and a resolver
	//     forwarding the value verbatim would otherwise get it as part of the key.
	// Get this wrong and the 401 is INVISIBLE: memory is fail-open, so a refused
	// credential surfaces as memory silently never working, not as an error.
	// Constrained to an RFC 9110 field-name token so a CRLF injection is refused here
	// rather than by the harness's HTTP client.
	// +optional
	// +kubebuilder:validation:Pattern="^[A-Za-z0-9!#$%&'*+.^_`|~-]+$"
	// +kubebuilder:validation:MaxLength=64
	Header string `json:"header,omitempty"`
}

// AchMemorySpec is the ach-memory memory backend (config: memory.achMemory).
//
// +kubebuilder:validation:XValidation:rule="has(self.endpoint) != has(self.mcpServerId)",message="memory.achMemory: set exactly one of endpoint or mcpServerId"
type AchMemorySpec struct {
	// Endpoint is the COMPLETE MCP endpoint, rendered VERBATIM — the harness appends
	// nothing, not `/mcp`, not a trailing slash. Whether ach-memory sits at a root
	// (`https://memory.internal/mcp/`) or behind ACH's gateway
	// (`https://api.ackstorm.ai/mcp/ach-memory`) is the operator's call. Do NOT append a
	// path here: a client that appends its own is how requests end up at `/mcp/mcp/`.
	// Use this for a deployment with no ACH manifest to resolve against; otherwise
	// prefer mcpServerId, which cannot drift and closes the second path.
	// +optional
	// +kubebuilder:validation:MinLength=1
	Endpoint string `json:"endpoint,omitempty"`
	// McpServerID names the hydrated runtime.mcpServers[].id serving ach-memory, as an
	// ALTERNATIVE to endpoint: the harness reads the address out of the same manifest
	// ACH granted, so it cannot drift from it.
	//
	// It ALSO EXCLUDES that server from the harness's MCP proxy, and that is the point.
	// With endpoint, an Environment that ALSO grants ach-memory hands the agent the same
	// service by TWO paths — the harness facade, which pins scope and injects
	// project_slug beneath the agent, and the proxied server, where project_slug is an
	// ordinary argument and the ek_ is attached. The facade's containment is then merely
	// advisory: the agent reaches another tenant's bank by calling the copy next to it.
	// Naming the id is what lets the harness close that second path.
	//
	// Not admission-validated: capability.environment may resolve in another cluster,
	// and the operator holds no ek_, so it cannot check that this id exists. Not
	// hydrated ⇒ the harness runs with NO memory (fail-open §6.5), never a guessed URL.
	// +optional
	// +kubebuilder:validation:MinLength=1
	McpServerID string `json:"mcpServerId,omitempty"`
	// +optional
	Auth *AchMemoryAuthSpec `json:"auth,omitempty"`
	// Project overrides the memory-bank slug. Empty (the norm) → the harness derives
	// {POD_NAMESPACE}-{agent.name} at boot, one bank per agent. Static: the slug SELECTS a
	// bank, so a payload-derived one would let an inbound event pick which bank the agent
	// reads and writes — the harness rejects {{ }} here, and so does the CEL below.
	// +optional
	// +kubebuilder:validation:XValidation:rule="!self.contains('{{')",message="memory.achMemory.project must be static — templating ({{ }}) is not allowed"
	Project string `json:"project,omitempty"`
}

// MemorySpec is the memory backend (config: memory). Omit for no memory (fail-open). The
// achMemory block is required (its endpoint has no default).
// +kubebuilder:validation:XValidation:rule="self.type=='ach-memory' && has(self.achMemory)",message="memory.achMemory is required when type=ach-memory"
type MemorySpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=ach-memory
	Type string `json:"type"`
	// +optional
	AchMemory *AchMemorySpec `json:"achMemory,omitempty"`
}

// WebhookAuthSpec configures webhook auth (config: channels[].webhook.auth; secretRef → secretPath).
// +kubebuilder:validation:XValidation:rule="self.type=='none' || has(self.secretRef)",message="webhook.auth.secretRef is required unless type=none"
// +kubebuilder:validation:XValidation:rule="self.type!='header_token' || (has(self.header) && size(self.header)>0)",message="webhook.auth.header is required when type=header_token"
type WebhookAuthSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=gitlab_token;hmac;header_token;none
	Type string `json:"type"`
	// +optional
	Header string `json:"header,omitempty"`
	// +optional
	SecretRef *SecretKeyRef `json:"secretRef,omitempty"`
}

// WebhookSpec configures webhook ingress for model and deterministic-script channels.
type WebhookSpec struct {
	// +kubebuilder:validation:Required
	Auth WebhookAuthSpec `json:"auth"`
	// +optional
	// +kubebuilder:validation:items:Enum=merge_request;issue;note;push;project_create;project_rename;project_transfer;project_update;repository_update
	GitlabEvents []string `json:"gitlabEvents,omitempty"`
	// BotUsername is the GitLab username the agent posts AS (the egress PAT's
	// user — a distinct fact from the agent name). When set, the harness drops
	// inbound events authored by this user plus gitlab-generated system notes
	// pre-enqueue (loop-guard). Omit → guard off. gitlab source only; ignored
	// for github/generic. Rendered verbatim to channels[].webhook.botUsername.
	// +optional
	BotUsername *string `json:"botUsername,omitempty"`
	// TriggerUsers is an actor allowlist: only these GitLab usernames may trigger
	// the handler. Omit → any author triggers. System events without a username are
	// rejected when this list is set, so webhook-script registrars should omit it.
	// GitLab source only; ignored for github/generic.
	// +optional
	TriggerUsers []string `json:"triggerUsers,omitempty"`
	// MergeRequestsOnly discards GitLab issues and notes not on a merge request before
	// admission (contract §2). gitlab source only; ignored for github/generic.
	// +optional
	MergeRequestsOnly *bool `json:"mergeRequestsOnly,omitempty"`
}

// CronSpec configures a cron channel (config: channels[].cron).
type CronSpec struct {
	// +kubebuilder:validation:Required
	Schedule string `json:"schedule"`
	// +optional
	// +kubebuilder:default=UTC
	Timezone string `json:"timezone,omitempty"`
}

// QueueSpec configures a redis queue channel (config: channels[].queue; type/ackMode are constants).
type QueueSpec struct {
	// +kubebuilder:validation:Required
	Key string `json:"key"`
}

// A2AAuthSpec configures a2a inbound auth (config: channels[].a2a.auth; secretRef → secretPath).
type A2AAuthSpec struct {
	// +optional
	// +kubebuilder:default=x-a2a-custom-api-key
	Header string `json:"header,omitempty"`
	// +kubebuilder:validation:Required
	SecretRef SecretKeyRef `json:"secretRef"`
}

// A2ASpec configures an a2a channel (config: channels[].a2a; mode async-only in v1).
type A2ASpec struct {
	// +kubebuilder:validation:Required
	Auth A2AAuthSpec `json:"auth"`
}

// RoutingSpec overrides the Harness's default Workspace/Session identity for a channel
// (config: channels[].routing, contract §2). Both fields are optional and independent: an
// omitted one keeps the adapter's default. A present one is a non-empty {{ }} template
// string (event.*, payload.*) the Harness renders — the operator never interprets or
// renders it; an invalid render is rejected by the Harness with no fallback. MinLength (an
// OpenAPI structural keyword, not CEL) enforces non-empty cheaply — only present values are
// checked, so the field stays genuinely optional.
type RoutingSpec struct {
	// WorkspaceKey overrides the adapter's default Workspace identity template. The
	// reserved keyword "session" gives each session its own workspace.
	// +optional
	// +kubebuilder:validation:MinLength=1
	WorkspaceKey *string `json:"workspaceKey,omitempty"`
	// SessionKey overrides the adapter's default Session identity template.
	// +optional
	// +kubebuilder:validation:MinLength=1
	SessionKey *string `json:"sessionKey,omitempty"`
}

// PrepareSpec configures a static /bin/sh program used by prepare, cleanup, and the
// deterministic webhook-script handler. The harness does not render {{ }} in scripts;
// event data reaches them as environment variables, and webhook-script additionally
// receives normalized JSON on stdin.
type PrepareSpec struct {
	// Script is the static /bin/sh program. Lifecycle hooks feed it to `sh -eu -s`;
	// webhook-script uses `sh -eu -c` so stdin remains available for webhook JSON.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Script string `json:"script"`
	// ForwardEnv selects names from the merged AgentProfile.spec.control.env + Agent.spec.env.
	// Literal values become the hook's env; secretKeyRef values become its secretEnv via
	// generated Pod aliases. Unknown names are ignored and remain unset.
	// +optional
	// +listType=set
	// +kubebuilder:validation:items:Pattern=`^[A-Za-z_][A-Za-z0-9_]*$`
	ForwardEnv []string `json:"forwardEnv,omitempty"`
	// TimeoutSeconds bounds the hook script; on expiry the harness SIGKILLs its process
	// group. Harness default is 120 when omitted.
	// +optional
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3600
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`
}

// HandoffSpec configures channels[].handoff: a credentialed harness script that runs in an
// empty $ACH_HANDOFF_DIR; its output replaces the session workspace's handoff/ directory.
// Replaces the old prepare/cleanup pair.
type HandoffSpec struct {
	PrepareSpec `json:",inline"`
	// Destination is the relative path inside the Workspace the handoff's output replaces
	// (contract §6). Required, non-empty. Path-escape rejection (no .. segments, no leading
	// /) happens at runtime (Harness) — a CEL equivalent here is prohibitively expensive
	// against the per-channel listType=map cost multiplier.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Destination string `json:"destination"`
}

// ChannelSpec is one inbound channel (config: channels[]).
// +kubebuilder:validation:XValidation:rule="((self.type=='webhook' || self.type=='webhook-script') && has(self.webhook)) || (self.type=='cron' && has(self.cron)) || (self.type=='queue' && has(self.queue)) || (self.type=='a2a' && has(self.a2a))",message="channels: the block matching type is required"
// +kubebuilder:validation:XValidation:rule="self.type=='webhook' || self.type=='webhook-script' || !has(self.source)",message="channels.source is only valid for webhook channels"
// +kubebuilder:validation:XValidation:rule="self.type=='webhook-script' ? has(self.script) : !has(self.script)",message="channels.script is required only for webhook-script"
// +kubebuilder:validation:XValidation:rule="self.type!='webhook-script' || (!has(self.prompt) && !has(self.handoff))",message="webhook-script forbids prompt and handoff"
// +kubebuilder:validation:XValidation:rule="!(self.type=='queue' || (self.type=='webhook' && has(self.source) && self.source=='generic')) || (has(self.routing) && has(self.routing.sessionKey))",message="generic webhook and queue channels require routing.sessionKey"
type ChannelSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=webhook;webhook-script;cron;queue;a2a
	Type string `json:"type"`
	// +optional
	// +kubebuilder:validation:Enum=gitlab;github;generic
	Source string `json:"source,omitempty"`
	// +optional
	// +kubebuilder:default=1
	// +kubebuilder:validation:Minimum=1
	Concurrency *int64 `json:"concurrency,omitempty"`
	// Routing overrides the Harness's default Workspace/Session identity for this
	// channel (contract §2). Omit for the adapter's default.
	// +optional
	Routing *RoutingSpec `json:"routing,omitempty"`
	// +optional
	Prompt string `json:"prompt,omitempty"`
	// +optional
	Webhook *WebhookSpec `json:"webhook,omitempty"`
	// +optional
	Cron *CronSpec `json:"cron,omitempty"`
	// +optional
	Queue *QueueSpec `json:"queue,omitempty"`
	// +optional
	A2A *A2ASpec `json:"a2a,omitempty"`
	// Handoff runs in the harness, in an empty directory; its output wholesale-replaces the
	// session workspace's handoff/ (content-agnostic — a repo clone, a DB extract, arbitrary
	// files). It runs every invocation; for per-session files set
	// routing.workspaceKey: session. Valid for every channel type.
	// +optional
	Handoff *HandoffSpec `json:"handoff,omitempty"`
	// Script is the deterministic handler for type=webhook-script. The normalized webhook
	// JSON is passed on stdin; the harness never invokes the agent engine. Its workspace is
	// temporary and removed after each event.
	// +optional
	Script *PrepareSpec `json:"script,omitempty"`
}

// HookSpec is a script run inside the agent's execution environment (the mini-harness),
// with only engine.forwardEnv variables. It carries no forwardEnv of its own: a hook never
// gets channel credentials.
type HookSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Script string `json:"script"`
	// +optional
	// +kubebuilder:validation:Minimum=1
	// +kubebuilder:validation:Maximum=3600
	TimeoutSeconds *int64 `json:"timeoutSeconds,omitempty"`
}

// HooksSpec configures the agent's workspace and session lifecycle hooks. Agent-level
// only: hooks describe one agent's behaviour and are not part of AgentProfile/AgentDefaults.
type HooksSpec struct {
	// WorkspaceStart runs once in the execution pod after the workspace is restored (or
	// created), before any session. Best-effort: a failure raises the alarm
	// workspace_start_failed and never blocks. Requires ach-runtime >= 0.1.13.
	// +optional
	WorkspaceStart *HookSpec `json:"workspaceStart,omitempty"`
	// WorkspaceStop runs once before the workspace snapshot/close. Best-effort: a failure
	// raises the alarm workspace_stop_failed. Requires ach-runtime >= 0.1.13.
	// +optional
	WorkspaceStop *HookSpec `json:"workspaceStop,omitempty"`
	// SessionStart runs once per new session, after the handoff and before the first turn.
	// It is a run gate: exit 0 appends its stdout to the prompt, exit 78 skips the run
	// without an engine call, and any other non-zero exit fails the invocation.
	// +optional
	SessionStart *HookSpec `json:"sessionStart,omitempty"`
	// SessionRestore runs once each time a session is restored from a snapshot (contract §6).
	// Failure fails the invocation. Never runs alongside SessionStart for the same session —
	// Start is create-only, Restore is restore-only.
	// +optional
	SessionRestore *HookSpec `json:"sessionRestore,omitempty"`
	// SessionSuspend runs every time the session's engine stops (idle, shutdown, sandbox
	// suspend), before any HOME archive. May run many times per session. Best-effort.
	// +optional
	SessionSuspend *HookSpec `json:"sessionSuspend,omitempty"`
}

// HeaderValue is one HTTP header value: a Secret key or a literal.
// +kubebuilder:validation:XValidation:rule="has(self.secretKeyRef) != has(self.value)",message="exactly one of secretKeyRef or value must be set"
type HeaderValue struct {
	// +optional
	SecretKeyRef *SecretKeyRef `json:"secretKeyRef,omitempty"`
	// +optional
	Value *string `json:"value,omitempty"`
}

// ModelLimitsSpec states the model's context and output windows when the endpoint does not
// advertise them.
type ModelLimitsSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Minimum=1
	Context int64 `json:"context"`
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Minimum=1
	Output int64 `json:"output"`
}

// StandaloneModelSpec is the model endpoint of a standalone Agent. Every header is stated by
// the user; there are no defaults per api.
type StandaloneModelSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// API is the wire format of the endpoint.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Enum=openai;anthropic;gemini
	API string `json:"api"`
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https?://[^/\s]+`
	BaseURL string `json:"baseUrl"`
	// Headers are sent to the endpoint by the control pod; the sandbox never sees them. Header
	// names are case-insensitive and unique; host, content-length, transfer-encoding,
	// connection, traceparent, tracestate, x-litellm-session-id, x-litellm-trace-id,
	// x-ach-* and langfuse_* are reserved (rejected when the Agent is rendered).
	// +optional
	// +kubebuilder:validation:MaxProperties=64
	Headers map[string]HeaderValue `json:"headers,omitempty"`
	// Params is an open, unvalidated dict splatted to the model client.
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Params *apiextensionsv1.JSON `json:"params,omitempty"`
	// +optional
	Thinking *ThinkingSpec `json:"thinking,omitempty"`
	// +optional
	Limits *ModelLimitsSpec `json:"limits,omitempty"`
}

// StandaloneMcpServerSpec is one remote MCP server of a standalone Agent.
type StandaloneMcpServerSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[a-z0-9][a-z0-9_-]{0,62}$`
	Name string `json:"name"`
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https?://[^/\s]+`
	URL string `json:"url"`
	// +optional
	// +kubebuilder:validation:MaxProperties=64
	Headers map[string]HeaderValue `json:"headers,omitempty"`
}

// StandaloneSpec is the capability source of an Agent that talks to its own model and MCP
// servers.
type StandaloneSpec struct {
	// +kubebuilder:validation:Required
	Model StandaloneModelSpec `json:"model"`
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:MaxItems=64
	McpServers []StandaloneMcpServerSpec `json:"mcpServers,omitempty"`
}

// AchModelSpec selects the ACH-served model.
type AchModelSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// API is the wire format of the ACH model route.
	// +kubebuilder:default=openai
	// +kubebuilder:validation:Enum=openai;anthropic;gemini
	API string `json:"api,omitempty"`
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	Params *apiextensionsv1.JSON `json:"params,omitempty"`
	// +optional
	Thinking *ThinkingSpec `json:"thinking,omitempty"`
}

// AchTokenExchange exchanges the control pod's projected ServiceAccount token for a
// short-lived ACH credential. ACH issues it only when a KeyGrant names this identity, the
// Environment and the pod's control ServiceAccount as <namespace>/<serviceaccount>.
type AchTokenExchange struct {
	// Identity is the ACH identity (an email) the credential acts as.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:Pattern=`^[^@\s*?\[\]]+@[^@\s*?\[\]]+$`
	Identity string `json:"identity"`
}

// AchKeyFrom is where the ACH credential comes from: exactly one of tokenExchange or
// secretKeyRef.
// +kubebuilder:validation:XValidation:rule="has(self.tokenExchange) != has(self.secretKeyRef)",message="ach.keyFrom: set exactly one of tokenExchange or secretKeyRef"
type AchKeyFrom struct {
	// +optional
	TokenExchange *AchTokenExchange `json:"tokenExchange,omitempty"`
	// SecretKeyRef is a Secret holding a static ek_.
	// +optional
	SecretKeyRef *SecretKeyRef `json:"secretKeyRef,omitempty"`
}

// AchServerSpec is the capability source of an Agent hydrated from ACH.
// +kubebuilder:validation:XValidation:rule="!has(self.keyFrom.tokenExchange) || (has(self.environment) && size(self.environment) > 0)",message="ach.environment is required with keyFrom.tokenExchange"
// +kubebuilder:validation:XValidation:rule="!has(self.keyFrom.tokenExchange) || self.baseUrl.startsWith('https://')",message="ach.baseUrl must be https:// with keyFrom.tokenExchange"
type AchServerSpec struct {
	// BaseURL is ACH's public URL. With tokenExchange it is also the projected token's
	// audience (trailing slashes removed) and must equal ACH's ACH_BASE_URL.
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	// +kubebuilder:validation:Pattern=`^https?://[^/\s]+`
	BaseURL string `json:"baseUrl"`
	// Environment is the ACH Hub Environment name. Required with tokenExchange.
	// +optional
	Environment string `json:"environment,omitempty"`
	// +kubebuilder:validation:Required
	KeyFrom AchKeyFrom `json:"keyFrom"`
	// +kubebuilder:validation:Required
	Model AchModelSpec `json:"model"`
}

// AgentOpenShellSpec is the Agent's own sandbox policy.
type AgentOpenShellSpec struct {
	// Policy is merged over the profile's policy: network rules by name, filesystem lists
	// concatenated, scalars agent over profile. Rule names starting with ach-agent- are
	// reserved.
	// +optional
	// +kubebuilder:pruning:PreserveUnknownFields
	// +kubebuilder:validation:Type=object
	Policy *apiextensionsv1.JSON `json:"policy,omitempty"`
}

// AgentSandboxSpec holds the per-Agent sandbox overrides.
type AgentSandboxSpec struct {
	// +optional
	OpenShell *AgentOpenShellSpec `json:"openshell,omitempty"`
}

// ParentRef is the Gateway an HTTPRoute attaches to.
type ParentRef struct {
	// +kubebuilder:validation:MinLength=1
	Name string `json:"name"`
	// +optional
	Namespace string `json:"namespace,omitempty"`
	// +optional
	SectionName string `json:"sectionName,omitempty"`
}

// RouteSpec publishes the Agent through a Gateway API HTTPRoute.
type RouteSpec struct {
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinLength=1
	Hostname string `json:"hostname"`
	// Path is the URL prefix on hostname; the HTTPRoute strips it before the control pod.
	// +kubebuilder:default="/"
	// +optional
	Path string `json:"path,omitempty"`
	// ParentRef defaults to the operator's configured Gateway.
	// +optional
	ParentRef *ParentRef `json:"parentRef,omitempty"`
}

// ExposeSpec controls how an Agent is reachable. An Agent is fully private (control Pod
// only, no Service) unless it opts in.
// +kubebuilder:validation:XValidation:rule="!has(self.route) || self.service",message="expose.route requires expose.service"
type ExposeSpec struct {
	// Service creates the ClusterIP Service so in-cluster peers (a2a) or an ingress can reach
	// the control pod. Required for any inbound HTTP channel to be reachable at all.
	// +optional
	Service bool `json:"service,omitempty"`
	// +optional
	Route *RouteSpec `json:"route,omitempty"`
}

// AgentSpec defines the desired state of an Agent.
// +kubebuilder:validation:XValidation:rule="self.capabilityServer == 'standalone' ? (has(self.standalone) && !has(self.ach)) : (has(self.ach) && !has(self.standalone))",message="capabilityServer selects exactly one of standalone/ach"
// +kubebuilder:validation:XValidation:rule="!has(self.filter) || !has(self.filter.exclude) || !has(self.filter.exclude.skills) || size(self.filter.exclude.skills) == 0 || self.capabilityServer == 'ach'",message="filter.exclude.skills requires capabilityServer ach"
type AgentSpec struct {
	// +kubebuilder:validation:Required
	ProfileRef LocalObjectRef `json:"profileRef"`
	// CapabilityServer selects where the model and tools come from.
	// +kubebuilder:default=standalone
	// +kubebuilder:validation:Enum=standalone;ach
	CapabilityServer string `json:"capabilityServer,omitempty"`
	// +optional
	Standalone *StandaloneSpec `json:"standalone,omitempty"`
	// +optional
	Ach *AchServerSpec `json:"ach,omitempty"`
	// Filter is the governance gate above the model, in both modes.
	// +optional
	Filter *FilterSpec `json:"filter,omitempty"`
	// AgentDefaults are the inline per-agent overrides of the profile's spec.defaults.
	// Per-field deep merge: a set field here wins, an omitted one inherits the profile's.
	AgentDefaults `json:",inline"`
	// Env are pod-level environment variables merged over AgentProfile.spec.control.env by
	// name. An agent entry replaces the complete inherited EnvVar. Reserved ACH_* names are
	// forbidden; only literal values and secretKeyRef sources are supported.
	// +optional
	// +listType=map
	// +listMapKey=name
	// +kubebuilder:validation:XValidation:rule="self.all(e, !e.name.startsWith('ACH_'))",message="env must not set reserved ACH_* vars"
	// +kubebuilder:validation:XValidation:rule="self.all(e, !has(e.valueFrom) || (has(e.valueFrom.secretKeyRef) && !has(e.valueFrom.configMapKeyRef) && !has(e.valueFrom.fieldRef) && !has(e.valueFrom.resourceFieldRef) && !has(e.valueFrom.fileKeyRef)))",message="env valueFrom supports only secretKeyRef"
	// +kubebuilder:validation:XValidation:rule="self.all(e, !has(e.valueFrom) || !has(e.value) || e.value == '')",message="env value and valueFrom are mutually exclusive"
	Env []corev1.EnvVar `json:"env,omitempty"`
	// +optional
	Prompt *AgentPromptSpec `json:"prompt,omitempty"`
	// +optional
	Memory *MemorySpec `json:"memory,omitempty"`
	// Hooks are agent-level workspace and session lifecycle hooks, run inside the sandbox
	// with only engine.forwardEnv variables.
	// +optional
	Hooks *HooksSpec `json:"hooks,omitempty"`
	// +kubebuilder:validation:Required
	// +kubebuilder:validation:MinItems=1
	// +listType=map
	// +listMapKey=name
	Channels []ChannelSpec `json:"channels"`
	// +optional
	Sandbox *AgentSandboxSpec `json:"sandbox,omitempty"`
	// Expose controls reachability. Omit for a fully private agent.
	// +optional
	Expose *ExposeSpec `json:"expose,omitempty"`
}

// AgentStatus is the observed state.
type AgentStatus struct {
	// +optional
	ObservedGeneration int64 `json:"observedGeneration,omitempty"`
	// +optional
	// +patchMergeKey=type
	// +patchStrategy=merge
	Conditions []metav1.Condition `json:"conditions,omitempty" patchStrategy:"merge" patchMergeKey:"type"`
}

// +kubebuilder:object:root=true
// +kubebuilder:subresource:status
// +kubebuilder:resource:scope=Namespaced
// +kubebuilder:printcolumn:name="Profile",type=string,JSONPath=".spec.profileRef.name"
// +kubebuilder:printcolumn:name="Ready",type=string,JSONPath=".status.conditions[?(@.type=='Ready')].status"
// +kubebuilder:printcolumn:name="Age",type=date,JSONPath=".metadata.creationTimestamp"
// +kubebuilder:validation:XValidation:rule="size(self.metadata.name) <= 50",message="Agent name must be <= 50 chars (operator derives <=63-char child names)"
// +kubebuilder:validation:XValidation:rule="!self.metadata.name.matches('-[0-9a-f]{20}$')",message="Agent name must not end in -<20 hex> (reserved for the runtime's agent-<name>-<20 hex> workspace names)"

// Agent is a running agent instance.
type Agent struct {
	metav1.TypeMeta   `json:",inline"`
	metav1.ObjectMeta `json:"metadata,omitempty"`

	Spec   AgentSpec   `json:"spec,omitempty"`
	Status AgentStatus `json:"status,omitempty"`
}

// +kubebuilder:object:root=true

// AgentList contains a list of Agent.
type AgentList struct {
	metav1.TypeMeta `json:",inline"`
	metav1.ListMeta `json:"metadata,omitempty"`
	Items           []Agent `json:"items"`
}

func init() {
	SchemeBuilder.Register(&Agent{}, &AgentList{})
}
