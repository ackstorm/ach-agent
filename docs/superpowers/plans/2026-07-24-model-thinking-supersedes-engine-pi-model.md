# Model-Owned Thinking — Corrective Plan (supersedes the `engine.pi.model`/`thinkingLevel` surface)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

> **SUPERSEDES** `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity.md` (shipped as
> v0.8.1) and its handoff `2026-07-23-pi-model-runtime-parity-handoff-ach.md`. That plan put
> model capability + thinking under `engine.pi.model`/`engine.pi.thinkingLevel`. That surface
> is architecturally wrong and is REMOVED here: `engine.type` only selects the runtime,
> `engine.pi` carries only executable knobs, and the **model block is the single
> configuration surface for model identity and thinking/reasoning intent**. The three
> unapproved, **unpushed** `../ach` commits (319bdae, 41efd46, 49d503b) that render the wrong
> surface are corrected via the Task 5 handoff — which inspects and then STOPS for human
> direction on their disposition (never an autonomous history rewrite) — and their local
> Task 4 is NOT advanced first.

**Goal:** Move the thinking/reasoning intent to a normalized, typed `model.thinking` block
(`enabled` + controlled `effort` enum), delete the one-release-old
`engine.pi.model`/`engine.pi.thinkingLevel` surface, and have each engine translate the
normalized block itself: Pi → `models.json` descriptor `reasoning` + `--thinking <effort>`;
opencode → per-call providerOptions in the generated `opencode.json` model options.

**Architecture:** `engine.type` selects the runtime driver — nothing else. `engine.pi`
returns to exactly `{binaryPath, mcpAdapterPath}` (executable knobs). `model` is the sole
source of truth for model identity (`name`/`type`) and reasoning intent
(`thinking.enabled`/`thinking.effort`); `model.params` keeps its existing, narrower meaning
— provider-specific per-call passthrough, never the canonical thinking surface — and **wins
on key collision** with the generated thinking options. Pi's remaining `models.json`
descriptor fields (`input`, `contextWindow`, `maxTokens`, `cost`) are deliberately NOT
operator-configurable: they stay at the existing safe builtin defaults, hardcoded in
`build_models_json` exactly as in v0.8.0; only `reasoning` becomes derived
(`= thinking.enabled`). No capability/pricing surface is introduced anywhere, and `/hydrate`
(`runtime.models[{id,endpoint}]`) is neither changed nor depended on — richer hydrate model
metadata is a possible future enhancement, out of scope. The localhost model proxy and ek
hygiene are untouched: `models.json`/`opencode.json` still point at the loopback proxy with
a dummy key; no real API key ever reaches an engine.

**Tech Stack:** Python 3.12, Pydantic v2 (`StrictBool`, `Literal`, `model_validator`),
pytest (`asyncio_mode=auto`), `jsonschema` (already a dep of
`tests/config/test_schema_artifact.py`), mypy `--strict`, ruff — no new dependency. Real
`pi` 0.81.1 + vendored `pi-mcp-adapter` for the e2e edit (same environment/CI guards as
today, unchanged). `../ach` side: Go CRD/render correction via the Task 5 handoff (executed
in `../ach`, never from this repo; commit disposition decided by Juan Carlos there).

## Global Constraints

- **P-1 — separate review/commit prerequisite (not part of this plan):** the working
  tree's in-progress work (Pi native-TUI: `src/ach_agent/engine/pi/driver.py`
  `run_tui`/`_prepare_agent_dir`/`_common_args`, `config.py` `PI_LOCAL_PROXY_API_KEY`,
  `models_json.py` `"$PI_LOCAL_PROXY_API_KEY"` apiKey, `main.py` TUI branch; plus
  `README.md`/`docs/index.md` genericization and the SP1 spec revision) is **reviewed and
  committed by Juan Carlos in its own cycle, before this plan starts**. The executor of
  this plan NEVER commits, stashes, resets, or otherwise disposes of that work itself — it
  only verifies the prerequisite: `git status --porcelain` at Task 1 Step 1 must show a
  clean tree (untracked plan docs `docs/superpowers/plans/2026-07-0{3,5,6,7}-*.md` are the
  only tolerated entries); anything else → STOP and hand back to Juan Carlos. Every diff
  below is written against that landed state (e.g. `_common_args` exists;
  `models_json.py` emits `"$PI_LOCAL_PROXY_API_KEY"`). If P-1 lands with different
  surrounding lines, adapt the quoted context, never the semantics.
- **Native-TUI telemetry is explicitly OUT OF SCOPE — symmetrically, for BOTH engines.**
  This is not a Pi-specific gap: both `--tui` paths bypass the harness's
  `engine_runner`/`StatsSink` seam by construction — opencode's TUI attaches the native
  client to the pre-warmed serve (`_run_opencode_attach`), and Pi's `run_tui` hands the
  terminal to `pi` directly — so neither native-TUI mode produces `ach:sessions`/
  `ach:tools` telemetry today, and this plan makes **no telemetry claim** for either.
  Normal non-TUI/agentic invocations retain full, equal stats parity on both engines
  (unchanged). What this plan does guarantee for TUI mode is config parity only: the same
  generated `models.json`/`opencode.json` and thinking translation as the agentic path.
  TUI observation, if ever wanted, is a **common engine-neutral future feature** at the
  driver seam — never a Pi exception.
- **`model.thinking` is the canonical thinking surface.** Normalized, engine-neutral:
  `enabled: StrictBool = false`, `effort: minimal|low|medium|high|xhigh | null` (effort requires
  `enabled: true` — hard-fail). Never `model.params.thinkingConfig`/`reasoningEffort` as the
  canonical surface — those remain available to the operator only as provider-specific
  passthrough, and explicit `params` keys override the generated translation.
- **Effort enum is exactly `minimal|low|medium|high|xhigh`** (decision confirmed
  2026-07-24). Pi's `off` is expressed as `enabled: false`; `max` and provider-specific
  levels (e.g. `ultracode`) stay OUTSIDE the portable enum — reachable only via
  `model.params` passthrough. Extending later means widening the `Literal` + the
  per-engine tables, nothing else.
- **`engine.pi` carries only `binaryPath` + `mcpAdapterPath`.** `engine.pi.model` and
  `engine.pi.thinkingLevel` are removed outright (a **breaking config change** vs v0.8.1 —
  acceptable: released yesterday, never rendered by any pushed control plane; the `../ach`
  render commits are local-only and get rewritten). Version bumps to **0.9.0**.
- **Pi descriptor defaults preserved byte-identically:** absent `model.thinking` →
  `reasoning: false`, `input: ["text"]`, `contextWindow: 128000`, `maxTokens: 16384`,
  `cost: {input:0, output:0, cacheRead:0, cacheWrite:0}`, no `--thinking` flag — same output
  as v0.8.0. No model IDs are branched on anywhere.
