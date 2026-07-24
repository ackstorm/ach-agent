# Thinking is model-owned: normalized `model.thinking`, engines translate

**Date:** 2026-07-24 · **Status:** Shipped · **Supersedes:**
[2026-07-23-pi-model-capability-and-thinking](2026-07-23-pi-model-capability-and-thinking.md)

## Symptom

v0.8.1 gave Pi's reasoning/thinking a home under `engine.pi.model`/`engine.pi.thinkingLevel`.
That bound a model-level concern to one engine's sub-block: flipping `engine.type` silently
dropped the operator's thinking intent, `../ach` had to render a Pi-shaped model descriptor
(`PiModelSpec`) that duplicated model semantics outside the model block, and the same intent
would have needed re-encoding per engine forever.

## Decision

- **`engine.type` only selects the runtime.** `engine.pi` carries only executable knobs
  (`binaryPath`, `mcpAdapterPath`). The v0.8.1 `engine.pi.model`/`thinkingLevel` fields are
  removed outright in v0.9.0 (breaking; released for one day, never rendered by any pushed
  control plane).
- **`model` is the single configuration surface for model identity and thinking intent**:
  `model.thinking.enabled` (strict bool) + `model.thinking.effort`
  (`minimal|low|medium|high|xhigh`; requires `enabled`; `off` ≡ `enabled:false`; `max`
  and provider-specific levels such as `ultracode` stay outside the portable enum —
  reachable via `model.params` passthrough; widen the Literal + per-engine tables if a
  level ever becomes portable). NOT `model.params`: params stays open provider-specific
  per-call passthrough, and explicit params keys win over the generated translation.
- **Each engine translates the normalized block itself**:
  - **pi** — `models.json` descriptor `reasoning: <enabled>`; `--thinking <effort>` on the
    launch argv, identity-mapped including `xhigh` (both RPC and native-TUI paths via the
    shared `_common_args`). The rest of the descriptor (`input`, `contextWindow`,
    `maxTokens`, `cost`) stays hardcoded at Pi's builtin defaults — deliberately not
    operator-configurable; no capability/pricing surface exists.
  - **opencode** — `lifecycle._thinking_options` merges per-wire providerOptions under the
    generated model options: openai → `reasoningEffort` (passthrough incl. `xhigh`),
    gemini → `thinkingConfig.thinkingLevel` (`xhigh` clamps to `high` — gemini has no
    xhigh level), anthropic → `thinking.budgetTokens`
    (1024/4096/10000/24576/32000 for minimal/low/medium/high/xhigh; chosen constants,
    provider minimum 1024).
- **No `/hydrate` involvement.** `runtime.models[{id,endpoint}]` and `resolve_model`'s
  fail-closed membership check are untouched; richer hydrate model metadata is a possible
  future enhancement, not a prerequisite.

## Changes

- `src/ach_agent/config/schema.py`: `ThinkingBlock`, `ModelBlock.thinking`;
  `PiEngineBlock` stripped to `binaryPath`/`mcpAdapterPath`; `PiModelCapabilities` deleted.
- `docs/schemas/agent-config-v1.schema.json`: regenerated.
- `src/ach_agent/engine/base/driver.py`: `EngineConfig.thinking_enabled`/`thinking_effort`
  replace `PiModelCapability`/`pi_model_capability`/`pi_thinking_level`.
- `src/ach_agent/engine/pi/models_json.py` + `driver.py`: derive `reasoning`, emit
  `--thinking` from the normalized fields.
- `src/ach_agent/engine/lifecycle.py`: `_thinking_options` + merge into
  `write_opencode_config`'s model options.
- `src/ach_agent/main.py`: `_engine_runtime_fields` (replaces `_pi_engine_fields`).
- `docs/plan/CONTRACT_v3.md` §2 rewritten; `../ach` correction handoff:
  `docs/superpowers/plans/2026-07-24-model-thinking-handoff-ach.md`.

Absent `model.thinking`, every generated artifact (models.json, argv, opencode.json) is
byte-identical to v0.8.0 defaults.
