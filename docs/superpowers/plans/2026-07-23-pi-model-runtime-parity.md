# Pi Model Runtime Parity — Corrective Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the gap between `engine.pi` (shipped in v0.8.0) and a known-good direct
Pi setup: every Pi model is currently forced to `reasoning: false`, `input: ["text"]`, a
128K/16K context/output ceiling, and no `--thinking` level, regardless of what the
operator actually wants. Give Pi's per-model capability and thinking preference a
**typed, generated, operator-legible** home — NOT the existing `model.params` open
dict, which is contractually **provider-call passthrough** (splatted verbatim to the
model API request) and would hide Pi-only concepts from
`docs/schemas/agent-config-v1.schema.json`, the artifact the Kubernetes operator author
actually reads. No model ID is ever branched on, and no model is assumed to reason.

**Architecture:** A typed `engine.pi.model` capability block
(`reasoning`/`input`/`contextWindow`/`maxTokens`) plus a sibling `engine.pi.thinkingLevel`
join the existing `engine.pi.{binaryPath,mcpAdapterPath}` sub-block
(`src/ach_agent/config/schema.py`'s `PiEngineBlock`) — engine-local, exactly like the
fields already there, and **generated** into `docs/schemas/agent-config-v1.schema.json`
via the existing `scripts/gen_schema.py` + `tests/config/test_schema_artifact.py` drift
guard (`AgentConfig` is that artifact's single source of truth — see
`scripts/gen_schema.py:1-11`). `model.params` (`ModelBlock.params`,
`src/ach_agent/config/schema.py:38-46`) is untouched and keeps its existing, narrower
meaning: an open, unvalidated dict splatted to the model API call itself (temperature,
top_p, …) — never Pi's own local descriptor. `model.type` → provider/wire selection
(`_PI_PROVIDER_BY_TYPE`) is untouched — no evidence in this investigation requires
changing it. `main.py`'s existing engine-local wiring pattern (`cfg.engine.pi.binary_path`
→ `EngineConfig.pi_mcp_adapter_path`, already there for `mcpAdapterPath`) is extended,
not replaced. Provider and model are still selected by explicit `--provider`/`--model`
CLI flags at Pi subprocess launch (unchanged); `--thinking <level>` joins them the same
way, **not** via Pi's `settings.json` `defaultProvider`/`defaultModel`/
`defaultThinkingLevel` — those are interactive-session defaults, irrelevant to a
harness-launched, one-shot-per-invocation RPC subprocess that already gets deterministic
CLI flags for provider/model. A real-`pi`-subprocess test proves Pi itself reports the
resolved `reasoning`/`thinkingLevel` back over RPC (`get_state`, with id+command
validated on every response), asserts the pinned `pi --version` exactly, and CI is
hardened so a missing `pi` binary **or** `pi-mcp-adapter` fails loudly instead of
silently skipping (local developer runs may still skip when either is absent). Closes with
CONTRACT_v3.md + a docs/references decision record, an exact `../ach` CRD/render handoff
(the schema lives in `ach-agent`; the operator that renders `AgentProfile.engine.pi` into
this schema lives in `../ach`), and a v0.8.1 version bump — per SP2's ship-order rule,
`../ach` must not advertise Pi reasoning support ahead of a released ach-agent image
carrying this fix.

**Tech Stack:** Python 3.12, Pydantic v2 (`StrictBool`, `StrictInt`, `Literal`,
`field_validator`, `model_validator`, `Field(gt=0)`), pytest (`asyncio_mode=auto`), `jsonschema`
(`Draft202012Validator`, already a dependency of `tests/config/test_schema_artifact.py`),
mypy `--strict`, ruff — no new dependency. Real `pi` binary 0.81.1 (already on `PATH` in
this environment) and the vendored `pi-mcp-adapter` (already present at
`~/.pi/agent/npm/node_modules/pi-mcp-adapter` in this environment) exercise the
real-subprocess task; `.github/workflows/ci.yml`'s `e2e-pi` job already installs both
from the Dockerfile's pinned `ARG PI_VERSION`/`ARG PI_MCP_ADAPTER_VERSION` and already
runs `tests/e2e/test_pi_e2e.py` directly (not the container-wrapped `make` targets), so
it is not currently skipped in CI — Task 4 adds belt-and-suspenders guards so that stays
true even if the install step ever silently stops installing either dependency.

## Global Constraints

- **`model.params` keeps its existing, narrower meaning** — an open, unvalidated dict
  (`src/ach_agent/config/schema.py:38-46`) splatted to the model API call. This plan adds
  **no** keys to it and reads **none** of them for Pi's capability/thinking. (Correction:
  the prior draft of this plan proposed reusing `model.params` for this and claimed no
  CONTRACT/schema change was needed — that was wrong. `model.params`'s own JSON Schema
  entry is (and must stay) `{"type": "object"}` — opaque to the operator by design,
  because it is genuinely open/unvalidated provider-call passthrough. Hiding Pi-specific,
  well-known keys inside it would make `docs/schemas/agent-config-v1.schema.json` lie
  about what `engine.pi` actually accepts.)
- **The new capability/thinking fields ARE a CONTRACT/schema change**, by design: a typed
  `engine.pi.model` block + `engine.pi.thinkingLevel`, added to
  `src/ach_agent/config/schema.py`'s `PiEngineBlock` (the existing Pydantic single source
  of truth — `scripts/gen_schema.py:21`), regenerated into
  `docs/schemas/agent-config-v1.schema.json`, documented in `docs/plan/CONTRACT_v3.md`,
  and handed off to `../ach` as an exact CRD/render diff (Task 6) — mirroring
  `docs/superpowers/plans/2026-07-23-pi-engine-driver-sp2/handoff-ach-render.md`'s
  existing style for `engine.type`/`engine.pi.{binaryPath,mcpAdapterPath}`.
- **`model.type` stays authoritative for provider + wire** (`_PI_PROVIDER_BY_TYPE`:
  openai→`openai-completions`, gemini→`google-generative-ai`, anthropic→`anthropic-
  messages`). No task in this plan touches that mapping —
  `docs/references/2026-07-03-provider-by-model-type.md` stands as-is.
- **No model IDs hardcoded anywhere.** Every capability value comes from
  `engine.pi.model`/`engine.pi.thinkingLevel` or a documented, engine-neutral default;
  nothing branches on `cfg.model`.
- **Backward-compatible defaults.** When `engine.pi.model`/`thinkingLevel` are absent
  (or `engine.type != "pi"`): `reasoning: false`, `input: ["text"]`,
  `contextWindow: 128000`, `maxTokens: 16384` — Pi's own builtin defaults, byte-identical
  to today's hardcoded output — and no `--thinking` flag. The existing
  `tests/e2e/test_pi_e2e.py::test_pi_turn_and_ek_never_on_disk_or_in_subprocess` and
  `tests/engine/pi/test_models_json.py` tests must keep passing unmodified (only
  extracted-helper-level edits, no behavior change, in Task 4).
