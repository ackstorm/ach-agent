# Pi model capability + thinking level: a typed `engine.pi` surface, not `model.params`

**Date:** 2026-07-23 · **Status:** Superseded by [2026-07-24-model-owned-thinking](2026-07-24-model-owned-thinking.md) (surface removed in v0.9.0)

## Symptom

`engine.pi`'s generated `models.json` hardcoded every model as `reasoning: false`,
`input: ["text"]`, `contextWindow: 128000`, `maxTokens: 16384`, and never passed
`--thinking` to the `pi` subprocess. A reasoning-capable model configured through Pi ran
with reasoning off and the wrong capability envelope no matter what the operator set —
the opposite of a direct `pi` setup, where a custom provider's model descriptor carries
real `reasoning`/`input`/`contextWindow`/`maxTokens`, and `--thinking`/
`defaultThinkingLevel` select a thinking level.

The real e2e test (`tests/e2e/test_pi_e2e.py`) only ever exercised a plain
`openai-completions`, non-reasoning model, so this gap shipped in v0.8.0 unnoticed.

## Decision

Give Pi's capability/thinking a **typed, generated** home: `engine.pi.model`
(`reasoning`/`input`/`contextWindow`/`maxTokens`) and `engine.pi.thinkingLevel`, added to
`PiEngineBlock` (`src/ach_agent/config/schema.py`) alongside the existing
`binaryPath`/`mcpAdapterPath` fields, and regenerated into
`docs/schemas/agent-config-v1.schema.json` via the existing `scripts/gen_schema.py` +
`tests/config/test_schema_artifact.py` drift guard.

This is deliberately **not** `model.params`. `model.params` is CONTRACT_v3 §2's existing
"OPEN, UNVALIDATED dict, splatted to the model client" — genuinely opaque, per-call
passthrough (temperature, top_p, …), and its JSON Schema entry is intentionally
`{"type": "object"}`. Reusing it for Pi's well-known, typed capability fields would have
made `docs/schemas/agent-config-v1.schema.json` — the artifact a Kubernetes operator
author actually reads to know what `engine.pi` accepts — lie about what's configurable.
An earlier draft of this decision proposed exactly that reuse; it was corrected before
implementation.

| `engine.pi` field | Type | Default | Effect |
|---|---|---|---|
| `model.reasoning` | strict bool (no string/numeric coercion) | `false` | `models.json` model descriptor's `reasoning` field |
| `model.input` | exactly `["text"]` or `["text", "image"]` | `["text"]` | `models.json` model descriptor's `input` |
| `model.contextWindow` | positive strict int (no bool/string/float coercion) | `128000` | `models.json` model descriptor's `contextWindow` |
| `model.maxTokens` | positive strict int (no bool/string/float coercion) | `16384` | `models.json` model descriptor's `maxTokens` |
| `thinkingLevel` | one of `off`/`minimal`/`low`/`medium`/`high`/`xhigh`/`max` | unset | Passed to `pi` as `--thinking <level>`; **requires** `model.reasoning: true` (hard-fail otherwise — never an implied auto-enable) |

Every invalid value (including strings/numerics offered for strict scalar fields; an
empty, image-only, duplicated, reversed, overlong, or unrecognized `input`; a
`thinkingLevel` without `model.reasoning: true`; or an unrecognized `thinkingLevel`
string) hard-fails at config-load time (`src/ach_agent/config/schema.py`'s
`PiModelCapabilities`/`PiEngineBlock` validators) — the same fail-loud posture as every
other CONTRACT §2 block (`extra="forbid"`, `sys.exit(1)` in `load_config`). Direct
Pydantic tests assert these are field-local validation errors, not the
pre-implementation `extra_forbidden` error caused by the entire `model` key being
unknown.

No `model.type` → provider/wire change: `_PI_PROVIDER_BY_TYPE` (openai/gemini/anthropic)
is untouched; this only affects the model *descriptor* inside whichever provider block
that mapping already selects.

### CLI flags, not `settings.json` defaults

Provider and model were already selected via explicit `--provider <name> --model <id>`
CLI flags at Pi subprocess launch (`src/ach_agent/engine/pi/driver.py`'s `launch()`),
never via Pi's `settings.json` `defaultProvider`/`defaultModel`. `--thinking <level>`
follows the same convention. `settings.json`'s `defaultThinkingLevel` (and
`defaultProvider`/`defaultModel`) are interactive-session fallbacks — meaningful when a
human opens `pi` without flags and expects a remembered default. The harness always
launches a fresh `pi --mode rpc` subprocess with deterministic, per-invocation CLI flags;
there is no interactive session for a settings-file default to serve, so the CLI flag is
both simpler and the only one actually exercised.

## What is deliberately NOT covered

- **`thinkingLevelMap`** (Pi's per-model mapping of pi thinking levels to
  provider-specific values) is not exposed. Pi's own default mapping per `api` kind is
  used. Add a typed `engine.pi.model.thinkingLevelMap` if a specific provider/model needs
  a non-default mapping — not needed by any currently configured model.
- **`model.params`'s per-call passthrough for Pi** — Pi's `ProviderModelConfig` has no
  generic per-call options field the way opencode's `providerOptions` does
  (`docs/references/2026-07-03-provider-by-model-type.md`'s `model.params` → per-model
  `options` follow-up is opencode-only). `model.params` continues to have zero effect on
  Pi. This is a real platform difference, not a bug to paper over.

## Changes

- `src/ach_agent/config/schema.py`: `PiModelCapabilities`, `PiEngineBlock.model`/
  `.thinking_level` + the cross-field `model_validator`.
- `docs/schemas/agent-config-v1.schema.json`: regenerated (drift-guarded by
  `tests/config/test_schema_artifact.py`).
- `docs/plan/CONTRACT_v3.md`: `engine.pi` example updated.
- `src/ach_agent/engine/base/driver.py`: `PiModelCapability` dataclass,
  `EngineConfig.pi_model_capability`/`.pi_thinking_level`.
- `src/ach_agent/engine/pi/models_json.py`: reads `cfg.pi_model_capability`.
- `src/ach_agent/engine/pi/driver.py`: `launch()` appends `--thinking <level>` when
  `cfg.pi_thinking_level` is set.
- `src/ach_agent/main.py`: `_pi_engine_fields()` wires `cfg.engine.pi.*` into
  `EngineConfig`.
- `../ach`: CRD/render handoff for `EngineSpec.Pi.Model`/`.ThinkingLevel` — see
  `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md`.
- Tests: `tests/config/test_schema.py` + `tests/config/fixtures/config_pi_reasoning.json`
  (schema), `tests/engine/pi/test_models_json.py` + `test_driver.py` (unit),
  `tests/test_main_wiring.py` (wiring), `tests/e2e/test_pi_e2e.py` (real `pi` 0.81.1
  subprocess, version-pinned, asserts on `get_state`'s `thinkingLevel`/`model.reasoning`
  with id+command validated on every RPC response).

Defaults are unchanged from v0.8.0 when `engine.pi.model`/`thinkingLevel` are absent, so
no existing configuration's behavior changes on upgrade.
