# Handoff prompt — `../ach`: render `engine.type` / `engine.pi`

> **This is a self-contained prompt for the ACH operator agent working in the `../ach` repo
> (`github.com/ackstorm/ach`). Do NOT execute it in `ach-agent`.** It is SP2 item 5.
>
> **Hard precondition:** an `ach-agent` image that ships the `pi` binary (SP2 Phase 1) must be
> **released** before this change lands / before any AgentProfile sets `engine.type: pi` —
> otherwise the rendered config names a binary the image lacks. Verify a `ghcr.io/ackstorm/ach-agent`
> tag with Pi exists first.

---

## Task

Let an `AgentProfile` author select the Pi engine. Add `type` + `pi` to the harness-local engine
block and render them into the config the harness consumes (`engine.type` / `engine.pi`, per
`ach-agent` `CONTRACT_v3.md`). `type` is a **free string** — the harness is the enforcer (D-2);
do **not** enum-lock it in the CRD.

The harness already accepts `engine.type: opencode|pi` and `engine.pi.{binaryPath, mcpAdapterPath}`;
this change just makes the operator able to render them.

## Changes (exact)

**1. CRD type — `api/ach/v1alpha1/agentprofile_types.go`**, `EngineSpec` (around `:41-58`), add
two optional fields and a new sub-struct:

```go
	// Type selects the engine. Free string ("opencode"|"pi"); the harness validates and
	// hard-fails on an unknown value. Omitted → harness default (opencode).
	// +optional
	Type string `json:"type,omitempty"`
	// Pi configures the Pi engine; consulted only when Type == "pi".
	// +optional
	Pi *PiEngineSpec `json:"pi,omitempty"`
```

```go
// PiEngineSpec is the harness-local Pi engine block (config: engine.pi.*). Both fields are
// optional; empty values fall back to the image defaults (pi on PATH; the vendored adapter at
// /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter).
type PiEngineSpec struct {
	// +optional
	BinaryPath string `json:"binaryPath,omitempty"`
	// +optional
	McpAdapterPath string `json:"mcpAdapterPath,omitempty"`
}
```

**2. Render struct — `internal/agentrender/config.go`**, `EngineBlock` (`:58-65`), add:

```go
	Type                  string   `json:"type,omitempty"`
	Pi                    *PiBlock `json:"pi,omitempty"`
```

```go
type PiBlock struct {
	BinaryPath     string `json:"binaryPath,omitempty"`
	McpAdapterPath string `json:"mcpAdapterPath,omitempty"`
}
```

**3. Render mapping — `internal/agentrender/render.go`**, `renderEngine` (`:204`), map the new
fields (leave the existing fields untouched):

```go
func renderEngine(e *achv1alpha1.EngineSpec) *EngineBlock {
	if e == nil {
		return nil
	}
	b := &EngineBlock{
		Home: e.Home, WorkDir: e.WorkDir, ForwardEnv: sanitizeForwardEnv(e.ForwardEnv),
		IdleTTLSeconds: e.IdleTTLSeconds, StartupTimeoutSeconds: e.StartupTimeoutSeconds,
		MaxToolCalls: e.MaxToolCalls, Type: e.Type,
	}
	if e.Pi != nil {
		b.Pi = &PiBlock{BinaryPath: e.Pi.BinaryPath, McpAdapterPath: e.Pi.McpAdapterPath}
	}
	return b
}
```

## Test (add to `internal/agentrender/render_test.go`)

Assert a profile with `engine.type: pi` renders `engine.type` + `engine.pi.*` into the config:

```go
func TestRenderEnginePi(t *testing.T) {
	e := &achv1alpha1.EngineSpec{
		Type: "pi",
		Pi:   &achv1alpha1.PiEngineSpec{BinaryPath: "pi", McpAdapterPath: "/opt/adapter"},
	}
	b := renderEngine(e)
	if b.Type != "pi" {
		t.Fatalf("Type = %q, want pi", b.Type)
	}
	if b.Pi == nil || b.Pi.BinaryPath != "pi" || b.Pi.McpAdapterPath != "/opt/adapter" {
		t.Fatalf("Pi = %+v, want binaryPath=pi mcpAdapterPath=/opt/adapter", b.Pi)
	}
}
```

## Regenerate + verify

```bash
make manifests generate   # or this repo's CRD-regen target — updates the CRD YAML + zz_generated.deepcopy.go
go test ./internal/agentrender/...
go build ./...
```

Expected: `TestRenderEnginePi` passes; the regenerated `AgentProfile` CRD carries the new
`engine.type` / `engine.pi` fields; build clean.

## Constraints

- `type` stays a **free string** — no `+kubebuilder:validation:Enum` (D-2; the harness validates).
- ek hygiene unchanged: these fields carry paths/type names only, never secrets. `sanitizeForwardEnv`
  is untouched.
- Follow this repo's commit ritual (conventional commit + its release process).