- **Recognized values are strictly typed and fail loudly.** `reasoning` is
  `StrictBool`; `contextWindow`/`maxTokens` are positive `StrictInt`s, so strings,
  floats, booleans, and other numeric/boolean coercions cannot pass. `input` allows
  exactly `["text"]` or `["text", "image"]`: empty lists, image-only lists, duplicates,
  unknown values, and any other order/shape fail;
  `thinkingLevel` is one of Pi's seven levels and **requires** `reasoning: true` (a
  `thinkingLevel` without `reasoning: true` is a hard-fail, not an implied auto-enable —
  the operator states both explicitly, matching the known-good direct Pi config where a
  model descriptor's `reasoning: true` and the thinking-level selection are both explicit
  settings). Every invalid case is an executable `pytest.raises(SystemExit)` test via the
  real `load_config` hard-fail path (`src/ach_agent/config/schema.py:721-723`), not an
  assertion about intent.
- **`../ach`'s free-string philosophy (D-2) extends to the new fields.** Per
  `docs/superpowers/plans/2026-07-23-pi-engine-driver-sp2/index.md`'s D-2 (`engine.type`
  is a free Go string; the harness is the enforcer), the new Go CRD fields
  (`PiEngineSpec.Model.Reasoning/Input/ContextWindow/MaxTokens`,
  `PiEngineSpec.ThinkingLevel`) are plain Go types with **no**
  `+kubebuilder:validation:Enum`/range annotations — ach-agent's Pydantic layer is the
  single enforcer, so the two repos never drift on what's "valid."
- **TDD.** Tasks 1-3 write a failing test first. Task 4 is a real-subprocess
  **verification** task (the acceptance test for the whole feature, run against the real
  `pi` binary) — not a new unit under fake/mocked TDD.
- **mypy `--strict`** passes for `src/ach_agent/config/schema.py`,
  `src/ach_agent/engine/base/driver.py`, `src/ach_agent/engine/pi/`, and `src/ach_agent/main.py`.
- **One commit per task; the tree stays green after every commit** — in particular, Task
  1 regenerates `docs/schemas/agent-config-v1.schema.json` in the *same* commit as the
  `PiEngineBlock` change, because `tests/config/test_schema_artifact.py`'s existing drift
  test would otherwise fail the moment the Pydantic model changes.
- **Do not `git add -A`; stage only the files named in that task.** Do not touch or
  stage the pre-existing dirty/untracked files reported by `git status` at
  plan-authoring time (`README.md`, `docs/index.md`,
  `docs/superpowers/specs/2026-07-23-pi-engine-driver-sp1-design.md`,
  `docs/superpowers/plans/2026-07-0{3,5,6,7}-*.md`) — they are unrelated in-progress work.

---

## Task 1: Typed `engine.pi.model` / `engine.pi.thinkingLevel` schema + regenerated artifact

**Files:**
- Modify: `src/ach_agent/config/schema.py:61-72` (`PiEngineBlock`)
- Modify: `docs/schemas/agent-config-v1.schema.json` (regenerated, not hand-edited)
- Test: `tests/config/test_schema.py`
- Test: `tests/config/test_schema_artifact.py`
- Create: `tests/config/fixtures/config_pi_reasoning.json`

**Interfaces:**
- Consumes: nothing new — extends the existing `PiEngineBlock` already wired as
  `EngineBlock.pi: PiEngineBlock | None` (`src/ach_agent/config/schema.py:104`), itself
  already part of `AgentConfig.engine` (`src/ach_agent/config/schema.py:672`).
- Produces: `PiModelCapabilities` (new `BaseModel`: `reasoning: StrictBool`,
  `input: list[Literal["text", "image"]]`, `context_window: int` (alias
  `contextWindow`, annotated with `StrictInt` + `Field(gt=0)`), `max_tokens: int`
  (alias `maxTokens`, annotated likewise)) and
  `PiEngineBlock.model: PiModelCapabilities` / `PiEngineBlock.thinking_level: Literal[...] | None`
  (alias `thinkingLevel`). Task 2 consumes `cfg.engine.pi.model.{reasoning,input,
  context_window,max_tokens}` and `cfg.engine.pi.thinking_level` by exactly these names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/config/test_schema.py` (near the other `_VALID_*_BASE` dicts, e.g.
right after `_VALID_CRON_BASE`'s block):

```python
def _pi_engine_base(**pi_overrides: object) -> dict:
    """_VALID_WEBHOOK_BASE with engine.type=pi; pi_overrides merge into engine.pi."""
    return {
        **_VALID_WEBHOOK_BASE,
        "engine": {
            "workDir": "/workspace",
            "startupTimeoutSeconds": 30,
            "type": "pi",
            "pi": {
                "binaryPath": "pi",
                "mcpAdapterPath": "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
                **pi_overrides,
            },
        },
    }


def test_pi_engine_model_capability_defaults_when_absent(tmp_path: Path) -> None:
    config = _load_raw(tmp_path, _pi_engine_base())
    assert config.engine.pi is not None
    assert config.engine.pi.model.reasoning is False
    assert config.engine.pi.model.input == ["text"]
    assert config.engine.pi.model.context_window == 128000
    assert config.engine.pi.model.max_tokens == 16384
    assert config.engine.pi.thinking_level is None


def test_pi_engine_model_capability_explicit_reasoning_and_thinking(tmp_path: Path) -> None:
    config = _load_raw(
        tmp_path,
        _pi_engine_base(
            model={
                "reasoning": True,
                "input": ["text", "image"],
                "contextWindow": 200000,
                "maxTokens": 32000,
            },
            thinkingLevel="high",
        ),
    )
    pi = config.engine.pi
    assert pi is not None
    assert pi.model.reasoning is True
    assert pi.model.input == ["text", "image"]
    assert pi.model.context_window == 200000
    assert pi.model.max_tokens == 32000
    assert pi.thinking_level == "high"


def test_pi_thinking_level_without_reasoning_hard_fails(tmp_path: Path) -> None:
    from ach_agent.config import load_config

    raw = _pi_engine_base(thinkingLevel="high")  # model.reasoning defaults to False
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


def test_pi_thinking_level_invalid_value_hard_fails(tmp_path: Path) -> None:
    from ach_agent.config import load_config

    raw = _pi_engine_base(model={"reasoning": True}, thinkingLevel="ultra")
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize(
    "invalid_input",
    [
        [],
        ["image"],
        ["text", "text"],
        ["image", "text"],
        ["text", "image", "image"],
        ["audio"],
    ],
)
def test_pi_model_input_rejects_every_shape_except_supported_ordered_shapes(
    tmp_path: Path, invalid_input: list[str]
) -> None:
    from ach_agent.config import load_config

    raw = _pi_engine_base(model={"input": invalid_input})
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("reasoning", "true"),
        ("reasoning", 1),
        ("contextWindow", "128000"),
        ("contextWindow", 128000.0),
        ("contextWindow", True),
        ("maxTokens", "16384"),
        ("maxTokens", 16384.0),
        ("maxTokens", False),
        ("maxTokens", 0),
    ],
)
def test_pi_model_strict_scalars_reject_coercion_and_non_positive_values(
    tmp_path: Path, field: str, invalid_value: object
) -> None:
    from ach_agent.config import load_config

    raw = _pi_engine_base(model={field: invalid_value})
    config_file = tmp_path / "config.json"
    config_file.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(SystemExit) as exc_info:
        load_config(str(config_file))
    assert exc_info.value.code != 0


