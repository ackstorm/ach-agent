# Handoff prompt — `../ach`: render `engine.pi.model` / `engine.pi.thinkingLevel`

> **SUPERSEDED (2026-07-24):** the `engine.pi.model`/`engine.pi.thinkingLevel` surface this
> document introduced was removed in v0.9.0. See
> `docs/superpowers/plans/2026-07-24-model-thinking-supersedes-engine-pi-model.md` and
> `docs/references/2026-07-24-model-owned-thinking.md`. Do not execute or hand off from
> this document.

> **This is a self-contained prompt for the ACH operator agent working in the `../ach`
> repo (`github.com/ackstorm/ach`). Do NOT execute it in `ach-agent`.**
>
> **Hard precondition:** an `ach-agent` image carrying this fix (Tasks 1-5 of
> `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity.md`, released as v0.8.1 or
> later) must be **released** before any `AgentProfile` sets `engine.pi.model.reasoning`
> or `engine.pi.thinkingLevel` — otherwise the rendered config carries fields an older
> harness rejects because pre-fix `PiEngineBlock` has `extra="forbid"`. Verify the
> deployed `ghcr.io/ackstorm/ach-agent` tag before rendering these fields.

## Task

Let an `AgentProfile` author configure Pi's model capability and thinking level. Add
`Model` and `ThinkingLevel` to the existing `PiEngineSpec` (added by the prior
`engine.type`/`engine.pi.{binaryPath,mcpAdapterPath}` handoff) and render them into
`engine.pi.model` / `engine.pi.thinkingLevel`, per `ach-agent`'s updated
`CONTRACT_v3.md` and `docs/schemas/agent-config-v1.schema.json`. Both new Go fields stay
**free-form** (no `+kubebuilder:validation:Enum`) — `ach-agent`'s Pydantic layer is the
single enforcer (mirrors the existing `EngineSpec.Type` free-string precedent, D-2).

## Changes (exact)

**1. CRD type — `api/ach/v1alpha1/agentprofile_types.go`**, extend `PiEngineSpec` (added
next to the existing `BinaryPath`/`McpAdapterPath` fields) with:

```go
// PiEngineSpec is the harness-local Pi engine block (config: engine.pi.*).
type PiEngineSpec struct {
	// +optional
	BinaryPath string `json:"binaryPath,omitempty"`
	// +optional
	McpAdapterPath string `json:"mcpAdapterPath,omitempty"`
	// Model is Pi's typed capability descriptor. Omitted → the harness's own builtin
	// defaults (reasoning=false, input=[text], contextWindow=128000, maxTokens=16384).
	// +optional
	Model *PiModelSpec `json:"model,omitempty"`
	// ThinkingLevel selects the --thinking level passed to pi at launch. Free string —
	// ach-agent validates (one of off|minimal|low|medium|high|xhigh|max) and hard-fails
	// on an unrecognized value or a value set without Model.Reasoning=true.
	// +optional
	ThinkingLevel string `json:"thinkingLevel,omitempty"`
}

// PiModelSpec is Pi's model capability descriptor (config: engine.pi.model.*). Free-form
// — ach-agent's Pydantic PiModelCapabilities is the single enforcer (D-2 precedent).
type PiModelSpec struct {
	// +optional
	Reasoning bool `json:"reasoning,omitempty"`
	// +optional
	Input []string `json:"input,omitempty"`
	// +optional
	ContextWindow int `json:"contextWindow,omitempty"`
	// +optional
	MaxTokens int `json:"maxTokens,omitempty"`
}
```

**2. Render struct — `internal/agentrender/config.go`**, extend the existing `PiBlock`:

```go
type PiBlock struct {
	BinaryPath     string        `json:"binaryPath,omitempty"`
	McpAdapterPath string        `json:"mcpAdapterPath,omitempty"`
	Model          *PiModelBlock `json:"model,omitempty"`
	ThinkingLevel  string        `json:"thinkingLevel,omitempty"`
}

type PiModelBlock struct {
	Reasoning     bool     `json:"reasoning,omitempty"`
	Input         []string `json:"input,omitempty"`
	ContextWindow int      `json:"contextWindow,omitempty"`
	MaxTokens     int      `json:"maxTokens,omitempty"`
}
```

**3. Render mapping — `internal/agentrender/render.go`**, extend `renderEngine`'s
existing `if e.Pi != nil` branch:

```go
	if e.Pi != nil {
		b.Pi = &PiBlock{
			BinaryPath: e.Pi.BinaryPath, McpAdapterPath: e.Pi.McpAdapterPath,
			ThinkingLevel: e.Pi.ThinkingLevel,
		}
		if e.Pi.Model != nil {
			b.Pi.Model = &PiModelBlock{
				Reasoning: e.Pi.Model.Reasoning, Input: e.Pi.Model.Input,
				ContextWindow: e.Pi.Model.ContextWindow, MaxTokens: e.Pi.Model.MaxTokens,
			}
		}
	}
```

## Test (add to `internal/agentrender/render_test.go`)

Assert a profile with `engine.pi.model`/`thinkingLevel` renders both into the config:

```go
func TestRenderEnginePiModelCapability(t *testing.T) {
	e := &achv1alpha1.EngineSpec{
		Type: "pi",
		Pi: &achv1alpha1.PiEngineSpec{
			BinaryPath: "pi",
			Model: &achv1alpha1.PiModelSpec{
				Reasoning: true, Input: []string{"text"}, ContextWindow: 200000, MaxTokens: 32000,
			},
			ThinkingLevel: "high",
		},
	}
	b := renderEngine(e)
	if b.Pi == nil || b.Pi.Model == nil {
		t.Fatalf("Pi.Model = nil, want a rendered PiModelBlock")
	}
	if !b.Pi.Model.Reasoning || b.Pi.Model.ContextWindow != 200000 || b.Pi.Model.MaxTokens != 32000 {
		t.Fatalf("Pi.Model = %+v, want reasoning=true contextWindow=200000 maxTokens=32000", b.Pi.Model)
	}
	if b.Pi.ThinkingLevel != "high" {
		t.Fatalf("Pi.ThinkingLevel = %q, want high", b.Pi.ThinkingLevel)
	}
}
```

## Regenerate + verify

```bash
make manifests generate   # or this repo's CRD-regen target — updates the CRD YAML + zz_generated.deepcopy.go
go test ./internal/agentrender/...
go build ./...
```

Expected: `TestRenderEnginePiModelCapability` passes (alongside the existing
`TestRenderEnginePi`); the regenerated `AgentProfile` CRD carries
`engine.pi.model.*`/`engine.pi.thinkingLevel`; build clean.

## Constraints

- `Model`/`ThinkingLevel` fields stay **free-form** — no `+kubebuilder:validation:Enum`
  or range annotations (D-2 precedent; `ach-agent`'s Pydantic layer is the single
  enforcer, so the two repos never drift on what's "valid").
- ek hygiene unchanged: these fields carry booleans/strings/ints only, never secrets.
- Follow this repo's commit ritual (conventional commit + its release process).
