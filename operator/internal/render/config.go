// SPDX-License-Identifier: MIT

// Package agentrender collapses an AgentProfile + Agent into the workspace-v1 WSConfig
// (render2.go's Render2) the ach-agent harness self-boots from. Pure: no API calls, no side
// effects. This file holds the Block types Render2/workspace-v1 share with each other —
// output JSON tags MUST match the vendored
// testdata/ach-workspace-config-v2.schema.json (validated by schema_test.go).
package render

type ModelBlock struct {
	Name     string         `json:"name"`
	Type     string         `json:"type"`
	Params   map[string]any `json:"params,omitempty"`
	Thinking *ThinkingBlock `json:"thinking,omitempty"`
}

// ThinkingBlock is the normalized model.thinking reasoning intent (schema
// $defs/ThinkingBlock) — the canonical thinking surface each engine translates.
type ThinkingBlock struct {
	Enabled bool   `json:"enabled"`
	Effort  string `json:"effort,omitempty"`
}

type PromptBlock struct {
	System  PromptSystemBlock `json:"system"`
	Compose string            `json:"compose,omitempty"`
}

// PromptSystemBlock — the render sets ONLY the active variant's fields; SystemAch legally
// carries an optional `file` (subpath), so type=ach may emit both ach and file.
type PromptSystemBlock struct {
	Type string `json:"type"`
	Text string `json:"text,omitempty"`
	File string `json:"file,omitempty"`
	Ach  string `json:"ach,omitempty"`
}

type MemoryBlock struct {
	Type      string          `json:"type"`
	AchMemory *AchMemoryBlock `json:"achMemory,omitempty"`
	Codemem   *CodememBlock   `json:"codemem,omitempty"`
}

// AchMemoryBlock renders memory.achMemory. Endpoint and McpServerID are the two
// arms of a WHERE-is-ach-memory choice — exactly one is set (CEL-enforced), so both
// are omitempty and the harness treats neither-or-both as a hard config error.
type AchMemoryBlock struct {
	Endpoint    string              `json:"endpoint,omitempty"`
	McpServerID string              `json:"mcpServerId,omitempty"`
	Auth        *AchMemoryAuthBlock `json:"auth,omitempty"`
	Project     string              `json:"project,omitempty"`
}

// AchMemoryAuthBlock is the rendered memory.achMemory.auth discriminated union.
// Env and Header are set on the bearer arm ONLY — the ach arm carries no second
// credential. Header is omitted when the author left it unset, so the harness
// applies its own "Authorization" default rather than ACH restating it.
type AchMemoryAuthBlock struct {
	Type   string `json:"type"`
	Env    string `json:"env,omitempty"`
	Header string `json:"header,omitempty"`
}

type CodememBlock struct {
	DBPath  string `json:"dbPath,omitempty"`
	Project string `json:"project,omitempty"`
}

// PrepareBlock is the rendered per-invocation workspace hook (channels[].handoff,
// channels[].script). SecretEnv carries env NAMES only; the values reach the harness
// process through secretKeyRef env injection (see ChannelSecretEnv).
type PrepareBlock struct {
	Script         string                       `json:"script"`
	Env            map[string]string            `json:"env,omitempty"`
	SecretEnv      map[string]SecretSourceBlock `json:"secretEnv,omitempty"`
	TimeoutSeconds *int64                       `json:"timeoutSeconds,omitempty"`
}

// SecretSourceBlock is an inbound-auth secret source (schema SecretSource). The
// operator only ever emits env: the value lives in the harness process env
// (unreadable by the same-uid agent under PR_SET_DUMPABLE=0), never on a
// same-uid-readable mounted file. The contract's file variant was dropped as the
// weaker path, so the operator has no file to render.
type SecretSourceBlock struct {
	Env string `json:"env,omitempty"`
}

type CronBlock struct {
	Schedule string `json:"schedule"`
	Timezone string `json:"timezone,omitempty"`
}