@pytest.mark.parametrize(
    "invalid_input",
    [
        [],
        ["image"],
        ["text", "text"],
        ["image", "text"],
        ["text", "image", "image"],
        ["audio"],
    ],
)
def test_pi_model_input_error_is_value_validation_not_extra_field(
    invalid_input: list[str],
) -> None:
    """Keep the red phase honest: today's PiEngineBlock rejects the whole `model`
    field as extra, which must not masquerade as proof that these shapes are checked."""
    from pydantic import ValidationError

    from ach_agent.config.schema import PiModelCapabilities

    with pytest.raises(ValidationError) as exc_info:
        PiModelCapabilities.model_validate({"input": invalid_input})
    errors = exc_info.value.errors()
    assert any(error["loc"][0] == "input" for error in errors)
    assert all(error["type"] != "extra_forbidden" for error in errors)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("reasoning", "true"),
        ("reasoning", 1),
        ("contextWindow", "128000"),
        ("contextWindow", 128000.0),
        ("maxTokens", "16384"),
        ("maxTokens", 16384.0),
    ],
)
def test_pi_model_scalar_error_is_strict_validation_not_extra_field(
    field: str, invalid_value: object
) -> None:
    """Prove the field itself rejects coercion after it becomes a recognized key."""
    from pydantic import ValidationError

    from ach_agent.config.schema import PiModelCapabilities

    with pytest.raises(ValidationError) as exc_info:
        PiModelCapabilities.model_validate({field: invalid_value})
    errors = exc_info.value.errors()
    expected_loc = {
        "reasoning": ("reasoning",),
        "contextWindow": ("contextWindow",),
        "maxTokens": ("maxTokens",),
    }[field]
    assert any(error["loc"] == expected_loc for error in errors)
    assert all(error["type"] != "extra_forbidden" for error in errors)
```

`pytest` and `Path` are already imported at the top of `tests/config/test_schema.py`
(used by the existing `test_engine_block_hard_fails` etc.) — no new imports needed in
the test file.

Append to `tests/config/test_schema_artifact.py` so the generated operator-facing
artifact expresses the same exact ordered shapes as runtime validation:

```python
def test_pi_input_schema_exposes_only_supported_ordered_shapes() -> None:
    schema = json.loads(_ARTIFACT.read_text(encoding="utf-8"))
    input_schema = schema["$defs"]["PiModelCapabilities"]["properties"]["input"]
    validator = Draft202012Validator(input_schema)

    assert validator.is_valid(["text"])
    assert validator.is_valid(["text", "image"])
    for invalid in (
        [],
        ["image"],
        ["text", "text"],
        ["image", "text"],
        ["text", "image", "image"],
        ["audio"],
    ):
        assert not validator.is_valid(invalid)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/config/test_schema.py -v -k "pi_engine or pi_thinking or pi_model"`
Expected: the positive tests FAIL because `PiEngineBlock` has no
`model`/`thinking_level`; the direct `PiModelCapabilities` validation tests FAIL at
import because that type does not exist. The `load_config` negative tests may already
raise `SystemExit`, but only because today's `PiEngineBlock(extra="forbid")` rejects the
entire `model`/`thinkingLevel` key. They are therefore not counted as red-test proof on
their own: the direct tests above require field-local validation errors and explicitly
reject `extra_forbidden`, distinguishing the intended failures from that
pre-implementation false positive.

Run:
`uv run pytest tests/config/test_schema_artifact.py -v -k "pi_input_schema_exposes_only_supported_ordered_shapes"`
Expected: FAILS because the pre-implementation artifact has no
`$defs.PiModelCapabilities`; it cannot yet describe either accepted shape.

- [ ] **Step 3: Implement the typed capability block**

Edit `src/ach_agent/config/schema.py`. Replace lines 61-72 (the current `PiEngineBlock`)
with:

```python
class PiModelCapabilities(BaseModel):
    """Pi-only model capability descriptor (models.json fields; CONTRACT engine.pi.model).

    NOT `model.params` (the open, per-call passthrough dict on `ModelBlock` above) —
    these values are never sent to the model API call. The harness reads them to build
    Pi's `models.json` model descriptor. Absent fields fall back to Pi's own builtin
    defaults (same values), so an `engine.pi` block that omits `model` entirely behaves
    exactly like today's hardcoded output.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    reasoning: StrictBool = False
    input: list[Literal["text", "image"]] = Field(
        default_factory=lambda: ["text"],
        json_schema_extra={
            "oneOf": [
                {
                    "prefixItems": [{"const": "text"}],
                    "minItems": 1,
                    "maxItems": 1,
                },
                {
                    "prefixItems": [{"const": "text"}, {"const": "image"}],
                    "minItems": 2,
                    "maxItems": 2,
                },
            ]
        },
    )
    context_window: Annotated[StrictInt, Field(gt=0)] = Field(
        default=128000, alias="contextWindow"
    )
    max_tokens: Annotated[StrictInt, Field(gt=0)] = Field(
        default=16384, alias="maxTokens"
    )

    @field_validator("input")
    @classmethod
    def _supported_input_shapes(
        cls, value: list[Literal["text", "image"]]
    ) -> list[Literal["text", "image"]]:
        if value not in (["text"], ["text", "image"]):
            raise ValueError('must be exactly ["text"] or ["text", "image"]')
        return value


class PiEngineBlock(BaseModel):
    """Pi-engine sub-block (consulted only when engine.type == 'pi').

    `binaryPath` pins the `pi` executable; `mcpAdapterPath` is the vendored pi-mcp-adapter
    package path referenced from Pi's settings.json `packages` (never a runtime `pi install`).
    Empty `mcpAdapterPath` → the driver falls back to the image's vendored default (SP2 pins it).

    `model` is Pi's typed capability descriptor (reasoning/input/contextWindow/maxTokens).
    `thinkingLevel` selects the `--thinking` level passed to `pi` at launch — it requires
    `model.reasoning: true` (hard-fail otherwise) and is never forced/defaulted by the
    harness; the operator states both explicitly.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    binary_path: str = Field(default="pi", alias="binaryPath")
    mcp_adapter_path: str = Field(default="", alias="mcpAdapterPath")
    model: PiModelCapabilities = Field(default_factory=PiModelCapabilities)
    thinking_level: Literal["off", "minimal", "low", "medium", "high", "xhigh", "max"] | None = (
        Field(default=None, alias="thinkingLevel")
    )

    @model_validator(mode="after")
    def _thinking_level_requires_reasoning(self) -> "PiEngineBlock":
        if self.thinking_level is not None and not self.model.reasoning:
            raise ValueError("engine.pi.thinkingLevel requires engine.pi.model.reasoning=true")
        return self