- **`model.type` stays authoritative for provider + wire** (`_PI_PROVIDER_BY_TYPE`,
  `_PROVIDER_BY_TYPE`) — untouched.
- **No `/hydrate` change or dependency.** `hydrate.py`'s `ModelEntry`/`resolve_model`
  fail-closed flow is untouched.
- **`../ach` free-string philosophy (D-2):** the new Go `ThinkingSpec` fields carry no
  `+kubebuilder:validation:Enum` — ach-agent's Pydantic layer is the single enforcer.
- **TDD; one commit per task; tree green (including `tests/e2e/test_pi_e2e.py`
  collectability) after every commit. Stage only the files named in the task — never
  `git add -A`.**
- **mypy `--strict`** for every touched `src/` file; ruff check + format clean.
- **ek hygiene unchanged**: no secret in any generated file; `ACH_TOKEN` never logged.

---

## Task 1: `model.thinking` schema + `engine.pi` strip + regenerated artifact

**Files:**
- Modify: `src/ach_agent/config/schema.py:70-145` (delete `_default_input` +
  `PiModelCapabilities`; add `ThinkingBlock`; strip `PiEngineBlock`; extend `ModelBlock`)
- Modify: `docs/schemas/agent-config-v1.schema.json` (regenerated, never hand-edited)
- Modify: `tests/config/test_schema.py` (replace the 8 `pi_*` tests + keep `_pi_engine_base`)
- Modify: `tests/config/test_schema_artifact.py` (replace the pi-input artifact test)
- Modify: `tests/config/fixtures/config_pi_reasoning.json`

**Interfaces:**
- Consumes: existing `ModelBlock` (`schema.py:47-54`) and `PiEngineBlock`
  (`EngineBlock.pi: PiEngineBlock | None`).
- Produces: `ThinkingBlock` (`enabled: StrictBool = False`,
  `effort: Literal["minimal","low","medium","high","xhigh"] | None = None`, after-validator
  `_effort_requires_enabled`) and `ModelBlock.thinking: ThinkingBlock`
  (default `ThinkingBlock()`). Task 2 consumes `cfg.model.thinking.enabled` /
  `cfg.model.thinking.effort` by exactly these names. `PiEngineBlock` exposes only
  `binary_path` (`binaryPath`) and `mcp_adapter_path` (`mcpAdapterPath`).

- [ ] **Step 1: Verify P-1, then write the failing tests**

First verify the P-1 prerequisite: run `git status --porcelain`. Expected: empty except
the tolerated untracked plan docs (`docs/superpowers/plans/2026-07-0{3,5,6,7}-*.md`).
Any other dirty/untracked entry → STOP; that work belongs to Juan Carlos's separate
review/commit cycle, never to this plan.

In `tests/config/test_schema.py`, **delete** these Task-1-of-the-superseded-plan tests
(they assert the removed surface): `test_pi_engine_model_capability_defaults_when_absent`,
`test_pi_engine_model_capability_explicit_reasoning_and_thinking`,
`test_pi_thinking_level_without_reasoning_hard_fails`,
`test_pi_thinking_level_invalid_value_hard_fails`,
`test_pi_model_input_rejects_every_shape_except_supported_ordered_shapes`,
`test_pi_model_strict_scalars_reject_coercion_and_non_positive_values`,
`test_pi_model_input_error_is_value_validation_not_extra_field`,
`test_pi_model_scalar_error_is_strict_validation_not_extra_field`. **Keep** the
`_pi_engine_base(**pi_overrides)` helper (line 103) — the rejection test below reuses it.
Then append:

```python
def _model_thinking_base(thinking: object) -> dict:
    """_VALID_WEBHOOK_BASE with model.thinking set."""
    return {
        **_VALID_WEBHOOK_BASE,
        "model": {**_VALID_WEBHOOK_BASE["model"], "thinking": thinking},
    }


def test_model_thinking_defaults_when_absent(tmp_path: Path) -> None:
    config = _load_raw(tmp_path, dict(_VALID_WEBHOOK_BASE))
    assert config.model.thinking.enabled is False
    assert config.model.thinking.effort is None


def test_model_thinking_enabled_with_effort(tmp_path: Path) -> None:
    config = _load_raw(tmp_path, _model_thinking_base({"enabled": True, "effort": "high"}))
    assert config.model.thinking.enabled is True
    assert config.model.thinking.effort == "high"


def test_model_thinking_enabled_without_effort_is_valid(tmp_path: Path) -> None:
    config = _load_raw(tmp_path, _model_thinking_base({"enabled": True}))
    assert config.model.thinking.enabled is True
    assert config.model.thinking.effort is None


def test_model_thinking_effort_without_enabled_hard_fails(tmp_path: Path) -> None:
    from ach_agent.config import load_config

    raw = _model_thinking_base({"effort": "high"})
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize("invalid_effort", ["off", "max", "ultracode", "HIGH", ""])
def test_model_thinking_invalid_effort_hard_fails(tmp_path: Path, invalid_effort: str) -> None:
    from ach_agent.config import load_config

    raw = _model_thinking_base({"enabled": True, "effort": invalid_effort})
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize("invalid_enabled", ["true", 1, 0, "false"])
def test_model_thinking_enabled_rejects_coercion(
    tmp_path: Path, invalid_enabled: object
) -> None:
    from ach_agent.config import load_config

    raw = _model_thinking_base({"enabled": invalid_enabled})
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize(
    "payload",
    [{"enabled": True, "effort": "ultracode"}, {"enabled": "true"}, {"effort": "high"}],
)
def test_model_thinking_error_is_field_validation_not_extra_field(payload: dict) -> None:
    """Keep the red phase honest: today ModelBlock rejects the whole `thinking` key as
    extra, which must not masquerade as proof these values are checked."""
    from pydantic import ValidationError

    from ach_agent.config.schema import ThinkingBlock

    with pytest.raises(ValidationError) as exc_info:
        ThinkingBlock.model_validate(payload)
    errors = exc_info.value.errors()
    assert all(error["type"] != "extra_forbidden" for error in errors)


@pytest.mark.parametrize(
    "removed_surface",
    [{"model": {"reasoning": True}}, {"thinkingLevel": "high"}],
)
def test_engine_pi_model_and_thinking_level_are_rejected(
    tmp_path: Path, removed_surface: dict
) -> None:
    """The v0.8.1 engine.pi.model/thinkingLevel surface is gone: extra=forbid rejects it."""
    from ach_agent.config import load_config

    raw = _pi_engine_base(**removed_surface)
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0
```

In `tests/config/test_schema_artifact.py`, **delete**
`test_pi_input_schema_exposes_only_supported_ordered_shapes` and append:

```python
def test_model_thinking_schema_replaces_pi_capability_surface() -> None:
    schema = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    assert "PiModelCapabilities" not in schema["$defs"]
    assert set(schema["$defs"]["PiEngineBlock"]["properties"]) == {
        "binaryPath",
        "mcpAdapterPath",
    }
    thinking = schema["$defs"]["ThinkingBlock"]["properties"]
    assert thinking["enabled"]["type"] == "boolean"
    effort = thinking["effort"]
    assert {
        "enum": ["minimal", "low", "medium", "high", "xhigh"],
        "type": "string",
    } in effort["anyOf"]
    assert {"type": "null"} in effort["anyOf"]
    model_props = schema["$defs"]["ModelBlock"]["properties"]
    assert model_props["thinking"]["$ref"].endswith("ThinkingBlock")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_schema.py -v -k "model_thinking or engine_pi_model_and_thinking"`
Expected: the positive `model_thinking` tests FAIL (`ModelBlock` rejects `thinking` as
extra); the direct `ThinkingBlock` tests FAIL at import (type doesn't exist). The
`load_config` negative tests may already exit non-zero, but only via `extra_forbidden` — the
direct tests are the red-phase proof, exactly the honesty pattern the superseded plan used.
The rejection test PASSES only after Step 3 removes the fields (today `engine.pi.model` is
still accepted, so `_pi_engine_base(model={"reasoning": True})` loads fine → the test is
red now, green after).

Run: `uv run pytest tests/config/test_schema_artifact.py -v -k model_thinking`
Expected: FAIL — the artifact has no `$defs.ThinkingBlock` yet.

- [ ] **Step 3: Implement**

Edit `src/ach_agent/config/schema.py`. **Replace the whole region from `def
_default_input(` (line 70) through the end of `PiEngineBlock` (line 143, the
`_thinking_level_requires_reasoning` validator's `return self`)** with:

```python
class PiEngineBlock(BaseModel):
    """Pi-engine sub-block (consulted only when engine.type == 'pi').

    ONLY executable knobs live here. `binaryPath` pins the `pi` executable;
    `mcpAdapterPath` is the vendored pi-mcp-adapter package path referenced from Pi's
    settings.json `packages` (never a runtime `pi install`). Empty `mcpAdapterPath` → the
    driver falls back to the image's vendored default (SP2 pins it). Model identity and
    thinking/reasoning intent live in the model block (ModelBlock.thinking) — the
    v0.8.1-only `model`/`thinkingLevel` fields here were removed in v0.9.0.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    binary_path: str = Field(default="pi", alias="binaryPath")
    mcp_adapter_path: str = Field(default="", alias="mcpAdapterPath")
```

Then, directly **above** `class ModelBlock` (line 47), insert:

```python
class ThinkingBlock(BaseModel):
    """CONTRACT §2 model.thinking — normalized, engine-neutral reasoning intent.

    The canonical surface for "should this model think, and how hard". Each engine
    translates it (pi: models.json `reasoning` + `--thinking <effort>`; opencode:
    per-call providerOptions merged into the generated model options). Deliberately NOT
    model.params — params stays provider-specific per-call passthrough and wins on key
    collision with the generated translation.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    effort: Literal["minimal", "low", "medium", "high", "xhigh"] | None = None

    @model_validator(mode="after")
    def _effort_requires_enabled(self) -> ThinkingBlock:
        if self.effort is not None and not self.enabled:
            raise ValueError("model.thinking.effort requires model.thinking.enabled=true")
        return self
```

And inside `ModelBlock`, after the `params` field, add:

```python
    thinking: ThinkingBlock = Field(default_factory=ThinkingBlock)
```

Finally run `uv run ruff check src/ach_agent/config/schema.py` and delete only the imports
this removal orphaned (`StrictInt`, `field_validator`, `Annotated` — each only if ruff
reports it unused; `StrictBool`, `Literal`, `model_validator` stay used).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/config/test_schema.py -v`
Expected: all PASS (including every pre-existing non-pi test — nothing else changed shape).

- [ ] **Step 5: Update the fixture**

Rewrite `tests/config/fixtures/config_pi_reasoning.json`: keep every block byte-identical
**except** replace its `model` and `engine` blocks with:

```json
  "model": {
    "name": "openai.gpt-5",
    "type": "openai",
    "params": {
      "temperature": 1
    },
    "thinking": {
      "enabled": true,
      "effort": "high"
    }
  },
  "engine": {
    "workDir": "/workspace",
    "startupTimeoutSeconds": 30,
    "type": "pi",
    "pi": {
      "binaryPath": "pi",
      "mcpAdapterPath": "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter"
    }
  },
```

(`tests/config/test_schema_artifact.py::test_rendered_fixtures_validate_against_schema`
already globs `config_*.json` — no parametrization edit.)

- [ ] **Step 6: Regenerate the artifact and verify the drift guard**

Run: `uv run python scripts/gen_schema.py --check` → Expected: `STALE: …out of sync`.
Run: `uv run python scripts/gen_schema.py` → Expected: `wrote docs/schemas/agent-config-v1.schema.json (<N> bytes)`.
Run: `uv run pytest tests/config/test_schema_artifact.py -v` → Expected: all PASS,
including `test_model_thinking_schema_replaces_pi_capability_surface` and the fixture
validation.

- [ ] **Step 7: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/config/schema.py tests/config/ && uv run ruff format --check src/ach_agent/config/schema.py tests/config/ && uv run mypy --strict src/ach_agent/config/schema.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/config/schema.py docs/schemas/agent-config-v1.schema.json tests/config/test_schema.py tests/config/test_schema_artifact.py tests/config/fixtures/config_pi_reasoning.json
git commit -m "feat(config)!: model.thinking replaces engine.pi.model/thinkingLevel"
```

---

## Task 2: Runtime swap — `EngineConfig.thinking_*`, Pi translation, wiring, e2e

One task because the tree must stay green per commit: removing the `EngineConfig` fields
breaks `models_json.py`, `pi/driver.py`, `main.py`, and the e2e in the same stroke.

**Files:**
- Modify: `src/ach_agent/engine/base/driver.py` (delete `PiModelCapability`; swap fields)
- Modify: `src/ach_agent/engine/pi/models_json.py`
- Modify: `src/ach_agent/engine/pi/driver.py` (`_common_args` — post-P-1 shape)
- Modify: `src/ach_agent/main.py` (`_pi_engine_fields` → `_engine_runtime_fields`)
- Modify: `tests/engine/pi/test_models_json.py`, `tests/engine/pi/test_driver.py`,
  `tests/test_main_wiring.py`, `tests/e2e/test_pi_e2e.py`

**Interfaces:**
- Consumes: `cfg.model.thinking.{enabled,effort}` (Task 1).
- Produces: `EngineConfig.thinking_enabled: bool = False`,
  `EngineConfig.thinking_effort: str | None = None` (replacing `PiModelCapability`,
  `pi_model_capability`, `pi_thinking_level`), and
  `_engine_runtime_fields(cfg: Any) -> dict[str, Any]` in `main.py` (replacing
  `_pi_engine_fields`; returns `binary_path`, `pi_mcp_adapter_path`, `thinking_enabled`,
  `thinking_effort`). Task 3 consumes `config.thinking_enabled`/`config.thinking_effort`
  from inside `write_opencode_config`.

- [ ] **Step 1: Write the failing tests**

`tests/engine/pi/test_models_json.py`: change the top-level import back to
`from ach_agent.engine.base.driver import EngineConfig` (drop `PiModelCapability`), then
**replace** `test_default_capability_matches_pi_builtin_defaults` and
`test_capability_overrides_from_engine_config` with:

```python
def test_default_descriptor_matches_pi_builtin_defaults() -> None:
    doc, provider = build_models_json(
        EngineConfig(model_type="openai", model_base_url="http://x/v1")
    )
    model = doc["providers"][provider]["models"][0]
    assert model == {
        "id": "gpt-4o-mini",
        "name": "gpt-4o-mini",
        "reasoning": False,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 16384,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }


def test_thinking_enabled_derives_descriptor_reasoning() -> None:
    cfg = EngineConfig(
        model_type="openai", model_base_url="http://x/v1", thinking_enabled=True
    )
    doc, provider = build_models_json(cfg)
    model = doc["providers"][provider]["models"][0]
    assert model["reasoning"] is True
    # Everything else stays at Pi's builtin defaults — thinking never widens capability.
    assert model["input"] == ["text"]
    assert model["contextWindow"] == 128000
    assert model["maxTokens"] == 16384
```

`tests/engine/pi/test_driver.py`: in `test_launch_adds_thinking_flag_when_resolved`, change
the kwarg `pi_thinking_level="high"` → `thinking_effort="high"`; in
`test_run_tui_uses_native_mode_not_rpc` (landed by P-1), change `pi_thinking_level="low"` →
`thinking_effort="low"`. No other edits (the `--thinking`-omitted-by-default test already
builds a default `EngineConfig`).

`tests/test_main_wiring.py`: **replace** `test_pi_engine_fields_defaults_for_opencode`,
`test_pi_engine_fields_defaults_to_pi_binary_without_pi_overrides` (landed by P-1), and
`test_pi_engine_fields_from_pi_config` with:

```python
def _wiring_cfg(engine_type: str, pi: object, enabled: bool, effort: str | None) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        engine=SimpleNamespace(type=engine_type, pi=pi),
        model=SimpleNamespace(thinking=SimpleNamespace(enabled=enabled, effort=effort)),
    )


def test_engine_runtime_fields_defaults_for_opencode() -> None:
    from ach_agent.main import _engine_runtime_fields

    fields = _engine_runtime_fields(_wiring_cfg("opencode", None, False, None))
    assert fields == {
        "binary_path": "opencode",
        "pi_mcp_adapter_path": "",
        "thinking_enabled": False,
        "thinking_effort": None,
    }


def test_engine_runtime_fields_pi_binary_without_pi_overrides() -> None:
    from ach_agent.main import _engine_runtime_fields

    fields = _engine_runtime_fields(_wiring_cfg("pi", None, True, "high"))
    assert fields == {
        "binary_path": "pi",
        "pi_mcp_adapter_path": "",
        "thinking_enabled": True,
        "thinking_effort": "high",
    }


def test_engine_runtime_fields_from_pi_block_and_model_thinking() -> None:
    from ach_agent.config.schema import PiEngineBlock
    from ach_agent.main import _engine_runtime_fields

    pi_block = PiEngineBlock(
        binaryPath="pi",
        mcpAdapterPath="/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
    )
    fields = _engine_runtime_fields(_wiring_cfg("pi", pi_block, True, "medium"))
    assert fields == {
        "binary_path": "pi",
        "pi_mcp_adapter_path": "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
        "thinking_enabled": True,
        "thinking_effort": "medium",
    }


def test_engine_runtime_fields_thinking_flows_to_opencode_too() -> None:
    from ach_agent.main import _engine_runtime_fields

    fields = _engine_runtime_fields(_wiring_cfg("opencode", None, True, "low"))
    assert fields["thinking_enabled"] is True
    assert fields["thinking_effort"] == "low"
```

`tests/e2e/test_pi_e2e.py` — three surgical lines inside
`test_pi_reasoning_model_reports_resolved_thinking_level` (every assertion in the test
stays byte-identical, including the apiKey assertion as landed by P-1): delete the
`from ach_agent.engine.base.driver import PiModelCapability` line, and replace the two
kwargs

```python
            pi_model_capability=PiModelCapability(reasoning=True),
            pi_thinking_level="high",
```

with:

```python
            thinking_enabled=True,
            thinking_effort="high",
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/pi/ tests/test_main_wiring.py -v -k "thinking or engine_runtime or descriptor"`
Expected: FAIL — `EngineConfig` has no `thinking_enabled`/`thinking_effort` kwargs;
`_engine_runtime_fields` doesn't exist.

- [ ] **Step 3: Swap the `EngineConfig` fields**

Edit `src/ach_agent/engine/base/driver.py`: **delete** the `PiModelCapability` dataclass
(lines 20-30) entirely, and inside `EngineConfig` **replace**

```python
    # CONTRACT engine.pi.model — Pi-only capability descriptor for models.json. Default
    # matches Pi's own builtin defaults, so an absent engine.pi.model behaves exactly
    # like today's hardcoded output.
    pi_model_capability: PiModelCapability = field(default_factory=PiModelCapability)
    # CONTRACT engine.pi.thinkingLevel — the --thinking level passed to pi at launch.
    # None → no --thinking flag (Pi's own default behavior). Already validated at config
    # load time (schema.py's PiEngineBlock requires reasoning=true when this is set).
    pi_thinking_level: str | None = None
```

with:

```python
    # CONTRACT model.thinking — normalized, engine-neutral reasoning intent. Each driver
    # translates it: pi → models.json descriptor `reasoning` + `--thinking <effort>`;
    # opencode → per-call providerOptions merged into the generated model options
    # (lifecycle._thinking_options; explicit model.params keys win on collision).
    # Already validated at config load (schema.ThinkingBlock: effort requires enabled).
    thinking_enabled: bool = False
    thinking_effort: str | None = None
```

- [ ] **Step 4: Pi translation — `models_json.py` + `driver.py`**

Replace `src/ach_agent/engine/pi/models_json.py` in full with (note: this preserves the
P-1-landed `"$PI_LOCAL_PROXY_API_KEY"` apiKey and its comment verbatim):

```python
# SPDX-License-Identifier: Apache-2.0
"""Build Pi's models.json using the localhost model proxy."""

from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import EngineConfig

_PI_PROVIDER_BY_TYPE: dict[str, tuple[str, str]] = {
    "openai": ("ach-openai", "openai-completions"),
    "gemini": ("ach-gemini", "google-generative-ai"),
    "anthropic": ("ach-anthropic", "anthropic-messages"),
}


def build_models_json(cfg: EngineConfig) -> tuple[dict[str, Any], str]:
    """Return the models document and provider name passed to Pi."""
    provider, api = _PI_PROVIDER_BY_TYPE.get(cfg.model_type, _PI_PROVIDER_BY_TYPE["openai"])
    model = {
        "id": cfg.model,
        "name": cfg.model,
        # model.thinking is the only operator-facing model surface beyond identity/params:
        # `reasoning` is derived from thinking.enabled. The remaining descriptor fields are
        # Pi's own builtin defaults, byte-identical to v0.8.0 — deliberately NOT
        # configurable (the v0.8.1 engine.pi.model surface was removed in v0.9.0).
        "reasoning": cfg.thinking_enabled,
        "input": ["text"],
        "contextWindow": 128000,
        "maxTokens": 16384,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    doc: dict[str, Any] = {
        "providers": {
            provider: {
                "api": api,
                "baseUrl": cfg.model_base_url,
                # Pi resolves provider keys from environment-variable references. The value is
                # a harmless localhost-proxy sentinel, never the ACH ek_.
                "apiKey": "$PI_LOCAL_PROXY_API_KEY",
                "headers": {},
                "models": [model],
            }
        }
    }
    return doc, provider
```

Edit `src/ach_agent/engine/pi/driver.py` inside `_common_args` (P-1 shape) — replace:

```python
        if cfg.pi_thinking_level is not None:
            args.extend(["--thinking", cfg.pi_thinking_level])
```

with:

```python
        if cfg.thinking_effort is not None:
            # Effort values are a strict subset of Pi's --thinking levels (identity map);
            # schema validation guarantees effort ⇒ enabled ⇒ models.json reasoning:true.
            args.extend(["--thinking", cfg.thinking_effort])
```

This lands the flag in **both** RPC (`launch`) and native-TUI (`run_tui`) paths, since P-1
made both share `_common_args` — the TUI process simply receives the same generated
`models.json` and `--thinking` translation. That is a config-parity statement only: like
opencode's native TUI attach, Pi's native TUI bypasses the `engine_runner`/`StatsSink`
seam, so neither engine's `--tui` mode emits session/tool telemetry (see Global
Constraints — engine-neutral, out of scope here; agentic paths keep full parity).

- [ ] **Step 5: `main.py` wiring**

Replace the whole `_pi_engine_fields` function (P-1 shape, above `async def main(`) with:

```python
def _engine_runtime_fields(cfg: Any) -> dict[str, Any]:
    """engine.type/engine.pi -> executable-selection EngineConfig kwargs, plus the
    normalized model.thinking intent every engine translates for itself.

    engine.pi carries ONLY executable knobs (binaryPath/mcpAdapterPath); model identity
    and thinking/reasoning intent live in the model block (CONTRACT §2 model.thinking).
    A Pi config with no engine.pi sub-block still launches the image's `pi` binary.
    """
    thinking = {
        "thinking_enabled": cfg.model.thinking.enabled,
        "thinking_effort": cfg.model.thinking.effort,
    }
    if cfg.engine.type != "pi":
        return {"binary_path": "opencode", "pi_mcp_adapter_path": "", **thinking}
    pi = cfg.engine.pi
    if pi is None:
        return {"binary_path": "pi", "pi_mcp_adapter_path": "", **thinking}
    return {
        "binary_path": pi.binary_path,
        "pi_mcp_adapter_path": pi.mcp_adapter_path,
        **thinking,
    }
```

and change its single call site in the `engine_cfg = EngineConfig(` construction from
`**_pi_engine_fields(cfg),` to `**_engine_runtime_fields(cfg),`.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/engine/pi/ tests/test_main_wiring.py -v`
Expected: all PASS, including every pre-existing test (default-argv tests see no
`--thinking`; provider-mapping tests see the unchanged default descriptor).

Run: `uv run pytest tests/ -q --ignore=tests/e2e && uv run pytest tests/e2e/test_pi_e2e.py -q`
Expected: full non-e2e suite PASS; the real-subprocess e2e PASSES locally (pi 0.81.1 +
adapter present in this environment) — the resolved `get_state` still reports
`thinkingLevel == "high"` / `model.reasoning is True`, now driven by `model.thinking`.

- [ ] **Step 7: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/ src/ach_agent/main.py tests/engine/pi/ tests/test_main_wiring.py tests/e2e/test_pi_e2e.py && uv run ruff format --check src/ach_agent/engine/ src/ach_agent/main.py tests/engine/pi/ tests/test_main_wiring.py tests/e2e/test_pi_e2e.py && uv run mypy --strict src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/ src/ach_agent/main.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/models_json.py src/ach_agent/engine/pi/driver.py src/ach_agent/main.py tests/engine/pi/test_models_json.py tests/engine/pi/test_driver.py tests/test_main_wiring.py tests/e2e/test_pi_e2e.py
git commit -m "feat(engine)!: drivers translate model.thinking (pi reasoning + --thinking)"
```

---

## Task 3: opencode translation — `model.thinking` → generated model options

**Files:**
- Modify: `src/ach_agent/engine/lifecycle.py` (add `_thinking_options`; merge into
  `write_opencode_config`'s model options)
- Test: `tests/engine/test_lifecycle.py`

**Interfaces:**
- Consumes: `EngineConfig.thinking_enabled` / `.thinking_effort` (Task 2);
  `write_opencode_config(ephemeral_home, config, session_key) -> Path` (unchanged
  signature).
- Produces: `_thinking_options(model_type: str, enabled: bool, effort: str | None) ->
  dict[str, object]` (module-level, testable, opencode-local — Pi never imports it).

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_lifecycle.py` (reuse the file's existing imports of
`EngineConfig`/`write_opencode_config`/`json`/`Path`; add any of these that are missing at
the top, matching the file's import style):

```python
def test_thinking_translates_to_openai_reasoning_effort(tmp_path: Path) -> None:
    cfg = EngineConfig(
        model="m1",
        model_type="openai",
        model_base_url="http://127.0.0.1:1/v1",
        thinking_enabled=True,
        thinking_effort="high",
    )
    path = write_opencode_config(tmp_path, cfg, "k-think-openai")
    doc = json.loads(path.read_text(encoding="utf-8"))
    options = doc["provider"]["ach"]["models"]["m1"]["options"]
    assert options["reasoningEffort"] == "high"


def test_thinking_translates_per_wire_for_gemini_and_anthropic(tmp_path: Path) -> None:
    gemini_cfg = EngineConfig(
        model="g1",
        model_type="gemini",
        model_base_url="http://127.0.0.1:1/gemini",
        thinking_enabled=True,
        thinking_effort="medium",
    )
    path = write_opencode_config(tmp_path, gemini_cfg, "k-think-gemini")
    doc = json.loads(path.read_text(encoding="utf-8"))
    gemini_options = doc["provider"]["google"]["models"]["g1"]["options"]
    assert gemini_options["thinkingConfig"] == {"thinkingLevel": "medium"}

    anthropic_cfg = EngineConfig(
        model="a1",
        model_type="anthropic",
        model_base_url="http://127.0.0.1:1/anthropic",
        thinking_enabled=True,
        thinking_effort="high",
    )
    path = write_opencode_config(tmp_path, anthropic_cfg, "k-think-anthropic")
    doc = json.loads(path.read_text(encoding="utf-8"))
    anthropic_options = doc["provider"]["anthropic"]["models"]["a1"]["options"]
    assert anthropic_options["thinking"] == {"type": "enabled", "budgetTokens": 24576}


def test_thinking_xhigh_maps_per_wire(tmp_path: Path) -> None:
    for model_type, base_path, provider_id, expected in (
        ("openai", "/v1", "ach", {"reasoningEffort": "xhigh"}),
        ("gemini", "/gemini", "google", {"thinkingConfig": {"thinkingLevel": "high"}}),
        (
            "anthropic",
            "/anthropic",
            "anthropic",
            {"thinking": {"type": "enabled", "budgetTokens": 32000}},
        ),
    ):
        cfg = EngineConfig(
            model="m1",
            model_type=model_type,
            model_base_url=f"http://127.0.0.1:1{base_path}",
            thinking_enabled=True,
            thinking_effort="xhigh",
        )
        path = write_opencode_config(tmp_path, cfg, f"k-xhigh-{model_type}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        options = doc["provider"][provider_id]["models"]["m1"]["options"]
        for key, value in expected.items():
            assert options[key] == value


def test_thinking_disabled_or_effortless_injects_nothing(tmp_path: Path) -> None:
    for kwargs in ({}, {"thinking_enabled": True}):
        cfg = EngineConfig(
            model="m1", model_type="openai", model_base_url="http://127.0.0.1:1/v1", **kwargs
        )
        path = write_opencode_config(tmp_path, cfg, f"k-none-{len(kwargs)}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        options = doc["provider"]["ach"]["models"]["m1"]["options"]
        assert "reasoningEffort" not in options
        assert "thinking" not in options
        assert "thinkingConfig" not in options


def test_explicit_model_params_win_over_thinking_translation(tmp_path: Path) -> None:
    cfg = EngineConfig(
        model="m1",
        model_type="openai",
        model_base_url="http://127.0.0.1:1/v1",
        params={"reasoningEffort": "low", "temperature": 1},
        thinking_enabled=True,
        thinking_effort="high",
    )
    path = write_opencode_config(tmp_path, cfg, "k-params-win")
    doc = json.loads(path.read_text(encoding="utf-8"))
    options = doc["provider"]["ach"]["models"]["m1"]["options"]
    assert options["reasoningEffort"] == "low"  # params passthrough stays supreme
    assert options["temperature"] == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/test_lifecycle.py -v -k thinking`
Expected: FAIL — no translation is injected yet (`reasoningEffort` missing).

- [ ] **Step 3: Implement `_thinking_options` and merge it**

Edit `src/ach_agent/engine/lifecycle.py`. Directly above
`def write_opencode_config(` (line 151), add:

```python
# model.thinking → opencode per-call providerOptions, keyed by the model wire
# (model.type — same switch as _PROVIDER_BY_TYPE). Merged UNDER model.params in the
# generated model options: the operator's explicit params keys win on collision, so
# params keeps its provider-specific-passthrough supremacy. `enabled` without `effort`
# injects nothing — the provider's own default thinking behavior applies.
# ponytail: anthropic budgets are chosen constants (provider minimum is 1024); make them
# configurable only if a real deployment needs different budgets.
_ANTHROPIC_THINKING_BUDGET: dict[str, int] = {
    "minimal": 1024,
    "low": 4096,
    "medium": 10000,
    "high": 24576,
    "xhigh": 32000,
}


def _thinking_options(model_type: str, enabled: bool, effort: str | None) -> dict[str, object]:
    """Translate the normalized model.thinking block into provider options."""
    if not enabled or effort is None:
        return {}
    if model_type == "gemini":
        # gemini's thinkingLevel vocabulary has no xhigh — clamp to its strongest level.
        level = "high" if effort == "xhigh" else effort
        return {"thinkingConfig": {"thinkingLevel": level}}
    if model_type == "anthropic":
        return {
            "thinking": {"type": "enabled", "budgetTokens": _ANTHROPIC_THINKING_BUDGET[effort]}
        }
    return {"reasoningEffort": effort}
```

Then in `write_opencode_config`, replace the model-registration line (currently
`"models": {config.model: {"options": dict(config.params)}},` at line 204) with:

```python
        "models": {
            config.model: {
                "options": {
                    **_thinking_options(
                        config.model_type, config.thinking_enabled, config.thinking_effort
                    ),
                    **dict(config.params),
                }
            }
        },
```

(Keep the existing comment block above it; it already documents that these options are
opencode's per-call providerOptions and names the per-wire knobs.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/engine/test_lifecycle.py tests/engine/test_codemem_opencode_config.py tests/engine/test_lifecycle_mcp.py tests/conformance/test_inv12_secret_hygiene.py -v`
Expected: all PASS — pre-existing `write_opencode_config` consumers see byte-identical
output for default `EngineConfig`s (empty translation dict merges to nothing).

Run: `uv run pytest tests/ -q --ignore=tests/e2e`
Expected: all PASS.

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py && uv run ruff format --check src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py && uv run mypy --strict src/ach_agent/engine/lifecycle.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py
git commit -m "feat(opencode): translate model.thinking into generated model options"
```

---

## Task 4: CONTRACT_v3 + decision record + supersede markers

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md:98-141`
- Create: `docs/references/2026-07-24-model-owned-thinking.md`
- Modify: `docs/references/2026-07-23-pi-model-capability-and-thinking.md` (status line)
- Modify: `docs/references/README.md` (index rows)
- Modify: `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity.md` (banner)
- Modify: `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md` (banner)

**Interfaces:** docs only; Task 5's handoff cites the CONTRACT text written here.

- [ ] **Step 1: Rewrite CONTRACT_v3 §2's model + engine.pi comments**

In `docs/plan/CONTRACT_v3.md`, replace the `"model"` block (lines 98-102) with:

```jsonc
  "model": {
    "name": "openai.gpt-5",                 // model id, passed verbatim; MUST be in hydrated models
    "type": "openai",                       // openai | gemini | anthropic — picks the ACH compat endpoint
    "params": { "temperature": 1 },         // OPEN, UNVALIDATED dict, splatted to the model client
    "thinking": {                           // normalized, engine-neutral reasoning intent — the
      "enabled": false,                     //   CANONICAL thinking surface (never params). Strict bool.
      "effort": null                        // minimal|low|medium|high|xhigh; requires
      //                                       enabled=true (hard-fail otherwise). `max` and
      //                                       provider-specific levels (e.g. ultracode) are NOT
      //                                       portable — reach them via params passthrough.
      //                                       Each engine translates it: pi → models.json
      //                                       `reasoning:<enabled>` + `--thinking <effort>` at
      //                                       launch (identity, incl. xhigh); opencode →
      //                                       per-call providerOptions in the generated model
      //                                       options (openai reasoningEffort incl. xhigh;
      //                                       gemini thinkingConfig.thinkingLevel, xhigh clamps
      //                                       to high; anthropic thinking budgetTokens
      //                                       1024/4096/10000/24576/32000), merged UNDER
      //                                       params — explicit params keys win on collision.
    }
  },
```

and replace the `"pi"` sub-block (lines 124-140) with:

```jsonc
    "pi": {                                   // PiEngineBlock; consulted only when type == "pi".
      "binaryPath": "pi",                     //   ONLY executable knobs — model identity and
      "mcpAdapterPath": ""                     //   thinking live in the model block above. "" →
      //                                          image default:
      //                                          /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter.
      //                                          (engine.pi.model/thinkingLevel existed only in
      //                                          v0.8.1 and were removed in v0.9.0.)
    }
```

- [ ] **Step 2: Write the new decision record**

Create `docs/references/2026-07-24-model-owned-thinking.md`:

```markdown
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
    xhigh level), anthropic → `thinking.budgetTokens` (1024/4096/10000/24576/32000 for
    minimal/low/medium/high/xhigh; chosen constants, provider minimum 1024).
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
```

- [ ] **Step 3: Mark the superseded artifacts**

In `docs/references/2026-07-23-pi-model-capability-and-thinking.md`, change the status line
to:

```markdown
**Date:** 2026-07-23 · **Status:** Superseded by [2026-07-24-model-owned-thinking](2026-07-24-model-owned-thinking.md) (surface removed in v0.9.0)
```

In `docs/references/README.md`: change that row's Status cell to `Superseded` and add a new
row for `2026-07-24-model-owned-thinking` (Status `Shipped`, hook: "thinking intent moves to
a normalized `model.thinking`; `engine.pi` back to executable knobs only; each engine
translates").

Prepend to BOTH `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity.md` and
`docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md` (directly under
the H1):

```markdown
> **SUPERSEDED (2026-07-24):** the `engine.pi.model`/`engine.pi.thinkingLevel` surface this
> document introduced was removed in v0.9.0. See
> `docs/superpowers/plans/2026-07-24-model-thinking-supersedes-engine-pi-model.md` and
> `docs/references/2026-07-24-model-owned-thinking.md`. Do not execute or hand off from
> this document.
```

- [ ] **Step 4: Verify + commit**

Run: `uv run pytest tests/config/test_schema_artifact.py -q` (docs-only change — proves no
accidental artifact edit). Expected: PASS.

```bash
git add docs/plan/CONTRACT_v3.md docs/references/2026-07-24-model-owned-thinking.md docs/references/2026-07-23-pi-model-capability-and-thinking.md docs/references/README.md docs/superpowers/plans/2026-07-23-pi-model-runtime-parity.md docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md
git commit -m "docs(contract): model.thinking canonical; supersede engine.pi.model records"
```

---

## Task 5: `../ach` correction handoff (document only — NEVER executed from this repo)

**Files:**
- Create: `docs/superpowers/plans/2026-07-24-model-thinking-handoff-ach.md`

**Interfaces:** consumes the CONTRACT text + regenerated schema artifact from Tasks 1/4.
Executed later by the ACH operator agent inside `../ach`.

- [ ] **Step 1: Write the handoff document**

Create `docs/superpowers/plans/2026-07-24-model-thinking-handoff-ach.md` with exactly this
content:

````markdown
# Handoff prompt — `../ach`: correct the unapproved Pi-engine surface for model-owned thinking

> **Self-contained prompt for the ACH operator agent working in `../ach`
> (`github.com/ackstorm/ach`). Do NOT execute it in `ach-agent`.**
>
> **Hard precondition:** an `ach-agent` release ≥ v0.9.0 (carrying `model.thinking` and the
> `engine.pi.model`/`thinkingLevel` REMOVAL) must be released before any `AgentProfile`
> renders `model.thinking` — and conversely, a pre-0.9.0 harness rejects nothing here
> except `model.thinking` itself (`extra="forbid"`), so do not push these renders until the
> v0.9.0 image is the deployed one.

## Why a correction

Local commits `319bdae` (CRD), `41efd46` (render), `49d503b` (schema re-sync) encode the
superseded `engine.pi.model`/`engine.pi.thinkingLevel` surface (`PiModelSpec`,
`PiEngineSpec.Model/ThinkingLevel`, `PiModelBlock`). ach-agent v0.9.0 removed that surface:
thinking intent is now the normalized `model.thinking` block, and `engine.pi` carries only
`binaryPath`/`mcpAdapterPath`. Do NOT proceed to the local plan's Task 4 (`make e2e-full`
gate) until this correction has landed.

## Step 0 — inspect, then STOP for human direction (no autonomous history rewrite)

```bash
git log --oneline origin/main..HEAD
git status --porcelain
```

Report exactly what you find (expected: `49d503b`, `41efd46`, `319bdae` and a clean tree),
then **STOP and ask Juan Carlos how to dispose of those commits** — e.g. correct them with
forward commits on top, or rewrite/squash them locally. **Never run `git reset`,
`git rebase`, or any other history rewrite yourself**; if any commit has been pushed, or
the log/status differs from the expected state, that is doubly a human decision. Only
after explicit direction, land the corrective end-state below using the mechanics Juan
Carlos chose. The end-state is the same either way:

**Change 1 — `feat(api): engine.type + engine.pi executable knobs + model.thinking`**
(`api/ach/v1alpha1/agentprofile_types.go` + regenerated deepcopy/CRDs/API-reference +
`examples/agent-runtime/profile.yaml`):

Keep `EngineSpec.Type` and `PiEngineSpec` exactly as committed, but **delete `PiModelSpec`
entirely and the `Model`/`ThinkingLevel` fields of `PiEngineSpec`**, leaving:

```go
// PiEngineSpec is the harness-local Pi engine block (config: engine.pi.*) — executable
// knobs ONLY (model identity/thinking live in ModelSpec). All fields are optional; empty
// binaryPath/mcpAdapterPath fall back to the image defaults (pi on PATH; the vendored
// adapter at /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter).
type PiEngineSpec struct {
	// +optional
	BinaryPath string `json:"binaryPath,omitempty"`
	// +optional
	McpAdapterPath string `json:"mcpAdapterPath,omitempty"`
}
```

Extend `ModelSpec` (after `Params`):

```go
	// Thinking is the normalized model-level reasoning intent (config: model.thinking).
	// Free-form (no Enum) — ach-agent's Pydantic ThinkingBlock is the single enforcer
	// (D-2 precedent): effort one of minimal|low|medium|high|xhigh, requires enabled=true.
	// +optional
	Thinking *ThinkingSpec `json:"thinking,omitempty"`
```

```go
// ThinkingSpec is the normalized reasoning intent each engine translates for itself.
type ThinkingSpec struct {
	// +optional
	Enabled bool `json:"enabled,omitempty"`
	// +optional
	Effort string `json:"effort,omitempty"`
}
```

Update `examples/agent-runtime/profile.yaml`'s engine.pi example accordingly (drop
model/thinkingLevel; optionally show `model.thinking`). Regenerate:
`make manifests generate` (CRD YAMLs + zz_generated.deepcopy.go + docs/api-reference).

**Change 2 — `feat(agentrender): render engine.type/engine.pi + model.thinking`**
(`internal/agentrender/config.go`, `render.go`, `render_test.go`):

`config.go`: keep `EngineBlock.Type`/`Pi`; `PiBlock` shrinks to
`BinaryPath`/`McpAdapterPath`; **delete `PiModelBlock`**; add:

```go
type ThinkingBlock struct {
	Enabled bool   `json:"enabled"`
	Effort  string `json:"effort,omitempty"`
}
```

and extend `ModelBlock` with `Thinking *ThinkingBlock \`json:"thinking,omitempty"\``.

`render.go`: `renderEngine`'s `if e.Pi != nil` branch renders only the two executable
knobs (drop the Model/ThinkingLevel mapping); the `Model: ModelBlock{...}` construction
(render.go:56) gains:

```go
	var thinking *ThinkingBlock
	if model.Thinking != nil {
		thinking = &ThinkingBlock{Enabled: model.Thinking.Enabled, Effort: model.Thinking.Effort}
	}
```

with `Thinking: thinking` added to the `ModelBlock{...}` literal.

`render_test.go`: keep `TestRenderEnginePi` (binaryPath/mcpAdapterPath); **replace**
`TestRenderEnginePiModelCapability` with:

```go
func TestRenderModelThinking(t *testing.T) {
	p := achv1alpha1.AgentProfile{Spec: achv1alpha1.AgentProfileSpec{
		Image: "x", Ach: achv1alpha1.AchEndpointSpec{BaseURL: "u"},
		Model: &achv1alpha1.ModelSpec{
			Name: "m", Type: "openai",
			Thinking: &achv1alpha1.ThinkingSpec{Enabled: true, Effort: "high"},
		},
	}}
	c := renderConfig(&p) // use this file's existing render entry-point helper
	if c.Model.Thinking == nil || !c.Model.Thinking.Enabled || c.Model.Thinking.Effort != "high" {
		t.Fatalf("Model.Thinking = %+v, want enabled=true effort=high", c.Model.Thinking)
	}
}
```

(Adapt the construction to this file's existing test helpers/entry point — the assertion is
what matters: `model.thinking {enabled:true, effort:"high"}` lands in the rendered config.)

**Change 3 — `test(agentrender): re-sync schema, cover pi engine + model.thinking`**:
copy ach-agent's regenerated `docs/schemas/agent-config-v1.schema.json` (from the v0.9.0
tree) over `internal/agentrender/testdata/agent-config-v1.schema.json`, and update the
conformance matrix case from the pi-capability profile to a
`engine.type=pi` + `model.thinking` profile.

## Verify

```bash
make manifests generate
go build ./... && go test ./internal/agentrender/... ./api/...
```

Then — and only then — run the local plan's Task 4 gate (`make e2e-full`) and update/annotate
the local plan doc `docs/superpowers/plans/2026-07-24-pi-engine-type-and-model-render.md`
(untracked) so it reflects this surface.

## Constraints

- `ThinkingSpec` stays free-form — no `+kubebuilder:validation:Enum` (D-2; ach-agent's
  Pydantic layer is the single enforcer).
- ek hygiene unchanged: booleans/strings only, never secrets.
- Ship order: ach-agent v0.9.0 image released and deployed BEFORE these renders push.
````

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-07-24-model-thinking-handoff-ach.md
git commit -m "docs(plan): ../ach correction handoff for model-owned thinking"
```

---

## Task 6: Version bump v0.9.0 + release marker

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`

- [ ] **Step 1: Bump**

`CHANGELOG.md` — add under `## [unreleased]`:

```markdown
## [0.9.0] - 2026-07-24

### Changed
- **BREAKING:** `engine.pi.model` and `engine.pi.thinkingLevel` (v0.8.1-only) are removed;
  `engine.pi` carries only `binaryPath`/`mcpAdapterPath`. Thinking/reasoning intent moves
  to the normalized `model.thinking` block (`enabled` +
  `effort: minimal|low|medium|high|xhigh`), translated per engine: Pi → `models.json`
  `reasoning` + `--thinking`; opencode → per-call providerOptions merged under
  `model.params` (explicit params win).
```

`pyproject.toml`: `version = "0.9.0"`. Then `uv lock` (syncs the self-entry).

- [ ] **Step 2: Verify the full gates**

Run: `make lint && make test && make conformance`
Expected: all green (router conformance untouched by this plan — nothing in `router/`
changed).

- [ ] **Step 3: Commit + marker (push = release trigger; get explicit go-ahead first)**

```bash
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore(release): bump version to 0.9.0"
git commit --allow-empty -m "chore(release): v0.9.0"
# git push origin main   ← CI parses the HEAD marker and cuts the release.
# Do NOT push without Juan Carlos's explicit release approval; never create the tag locally.
```

---

## Self-Review (done at plan-authoring time)

- **Scope coverage:** engine.type=runtime-only ✔ (untouched); engine.pi=executable knobs ✔
  (Task 1); model-owned normalized thinking ✔ (Tasks 1-3); per-engine translation incl.
  OpenCode config + Pi models.json/`--thinking` ✔ (Tasks 2-3); Pi descriptor defaults
  preserved, no new capability/pricing fields ✔ (Task 2); no /hydrate change ✔ (no task
  touches `hydrate.py`); CONTRACT/handoff rework ✔ (Tasks 4-5); `../ach` Task 1-3
  correction instead of advancing their Task 4 ✔ (Task 5); native-TUI telemetry
  explicitly out of scope for BOTH engines' `--tui` paths (each bypasses
  `engine_runner`/`StatsSink`; agentic paths keep equal stats parity; future TUI
  observation = common engine-neutral feature) — only config translation is claimed
  (Global Constraints + Task 2 Step 4); cross-repo order ✔ (P-1 reviewed/committed
  separately → Tasks 1-6 → v0.9.0 release → `../ach` correction, with commit disposition
  decided by Juan Carlos at the handoff's Step 0 → their Task 4 gate).
- **Type consistency:** `ThinkingBlock.enabled/effort` (schema) →
  `EngineConfig.thinking_enabled/thinking_effort` → consumed by `build_models_json`,
  `_common_args`, `_thinking_options`, `_engine_runtime_fields` — names match across tasks.
- **Placeholder scan:** none; every step carries the code or exact edit.
