# Phase 5 — Config seam: `engine.type` + schema + runtime spec

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first.

**Goal:** Add `EngineBlock.type: Literal["opencode","pi"] = "opencode"` plus an optional Pi sub-block, regenerate the frozen JSON Schema, and amend the runtime spec §7.4 to make `pi` (not `pymono`) the canonical wire name. This unblocks driver selection in Phase 6.

**Exit criterion:** `tests/config/test_schema_artifact.py` (drift guard) green with the regenerated artifact; a config with `engine.type: pi` loads; `type: bogus` is rejected.

**Files:**
- Modify: `src/ach_agent/config/schema.py:61-88` (`EngineBlock`) + add `PiEngineBlock`
- Regenerate: `docs/schemas/agent-config-v1.schema.json` (via `make schema`)
- Modify: `docs/spec/ach-agent-runtime-spec-v1_4_2.md` §7.4 (`pymono` → `pi`)
- Create/Modify tests: `tests/config/test_engine_type.py`
- Best-effort (design record): `docs/plan/CONTRACT_v3.md` engine section (git-ignored — see note)

**Interfaces:**
- Produces (consumed by Phase 6): `cfg.engine.type` (`"opencode"|"pi"`), `cfg.engine.pi` (`PiEngineBlock | None`).
- Consumes: nothing from prior phases (config-only).

---

### Task 5.1: `EngineBlock.type` + `PiEngineBlock`

- [ ] **Step 1: Write the failing test**

Create `tests/config/test_engine_type.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import EngineBlock


def test_engine_type_defaults_to_opencode() -> None:
    assert EngineBlock().type == "opencode"


def test_engine_type_pi_accepted_with_subblock() -> None:
    eng = EngineBlock.model_validate({"type": "pi", "pi": {"binaryPath": "pi"}})
    assert eng.type == "pi"
    assert eng.pi is not None and eng.pi.binary_path == "pi"


def test_engine_type_rejects_unknown() -> None:
    with pytest.raises(ValidationError):
        EngineBlock.model_validate({"type": "pymono"})  # renamed to 'pi' — old name rejected


def test_pi_subblock_defaults() -> None:
    from ach_agent.config.schema import PiEngineBlock

    pi = PiEngineBlock()
    assert pi.binary_path == "pi"
    assert pi.mcp_adapter_path == ""
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/config/test_engine_type.py -q`
Expected: FAIL — `EngineBlock` has no `type`; `PiEngineBlock` does not exist.

- [ ] **Step 3: Add `PiEngineBlock` and extend `EngineBlock`**

In `src/ach_agent/config/schema.py`, add `PiEngineBlock` immediately **before** `class EngineBlock`:

```python
class PiEngineBlock(BaseModel):
    """Pi-engine sub-block (consulted only when engine.type == 'pi').

    `binaryPath` pins the `pi` executable; `mcpAdapterPath` is the vendored pi-mcp-adapter
    package path referenced from Pi's settings.json `packages` (never a runtime `pi install`).
    Empty `mcpAdapterPath` → the driver falls back to the image's vendored default (SP2 pins it).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    binary_path: str = Field(default="pi", alias="binaryPath")
    mcp_adapter_path: str = Field(default="", alias="mcpAdapterPath")
```

Then add two fields to `EngineBlock` (after `max_tool_calls`):

```python
    # SP1: which engine runs this agent. Canonical wire name is "pi" (runtime spec §7.4 amended
    # from the reserved "pymono"). Selects the EngineDriver in main._make_engine_runner.
    type: Literal["opencode", "pi"] = Field(default="opencode", alias="type")
    # Pi sub-block — only consulted when type == "pi"; optional so opencode configs never carry it.
    pi: PiEngineBlock | None = Field(default=None, alias="pi")
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/config/test_engine_type.py -q`
Expected: PASS.

- [ ] **Step 5: Regenerate the frozen JSON Schema**

Run: `make schema`  (wraps `uv run python scripts/gen_schema.py`)
This rewrites `docs/schemas/agent-config-v1.schema.json` from `AgentConfig`. Confirm the diff adds `type`/`pi` under the engine block and a `PiEngineBlock` `$def`.

Run the drift guard:

Run: `uv run pytest tests/config/test_schema_artifact.py -q`
Expected: PASS (artifact now matches `AgentConfig`).

- [ ] **Step 6: Amend the runtime spec §7.4**

In `docs/spec/ach-agent-runtime-spec-v1_4_2.md` §7.4, change the reserved `pymono` engine type to `pi` and note it is implemented in SP1. Concretely, in the "Future engine types may include" block, replace the line `pymono` with `pi`, and add above it a sentence:

```text
### 7.4 Engine types

v1 implementation targets:

```text
opencode
pi
```

Future engine types may include:

```text
claudeCode
codex
cryoAI
custom
awsAgentCore
bedrockAgent
```
```

(i.e. move `pi` up into the implemented set and drop `pymono` from the future list.)

- [ ] **Step 7: Best-effort — CONTRACT design record**

`docs/plan/CONTRACT_v3.md` is the internal design record and is **git-ignored** (per `scripts/gen_schema.py` header — the committed machine-readable contract is the JSON schema from Step 5). If the file exists locally, add `engine.type` (+ optional `engine.pi`) to its §2 engine block for consistency. Skip if absent; do **not** attempt to `git add` it.

- [ ] **Step 8: Type-check + commit**

Run: `uv run mypy --strict src/ach_agent/config/`
Expected: no errors.

```bash
git add src/ach_agent/config/schema.py tests/config/test_engine_type.py \
        docs/schemas/agent-config-v1.schema.json docs/spec/ach-agent-runtime-spec-v1_4_2.md
git commit -m "feat(config): add engine.type (opencode|pi) + PiEngineBlock; regen schema; spec §7.4 pi"
```

---

## Self-review (Phase 5)

- **Spec coverage:** §7 — `EngineBlock.type: Literal["opencode","pi"] = "opencode"`, optional Pi sub-fields (`binaryPath` + `mcpAdapterPath`), canonical wire name `pi` (runtime spec §7.4 amended, no alias), frozen schema regenerated via the existing generator, drift-guarded. All present.
- **Placeholders:** none — full model code, full tests, exact schema/spec commands and edits.
- **YAGNI:** the Pi sub-block carries only the two fields Phase 8 actually needs (`binaryPath`, `mcpAdapterPath`); no speculative knobs.
- **Type consistency:** `cfg.engine.type` / `cfg.engine.pi.binary_path` / `cfg.engine.pi.mcp_adapter_path` are exactly the names Phase 6 (driver selection + `EngineConfig.engine_type`/`binary_path`) and Phase 8 (settings.json `packages`) consume.