```

Add `StrictBool` and `StrictInt` to the existing `pydantic` import.
`Annotated`, `field_validator`, `model_validator`, and `Literal` are already imported.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/config/test_schema.py -v`
Expected: all tests in the file PASS, including every pre-existing test (nothing else in
`PiEngineBlock`'s alias/default shape changed).

- [ ] **Step 5: Add the reasoning fixture**

Create `tests/config/fixtures/config_pi_reasoning.json` (same overall shape as
`tests/config/fixtures/config_cron.json`, with `engine.type: pi` + capability/thinking
set):

```json
{
  "schemaVersion": "1",
  "agent": {
    "name": "pi-reasoning-test-agent"
  },
  "model": {
    "name": "openai.gpt-5",
    "type": "openai",
    "params": {
      "temperature": 1
    }
  },
  "engine": {
    "workDir": "/workspace",
    "startupTimeoutSeconds": 30,
    "type": "pi",
    "pi": {
      "binaryPath": "pi",
      "mcpAdapterPath": "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
      "model": {
        "reasoning": true,
        "input": ["text"],
        "contextWindow": 200000,
        "maxTokens": 32000
      },
      "thinkingLevel": "high"
    }
  },
  "capability": {
    "type": "ach",
    "ach": {
      "baseUrl": "https://ach.example.com",
      "environment": "production"
    },
    "filter": {
      "exclude": {
        "tools": []
      }
    }
  },
  "limits": {
    "maxConcurrentInvocations": 1,
    "maxInvocationSeconds": 1800,
    "maxQueuedTotal": 100,
    "idempotencyWindowSeconds": 3600,
    "maxSteps": 50,
    "terminalOutputRetries": 1
  },
  "persistence": {
    "enabled": false,
    "mountPath": "/var/lib/ach-agent"
  },
  "health": {
    "host": "0.0.0.0",
    "port": 8000
  },
  "channels": [
    {
      "name": "heartbeat",
      "type": "cron",
      "concurrency": 1,
      "cron": {
        "schedule": "* * * * *",
        "timezone": "Europe/Madrid"
      }
    }
  ]
}
```

`tests/config/test_schema_artifact.py::test_rendered_fixtures_validate_against_schema`
already globs `tests/config/fixtures/config_*.json` (see `_FIXTURES` at that file's
line 20), so this fixture is picked up automatically without another parametrization
edit; the exact-input-shape artifact test was added in Step 1.

- [ ] **Step 6: Regenerate the JSON Schema artifact and verify the drift guard**

Run: `uv run python scripts/gen_schema.py --check`
Expected: FAILS with `STALE: docs/schemas/agent-config-v1.schema.json is out of sync
with AgentConfig` (the artifact hasn't been regenerated yet).

Run: `uv run python scripts/gen_schema.py`
Expected: `wrote docs/schemas/agent-config-v1.schema.json (<N> bytes)` — this rewrites the
artifact in place; do not hand-edit it.

Run: `uv run pytest tests/config/test_schema_artifact.py -v`
Expected: all tests PASS — `test_artifact_matches_generated` (fresh render matches
committed file), `test_artifact_is_valid_json_schema` (still well-formed Draft 2020-12),
`test_rendered_fixtures_validate_against_schema` parametrized over every fixture
including the new `config_pi_reasoning.json`, and
`test_pi_input_schema_exposes_only_supported_ordered_shapes` proving the artifact
accepts exactly the same two input shapes as Pydantic.

Spot-check the operator-visible result:

Run: `uv run python -c "import json; s=json.load(open('docs/schemas/agent-config-v1.schema.json')); print(json.dumps(s['\$defs']['PiEngineBlock'], indent=2))"`
Expected: the `PiEngineBlock` definition now shows `model` (a `$ref` to
`PiModelCapabilities`, itself showing `reasoning: boolean`,
`input: array of "text"/"image" enum` with `oneOf` branches for exactly `["text"]` and
`["text", "image"]`, `contextWindow`/`maxTokens: integer, exclusiveMinimum 0`) and `thinkingLevel` (a
7-value string enum, nullable) — this is
exactly what a Kubernetes operator author reading the schema now sees; nothing Pi-only
is hidden inside an opaque `object`-typed `params`.

- [ ] **Step 7: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/config/schema.py tests/config/ && uv run ruff format --check src/ach_agent/config/schema.py tests/config/ && uv run mypy --strict src/ach_agent/config/schema.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/config/schema.py docs/schemas/agent-config-v1.schema.json tests/config/test_schema.py tests/config/test_schema_artifact.py tests/config/fixtures/config_pi_reasoning.json
git commit -m "feat(config): typed engine.pi.model capability + engine.pi.thinkingLevel"
```

---

## Task 2: `EngineConfig` typed Pi fields + `models_json.py` + `driver.py` `--thinking` wiring

**Files:**
- Modify: `src/ach_agent/engine/base/driver.py:20-83` (`EngineConfig`)
- Modify: `src/ach_agent/engine/pi/models_json.py`
- Modify: `src/ach_agent/engine/pi/driver.py:17-20` (import), `:93-95` (launch argv)
- Test: `tests/engine/pi/test_models_json.py`, `tests/engine/pi/test_driver.py`

**Interfaces:**
- Consumes: nothing from `AgentConfig` directly (Task 3 does the `cfg` → `EngineConfig`
  wiring) — this task only adds fields to the harness-internal `EngineConfig` dataclass
  and makes `models_json.py`/`driver.py` read them.
- Produces: `PiModelCapability` (new dataclass in `engine/base/driver.py`: `reasoning:
  bool = False`, `input: list[str] = ["text"]`, `context_window: int = 128000`,
  `max_tokens: int = 16384`), `EngineConfig.pi_model_capability: PiModelCapability`,
  `EngineConfig.pi_thinking_level: str | None = None`. `build_models_json(cfg)` reads
  `cfg.pi_model_capability.*`; `PiDriver.launch()` reads `cfg.pi_thinking_level` directly
  (no `resolve_thinking_level()` helper needed — the value is already validated Pydantic
  input by the time it reaches `EngineConfig`, per Task 1's `PiEngineBlock` validators).
  Task 3 consumes `PiModelCapability` by this exact name/shape.

- [ ] **Step 1: Write the failing tests**

In `tests/engine/pi/test_models_json.py`, extend the existing top-level import:

```python
from ach_agent.engine.base.driver import EngineConfig, PiModelCapability
```

Then append:

```python

def test_default_capability_matches_pi_builtin_defaults() -> None:
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


def test_capability_overrides_from_engine_config() -> None:
    cfg = EngineConfig(
        model_type="openai",
        model_base_url="http://x/v1",
        pi_model_capability=PiModelCapability(
            reasoning=True, input=["text", "image"], context_window=200000, max_tokens=32000
        ),
    )
    doc, provider = build_models_json(cfg)
    model = doc["providers"][provider]["models"][0]
    assert model["reasoning"] is True
    assert model["input"] == ["text", "image"]
    assert model["contextWindow"] == 200000
    assert model["maxTokens"] == 32000
```

Append to `tests/engine/pi/test_driver.py`:

```python
async def test_launch_adds_thinking_flag_when_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _LaunchProcess:
        captured["args"] = args
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pi_module, "PiRpcClient", lambda _proc: object())
    cfg = EngineConfig(
        binary_path="pi",
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
        pi_thinking_level="high",
    )
    await PiDriver().launch(cfg, "argv")
    args = list(captured["args"])
    assert args[args.index("--thinking") + 1] == "high"


async def test_launch_omits_thinking_flag_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _LaunchProcess:
        captured["args"] = args
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pi_module, "PiRpcClient", lambda _proc: object())
    cfg = EngineConfig(
        binary_path="pi", home=str(tmp_path / "home"), work_dir=str(tmp_path / "work")
    )
    await PiDriver().launch(cfg, "argv")
    assert "--thinking" not in list(captured["args"])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/engine/pi/test_models_json.py tests/engine/pi/test_driver.py -v`
Expected: collection of `test_models_json.py` FAILS because `PiModelCapability` does not
exist yet (`ImportError`); the new driver tests FAIL because `EngineConfig` has no
`pi_thinking_level` kwarg and `--thinking` is not wired. Once Step 3 introduces the
dataclass/kwargs, `test_default_capability_matches_pi_builtin_defaults` locks the
already-existing hardcoded default while the override/argv tests remain red until Steps
4-5 consume the new fields.

- [ ] **Step 3: Add the typed fields to `EngineConfig`**

Edit `src/ach_agent/engine/base/driver.py`. Add, directly above the `@dataclass` /
`class EngineConfig:` declaration (currently line 20):

```python
@dataclass
class PiModelCapability:
    """Pi-only model capability descriptor (CONTRACT engine.pi.model — schema.py's
    PiModelCapabilities, mirrored here as the harness-internal runtime shape)."""

    reasoning: bool = False
    input: list[str] = field(default_factory=lambda: ["text"])
    context_window: int = 128000
    max_tokens: int = 16384


```

Then, inside `EngineConfig`, replace lines 77-78 (currently):

```python
    # Optional path to the vendored pi-mcp-adapter package. Empty selects the image default.
    pi_mcp_adapter_path: str = ""
```

with:

```python
    # Optional path to the vendored pi-mcp-adapter package. Empty selects the image default.
    pi_mcp_adapter_path: str = ""
    # CONTRACT engine.pi.model — Pi-only capability descriptor for models.json. Default
    # matches Pi's own builtin defaults, so an absent engine.pi.model behaves exactly
    # like today's hardcoded output.
    pi_model_capability: PiModelCapability = field(default_factory=PiModelCapability)
    # CONTRACT engine.pi.thinkingLevel — the --thinking level passed to pi at launch.
    # None → no --thinking flag (Pi's own default behavior). Already validated at config
    # load time (schema.py's PiEngineBlock requires reasoning=true when this is set).
    pi_thinking_level: str | None = None
```

- [ ] **Step 4: Read the typed capability in `models_json.py`**

Replace `src/ach_agent/engine/pi/models_json.py` in full with:

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
    cap = cfg.pi_model_capability
    model = {
        "id": cfg.model,
        "name": cfg.model,
        "reasoning": cap.reasoning,
        "input": list(cap.input),
        "contextWindow": cap.context_window,
        "maxTokens": cap.max_tokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
    }
    doc: dict[str, Any] = {
        "providers": {
            provider: {
                "api": api,
                "baseUrl": cfg.model_base_url,
                "apiKey": "local-proxy",
                "headers": {},
                "models": [model],
            }
        }
    }
    return doc, provider
```

- [ ] **Step 5: Wire `--thinking` into `driver.py`'s launch argv**

Edit `src/ach_agent/engine/pi/driver.py`. In `launch()`, immediately after the existing
`exclude_tools` block (currently lines 93-94) and before `proc = await
asyncio.create_subprocess_exec(`:

```python
        if cfg.exclude_tools:
            args.extend(["--exclude-tools", ",".join(cfg.exclude_tools)])
        if cfg.pi_thinking_level is not None:
            args.extend(["--thinking", cfg.pi_thinking_level])
        proc = await asyncio.create_subprocess_exec(
```

No new import needed in `driver.py` for this — `cfg.pi_thinking_level` is a plain
`EngineConfig` attribute.

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/engine/pi/test_models_json.py tests/engine/pi/test_driver.py -v`
Expected: all PASS, including every pre-existing test in both files (in particular
`test_launch_persona_and_exclude_tools_argv`'s four parametrized cases, which leave
`pi_thinking_level` at its `None` default and must see no `--thinking` flag inserted;
and `test_provider_api_mapping_and_no_ek`/`test_openai_and_anthropic_api_kinds`, which
leave `pi_model_capability` at its default and must see the unchanged default dict).

- [ ] **Step 7: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/ tests/engine/pi/ && uv run ruff format --check src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/ tests/engine/pi/ && uv run mypy --strict src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/engine/base/driver.py src/ach_agent/engine/pi/models_json.py src/ach_agent/engine/pi/driver.py tests/engine/pi/test_models_json.py tests/engine/pi/test_driver.py
git commit -m "feat(pi): read model capability + thinking level from typed EngineConfig fields"
```

---

## Task 3: `main.py` wiring — `cfg.engine.pi` → `EngineConfig`'s typed Pi fields

**Files:**
- Modify: `src/ach_agent/main.py:1395-1429` (`engine_cfg` construction)
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `PiEngineBlock`/`PiModelCapabilities` (Task 1), `PiModelCapability`/
  `EngineConfig` (Task 2).
- Produces: `_pi_engine_fields(cfg: Any) -> dict[str, Any]` — a small, independently
  testable pure function isolating the `cfg.engine.pi.*` → `EngineConfig` kwargs
  mapping, mirroring the existing (now-replaced) inline `binary_path`/
  `pi_mcp_adapter_path` ternaries at `main.py:1420-1429`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main_wiring.py`:

```python
def test_pi_engine_fields_defaults_for_opencode() -> None:
    from types import SimpleNamespace

    from ach_agent.main import _pi_engine_fields

    cfg = SimpleNamespace(engine=SimpleNamespace(type="opencode", pi=None))
    fields = _pi_engine_fields(cfg)
    assert fields["binary_path"] == "opencode"
    assert fields["pi_mcp_adapter_path"] == ""
    assert fields["pi_model_capability"].reasoning is False
    assert fields["pi_thinking_level"] is None


def test_pi_engine_fields_from_pi_config() -> None:
    from types import SimpleNamespace

    from ach_agent.config.schema import PiEngineBlock, PiModelCapabilities
    from ach_agent.main import _pi_engine_fields

    pi_block = PiEngineBlock(
        binaryPath="pi",
        mcpAdapterPath="/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
        model=PiModelCapabilities(
            reasoning=True, input=["text", "image"], contextWindow=200000, maxTokens=32000
        ),
        thinkingLevel="high",
    )
    cfg = SimpleNamespace(engine=SimpleNamespace(type="pi", pi=pi_block))
    fields = _pi_engine_fields(cfg)
    assert fields["binary_path"] == "pi"
    assert fields["pi_mcp_adapter_path"] == "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter"
    cap = fields["pi_model_capability"]
    assert cap.reasoning is True
    assert cap.input == ["text", "image"]
    assert cap.context_window == 200000
    assert cap.max_tokens == 32000
    assert fields["pi_thinking_level"] == "high"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_main_wiring.py -v -k "pi_engine_fields"`
Expected: both FAIL with `ImportError: cannot import name '_pi_engine_fields'`.

- [ ] **Step 3: Implement `_pi_engine_fields` and use it**

Edit `src/ach_agent/main.py`. Add this function directly above `async def main(`
(currently line 1159):

```python
def _pi_engine_fields(cfg: Any) -> dict[str, Any]:
    """engine.pi.* -> the EngineConfig kwargs Pi's driver consumes.

    Opencode-default shape when engine.type != "pi" or engine.pi is absent — opencode
    configs never carry these.
    """
    from ach_agent.engine.base.driver import PiModelCapability

    pi = cfg.engine.pi if cfg.engine.type == "pi" else None
    if pi is None:
        return {
            "binary_path": "opencode",
            "pi_mcp_adapter_path": "",
            "pi_model_capability": PiModelCapability(),
            "pi_thinking_level": None,
        }
    return {
        "binary_path": pi.binary_path,
        "pi_mcp_adapter_path": pi.mcp_adapter_path,
        "pi_model_capability": PiModelCapability(
            reasoning=pi.model.reasoning,
            input=list(pi.model.input),
            context_window=pi.model.context_window,
            max_tokens=pi.model.max_tokens,
        ),
        "pi_thinking_level": pi.thinking_level,
    }
```

Then replace lines 1419-1429 (currently):

```python
        engine_type=cfg.engine.type,
        binary_path=(
            cfg.engine.pi.binary_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else "opencode"
        ),
        pi_mcp_adapter_path=(
            cfg.engine.pi.mcp_adapter_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else ""
        ),
```

with:

```python
        engine_type=cfg.engine.type,
        **_pi_engine_fields(cfg),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_main_wiring.py -v`
Expected: all PASS, including every pre-existing test in the file (the `engine_cfg`
construction's other kwargs are untouched; only the `binary_path`/`pi_mcp_adapter_path`
ternaries were replaced by the equivalent dict).

- [ ] **Step 5: Full non-e2e regression + lint + typecheck**

Run: `uv run pytest tests/ -q --ignore=tests/e2e`
Expected: all PASS (no regression elsewhere in `main.py`'s boot path).

Run: `uv run ruff check src/ach_agent/main.py tests/test_main_wiring.py && uv run ruff format --check src/ach_agent/main.py tests/test_main_wiring.py && uv run mypy --strict src/ach_agent/main.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_wiring.py
git commit -m "fix(main): wire engine.pi.model/thinkingLevel into EngineConfig"
```

---

## Task 4: Real-subprocess verification — hardened, no masquerading skips

**Files:**
- Modify: `tests/e2e/test_pi_e2e.py`

**Interfaces:**
- Consumes: `PiDriver.launch(cfg, session_key) -> ManagedServer` (unchanged signature),
  `ManagedServer.ephemeral_home: Path`, `ManagedServer._client`.
- Produces: nothing for later tasks — this is the acceptance test for the whole feature.

- [ ] **Step 1: Require both real-subprocess dependencies; skip only for local developers**

Edit `tests/e2e/test_pi_e2e.py`. Add `NoReturn` to the existing `typing` import, then
replace the current top-of-file `PI` lookup/skip:

```python
from typing import Any, NoReturn
```

```python
def _missing_pi_dependency(name: str) -> NoReturn:
    if os.environ.get("CI"):
        # GitHub Actions sets CI=true on every runner (including e2e-pi). Missing either
        # real-subprocess dependency is an install regression, never a valid CI skip.
        raise RuntimeError(
            f"{name} not installed — CI must run tests/e2e/test_pi_e2e.py, not skip it"
        )
    pytest.skip(f"{name} not installed", allow_module_level=True)


def _require_pi_binary() -> str:
    path = shutil.which("pi")
    if path is None:
        _missing_pi_dependency("pi binary")
    return path


def _require_pi_mcp_adapter() -> str:
    candidates = [
        os.environ.get("PI_MCP_ADAPTER_PATH", ""),
        "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
        str(Path.home() / ".pi/agent/npm/node_modules/pi-mcp-adapter"),
    ]
    path = next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_dir()),
        None,
    )
    if path is None:
        _missing_pi_dependency("pi-mcp-adapter")
    return path


PI = _require_pi_binary()
PI_MCP_ADAPTER_PATH = _require_pi_mcp_adapter()
```

This deliberately resolves **both** dependencies during module collection. On a local
developer machine, absence of either dependency skips the real-subprocess module. Under
`CI=true`, absence of either raises and makes collection fail. In particular, do not
leave adapter checks as per-test `pytest.skip(...)` calls: that would still let CI go
green without executing the acceptance test.

- [ ] **Step 2: Pin the exact Pi version for every test in this module**

Add, directly below the dependency guards above:

```python
@pytest.fixture(autouse=True)
def _pinned_pi_version() -> None:
    """Every test in this module runs the pinned 0.81.1 — a version drift silently
    changing RPC/CLI behavior must fail loudly here, not pass on a different Pi."""
    import subprocess

    result = subprocess.run(
        [PI, "--version"], capture_output=True, text=True, check=True
    )
    version = result.stdout.strip()
    assert version == "0.81.1", (
        f"pi --version = {version!r}, expected the pinned 0.81.1"
    )
```

- [ ] **Step 3: Add an id+command-validating RPC helper**

Add this module-level helper (place it above `async def _chat_completions`):

```python
async def _rpc_roundtrip(client: Any, command: str, **payload: Any) -> dict[str, Any]:
    """Send a request and return its data, asserting id AND command match (not just id) —
    a matching id with a mismatched command would mean the client desynced from the
    protocol, and a silent pass there would hide that."""
    request_id = f"e2e-{command}"
    await client.send({**payload, "type": command, "id": request_id})
    while True:
        event = await client.recv()
        if event.get("type") != "response" or event.get("id") != request_id:
            continue
        assert event.get("command") == command, (
            f"response command mismatch: expected {command!r}, got {event.get('command')!r}"
        )
        assert event.get("success") is True, f"{command} failed: {event.get('error')}"
        return event.get("data") or {}
```

Then replace the inline adapter lookup and skip currently inside
`test_pi_turn_and_ek_never_on_disk_or_in_subprocess` with:

```python
    adapter_path = PI_MCP_ADAPTER_PATH
```

(The local absence behavior moved to the module-level dependency guard; CI absence now
hard-fails instead of skipping.)

- [ ] **Step 4: Write the hardened reasoning/thinking-level test**

Append to `tests/e2e/test_pi_e2e.py`:

```python
async def test_pi_reasoning_model_reports_resolved_thinking_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_e2e_secret_marker")
    monkeypatch.setenv("ACH_API_KEY", "ek_api_secret_marker")
    adapter_path = PI_MCP_ADAPTER_PATH
    runner, base_url = await _start_stub_server()
    server: Any = None
    try:
        from ach_agent.engine.base.driver import PiModelCapability

        cfg = EngineConfig(
            engine_type="pi",
            binary_path=PI,
            home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "workspace"),
            model="stub-reasoning-model",
            model_type="openai",
            model_base_url=f"{base_url}/v1",
            pi_mcp_adapter_path=adapter_path,
            pi_model_capability=PiModelCapability(reasoning=True),
            pi_thinking_level="high",
        )
        driver = PiDriver()
        server = await driver.launch(cfg, "e2e-reasoning-key")

        # Known-good config, restated: provider/model chosen explicitly (--provider/
        # --model, asserted implicitly by a successful launch below), dummy local-proxy
        # key + localhost-only baseUrl (never the ek_ or a real ACH endpoint).
        models_doc = json.loads(
            (server.ephemeral_home / "models.json").read_text(encoding="utf-8")
        )
        provider_doc = next(iter(models_doc["providers"].values()))
        assert provider_doc["apiKey"] == "local-proxy"
        assert provider_doc["baseUrl"].startswith("http://127.0.0.1:")
        assert provider_doc["models"][0]["reasoning"] is True

        client: Any = server._client
        await _rpc_roundtrip(client, "new_session")
        state = await _rpc_roundtrip(client, "get_state")

        assert state.get("thinkingLevel") == "high"
        model_info = state.get("model") or {}
        assert model_info.get("reasoning") is True

        for name in ("models.json", "settings.json", "mcp.json"):
            blob = (server.ephemeral_home / name).read_text(encoding="utf-8")
            assert "ek_e2e_secret_marker" not in blob, f"ek leaked into {name}"
            assert "ek_api_secret_marker" not in blob, f"ek leaked into {name}"

        process = server._process
        assert process is not None
        environ = Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
        assert all(b"ACH_TOKEN" not in item for item in environ)
        assert all(b"ek_e2e_secret_marker" not in item for item in environ)
    finally:
        if server is not None:
            await PiDriver().stop(server)
        await runner.cleanup()
```

- [ ] **Step 5: Run against the real `pi` binary and confirm it passes**

Run: `uv run pytest tests/e2e/test_pi_e2e.py -v`
Expected: both `test_pi_turn_and_ek_never_on_disk_or_in_subprocess` (same pi-present
behavior after centralizing dependency resolution) and
`test_pi_reasoning_model_reports_resolved_thinking_level` PASS. This is real-subprocess
proof, not a mock: if Pi 0.81.1 needs `new_session` before `get_state` reports a model
(already handled above), or rejects `--thinking high` for a `reasoning: true` model on
bare `openai-completions` without a `compat`/`thinkingLevelMap` hint this plan doesn't
set, this test fails here — the signal to revisit Task 2's argv wiring or Task 1's
`PiModelCapabilities` shape, not to weaken this assertion.

- [ ] **Step 6: Confirm the CI hardening doesn't break the normal (pi-present) path**

Run: `CI=true uv run pytest tests/e2e/test_pi_e2e.py -v`
Expected: identical PASS result to Step 5 — the `CI` env var only changes behavior when
`pi` or `pi-mcp-adapter` is *absent*; since both are present in this environment, both
tests run exactly as in Step 5. Code review of `_missing_pi_dependency` is the guard for
both absence paths: each `_require_*` function calls it, and under `CI=true` it raises
instead of calling `pytest.skip`.

- [ ] **Step 7: Lint**

Run: `uv run ruff check tests/e2e/test_pi_e2e.py && uv run ruff format --check tests/e2e/test_pi_e2e.py`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add tests/e2e/test_pi_e2e.py
git commit -m "test(pi): real-subprocess verification of resolved reasoning/thinkingLevel; CI can't silently skip"
```

---

## Task 5: `CONTRACT_v3.md` + decision record + reference index

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md:124-127`
- Create: `docs/references/2026-07-23-pi-model-capability-and-thinking.md`
- Modify: `docs/references/README.md`

**Interfaces:** None (documentation only).

- [ ] **Step 1: Update `CONTRACT_v3.md`'s `engine.pi` example**

Edit `docs/plan/CONTRACT_v3.md`. Replace lines 124-127 (currently):

```
    "pi": null                                // PiEngineBlock; consulted only when type == "pi":
    //                                           { "binaryPath": "pi",           // pi on PATH in the image
    //                                             "mcpAdapterPath": "" }        // "" → image default
    //                                           /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter
```

with:

```
    "pi": {                                   // PiEngineBlock; consulted only when type == "pi"
      "binaryPath": "pi",                     // pi on PATH in the image
      "mcpAdapterPath": "",                    // "" → image default:
      //                                          /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter
      "model": {                              // Pi-only model capability descriptor (models.json
        "reasoning": false,                    // fields). NOT sent to the model API call — that
        "input": ["text"],                     // stays model.params above (open, unvalidated,
        "contextWindow": 128000,               // per-call passthrough). Absent fields → these
        "maxTokens": 16384                      // same values (Pi's own builtin defaults).
      },
      "thinkingLevel": null                    // off|minimal|low|medium|high|xhigh|max; requires
      //                                           model.reasoning=true (hard-fail otherwise);
      //                                           passed to `pi` as --thinking at launch — never
      //                                           via settings.json defaults, never forced by the
      //                                           harness. Generated into
      //                                           docs/schemas/agent-config-v1.schema.json.
    }
```

- [ ] **Step 2: Write the decision record**

Create `docs/references/2026-07-23-pi-model-capability-and-thinking.md`:

```markdown
# Pi model capability + thinking level: a typed `engine.pi` surface, not `model.params`

**Date:** 2026-07-23 · **Status:** Shipped

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
```

- [ ] **Step 3: Add the index row**

Edit `docs/references/README.md`, append a row to the decision-records table (matching
the existing `| Date | Doc | Status | What it decides |` format used by every other row):

```markdown
| 2026-07-23 | [pi-model-capability-and-thinking](2026-07-23-pi-model-capability-and-thinking.md) | Shipped | Pi's `reasoning`/`input`/`contextWindow`/`maxTokens`/`--thinking` get a typed, generated `engine.pi.model`/`engine.pi.thinkingLevel` surface (not `model.params`, which stays opaque per-call passthrough); `model.type` wire selection untouched. |
```

- [ ] **Step 4: Commit**

```bash
git add docs/plan/CONTRACT_v3.md docs/references/2026-07-23-pi-model-capability-and-thinking.md docs/references/README.md
git commit -m "docs(pi): CONTRACT_v3 + decision record for typed engine.pi.model/thinkingLevel"
```

---

## Task 6: `../ach` handoff — render `engine.pi.model` / `engine.pi.thinkingLevel`

**Files:**
- Create: `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md`

**Interfaces:** None — this file is a self-contained prompt for the `../ach` operator
repo, mirroring `docs/superpowers/plans/2026-07-23-pi-engine-driver-sp2/handoff-ach-render.md`'s
existing structure and precondition style for the earlier `engine.type`/
`engine.pi.{binaryPath,mcpAdapterPath}` handoff.

- [ ] **Step 1: Write the handoff prompt**

Create `docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md`:

```markdown
# Handoff prompt — `../ach`: render `engine.pi.model` / `engine.pi.thinkingLevel`

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
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/plans/2026-07-23-pi-model-runtime-parity-handoff-ach.md
git commit -m "docs(pi): ../ach handoff for engine.pi.model/thinkingLevel rendering"
```

---

## Task 7: Version bump — v0.8.1

This is a corrective fix to the engine `../ach` is about to advertise
(`docs/superpowers/plans/2026-07-23-pi-engine-driver-sp2/index.md`'s ship-order rule:
an ach-agent image must ship before `../ach` advertises `engine.type: pi` /
`engine.pi.model`). Cut v0.8.1 so the released image carries this fix before Task 6's
handoff proceeds in `../ach`.

**Files:**
- Modify: `CHANGELOG.md`, `pyproject.toml`, `uv.lock`

- [ ] **Step 1: Update `CHANGELOG.md`**

Compute today's release date:

```bash
RELEASE_DATE=$(date +%F)
echo "Release date resolved to: $RELEASE_DATE"
```

Edit `CHANGELOG.md`, inserting a new section between `## [unreleased]` and `## [0.8.0]`
(replace `<RELEASE_DATE>` with the value printed above):

```markdown
## [unreleased]

## [0.8.1] - <RELEASE_DATE>

### Fixed
- **Pi reasoning/thinking parity.** `engine.pi` no longer hardcodes every model as
  non-reasoning, text-only, with a 128K-context/16K-output ceiling. A typed
  `engine.pi.model` capability block (`reasoning`, `input`, `contextWindow`,
  `maxTokens`) and `engine.pi.thinkingLevel` join `engine.pi.binaryPath`/
  `mcpAdapterPath`, generated into `docs/schemas/agent-config-v1.schema.json` so the
  Kubernetes operator author can see exactly what Pi accepts. `model.params` is
  unchanged (still opaque per-call passthrough; Pi never read it and still doesn't).
  Defaults are unchanged when `engine.pi.model`/`thinkingLevel` are absent, so existing
  `engine.type: pi` configurations are unaffected.

## [0.8.0] - 2026-07-23
```

- [ ] **Step 2: Bump `pyproject.toml` and relock**

Edit `pyproject.toml`: change `version = "0.8.0"` to `version = "0.8.1"`.

Run: `uv lock`
Expected: `uv.lock`'s `ach-agent` self-entry version updates to `0.8.1`; exit code 0.

- [ ] **Step 3: Commit the version bump**

```bash
git add CHANGELOG.md pyproject.toml uv.lock
git commit -m "chore(release): bump version to 0.8.1"
```

- [ ] **Step 4: Empty marker commit — STOP for explicit approval before this step**

Per the repo's release ritual (`CLAUDE.md` "Release ritual"), CI only cuts a release when
the **head commit** message starts with `chore(release): v<x.y.z>` on a push to `main`.
This step creates that marker commit and is followed by `git push origin main`, which
triggers a real CI release build + GHCR image push + GitHub release. **Do not run this
step or push without the user's explicit go-ahead at execution time** — confirm the
target branch and that Tasks 1-6 are merged/landed on `main` first.

```bash
git commit --allow-empty -m "chore(release): v0.8.1"
```

Only after explicit confirmation:

```bash
git push origin main
```

---

## Amendment Summary

- `PiModelCapabilities` uses `StrictBool`/`StrictInt`; red tests cover rejected string,
  numeric, float, and bool coercions and prove the errors are field-local rather than
  today's whole-`model` `extra_forbidden` failure.
- `engine.pi.model.input` accepts only the two ordered shapes `["text"]` and
  `["text", "image"]`; tests separately reject empty, image-only, duplicate, reversed,
  overlong, and unknown-value lists.
- Real-Pi collection requires both `pi` and `pi-mcp-adapter`: local dependency absence
  may skip, while `CI=true` raises for either missing dependency.
- Every `pytest -k` selector in this plan is shell-quoted.

## Verification Gates (all must be green before Task 7)

```bash
make lint                                   # ruff check + format --check + mypy --strict, all of src/
make test                                    # pytest, all of tests/ except tests/e2e
make conformance                              # CONTRACT §6 — 11 named router invariants (untouched by this plan, must stay green)
uv run python scripts/gen_schema.py --check   # docs/schemas/agent-config-v1.schema.json matches AgentConfig
uv run pytest tests/config/test_schema_artifact.py -v   # drift guard + every fixture (incl. config_pi_reasoning.json) validates
uv run pytest tests/e2e/test_pi_e2e.py -v     # real pi 0.81.1 subprocess — both tests, version-pinned
```

## Self-Review

- **Spec coverage:** (1) authoritative surface re-decided — typed `engine.pi.model`/
  `thinkingLevel` in Pydantic (Task 1), regenerated schema + drift test (Task 1 Step 6),
  CONTRACT_v3.md (Task 5), exact `../ach` handoff (Task 6); the stale "no CONTRACT/schema
  change needed" claim is explicitly corrected in Global Constraints and the decision
  record. (2) known-good Pi config matched — explicit `--provider`/`--model` (unchanged,
  documented), dummy `local-proxy` key + localhost-only `baseUrl` (asserted in Task 4's
  e2e test), per-model `reasoning`/`input` (Task 1/2), thinking level only when
  configured (Task 1's cross-field validator + Task 2's `None`-default), CLI-flags-vs-
  settings-defaults explained (Architecture + decision record). (3) strict recognized-
  value handling — `reasoning` is `StrictBool`; `contextWindow`/`maxTokens` are
  positive `StrictInt`s; `input` is validated to exactly `["text"]` or
  `["text", "image"]`. Parametrized tests reject scalar coercions and every unsupported
  input shape through `load_config`, while direct Pydantic tests require field-local
  errors and reject `extra_forbidden`, so the pre-implementation unknown-`model` failure
  cannot masquerade as a useful red test. Absent fields preserve Pi defaults (Task 1's
  first positive test + Task 2's default-capability test). (4) real-subprocess test hardened —
  exact `pi --version` pin (autouse fixture), id+command validated on every RPC response
  (`_rpc_roundtrip`), `get_state`'s `model.reasoning`/`thinkingLevel` asserted, secret +
  localhost invariants asserted, and module collection resolves both `pi` and
  `pi-mcp-adapter`, locally skipping but hard-failing under `CI=true` when either is
  absent (Task 4). (5)
  `model.type` wire mapping untouched (stated constraint, no task touches
  `_PI_PROVIDER_BY_TYPE`), no model-ID branches (every task reads config/EngineConfig
  fields, never `cfg.model`), release marker/push gated on explicit approval (Task 7
  Step 4).
- **Placeholder scan:** no TBD/"add error handling"/"similar to Task N" — every step has
  runnable code, exact commands, and the CHANGELOG date is computed by a shown shell
  command rather than left as a bare placeholder.
- **Shell consistency:** every `pytest -k` expression is quoted, so boolean selectors
  reach pytest intact instead of being split/interpreted by the shell.
- **Type consistency:** `PiModelCapabilities` (Pydantic, Task 1) and `PiModelCapability`
  (dataclass, Task 2) are deliberately two distinct types at two layers (config schema vs.
  harness-internal runtime config) with the same field *names* after alias resolution
  (`reasoning`/`input`/`context_window`/`max_tokens`) — Task 3's `_pi_engine_fields`
  is the one place that maps one to the other, and every task after Task 2 references
  `EngineConfig.pi_model_capability`/`.pi_thinking_level` by exactly those names.
