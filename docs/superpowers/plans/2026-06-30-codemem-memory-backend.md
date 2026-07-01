# codemem Memory Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `codemem` as a second `memory.type` backend, wired into opencode as a **stdio `type:local` MCP server** that the model drives explicitly (no auto-capture, no prompt-injection), with the existing Hindsight path preserved.

**Architecture:** `config.memory` becomes a discriminated union on `memory.type` (`hindsight` | `codemem`). Hindsight keeps its current behaviour (external endpoint, probe + prompt-inject + remote MCP registration). codemem is a harness-managed *local* MCP: opencode spawns `codemem mcp --db-path <db>` as its own stdio child (1:1 with each opencode process), so the harness only writes the opencode.json entry — no port, no paired sidecar process, no inject. Memory is **model-managed**: the model calls codemem's MCP tools (`search`/`remember`/`timeline`) on demand, which keeps the system-prompt prefix stable (prompt-cache friendly).

**Tech Stack:** Python 3.12 + Pydantic v2 (discriminated union, `extra='forbid'`), opencode `serve --pure` (unchanged), codemem CLI (Node 24, `codemem mcp` stdio), pytest.

## Global Constraints

- `opencode serve --pure` stays — it is the trust boundary against the untrusted `/workspace` repo (blocks `/workspace/.opencode/plugins/*` RCE). Do NOT remove `--pure`. codemem is registered via opencode.json, not as a plugin. (verbatim invariant from design discussion)
- **ek-hygiene (SEC-01 / CONTRACT §6.10):** codemem is local; it must NEVER receive `ek_` (`ACH_TOKEN`/`ACH_API_KEY`). It inherits opencode's clean-slate env. Its MCP entry carries NO secret.
- **Fail-open (MEM-02 / D-02):** a missing/broken codemem must NEVER fail an invocation. On `codemem` absent from PATH → degrade (skip the MCP entry, increment `MEMORY_DEGRADED`), launch opencode without memory tools.
- **No auto-capture, no auto-inject in v1:** `inject=false`, `capture=false`. Memory is model-managed via MCP tools only. Rationale: a mutating injected `## Memory` block would churn the prompt-prefix cache.
- **db_path is static operator config (per-agent), NOT templated per-repo in v1.** Source is the operator-rendered config (trusted, like `bank_id`, never inbound payload). Per-repo multi-tenant separation is OUT of v1 scope (the pool writes opencode.json at `launch()`, fixed per server — per-event db_path would break pool reuse).
- **Viewer OFF:** the codemem MCP `environment` must set `CODEMEM_VIEWER=0` and `CODEMEM_VIEWER_AUTO=0` (headless; N sessions must not each spawn a web viewer).
- Always use the project venv (`uv`); never system-wide pip. Tests: `pytest`.

## ⚠ Cross-repo / Contract coordination (READ BEFORE STARTING)

`MemoryBlock` is **contract-reserved (CONTRACT_v3 §2)** — `ach-runtime` (the Go operator) renders it. Introducing `memory.type` + `memory.codemem.*` is a **coordinated CONTRACT_v3 change**: the operator must learn to render the new shape. This plan implements the **harness (Python) side only**, kept **backward-compatible** so legacy `memory:` blocks (no `type`) still load as `hindsight`. Do NOT delete the legacy Hindsight fields. The CONTRACT_v3.md + ach-runtime change is tracked separately and is a prerequisite for production rendering — but the harness is independently testable with a hand-written config (project invariant).

## File Structure

- `src/ach_agent/config/schema.py` — replace flat `MemoryBlock` with `HindsightMemory | CodememMemory` discriminated union + a `before` validator on the `memory` field for backward-compat default `type:"hindsight"`.
- `src/ach_agent/engine/lifecycle.py` — `EngineConfig` gains `codemem_db_path: str = ""`; `write_opencode_config` emits a `type:local` codemem MCP entry when it is set.
- `src/ach_agent/memory/adapter.py` — add `prepare_codemem(memory_cfg) -> tuple[bool, str]` (PATH probe, fail-open). Hindsight path unchanged.
- `src/ach_agent/main.py` — boot: set `engine_cfg.codemem_db_path` when `memory.type == "codemem"`; engine_runner: branch so the Hindsight probe/inject runs ONLY for `type == "hindsight"`.
- `Dockerfile` / `.dockerignore` — install codemem (Node 24) so `codemem` is on PATH in the runtime image.
- `tests/...` — unit tests per task.

---

### Task 1: `memory.type` discriminated union (schema, backward-compatible)

**Files:**
- Modify: `src/ach_agent/config/schema.py` (replace `MemoryBlock`, add union + validator)
- Modify: `src/ach_agent/config/schema.py` (the field on the top-level config: `memory: MemoryBlock | None` → `memory: Memory | None`)
- Test: `tests/config/test_memory_union.py`

**Interfaces:**
- Produces: `HindsightMemory(type:Literal["hindsight"], endpoint:str, mission:str, bank:str, mental_models:list[str])`; `CodememMemory(type:Literal["codemem"], db_path:str, mission:str)`; `Memory = Annotated[HindsightMemory | CodememMemory, Field(discriminator="type")]`. Both expose `.type`; Hindsight exposes `.endpoint/.bank/.mental_models`; codemem exposes `.db_path`.

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_memory_union.py
import pytest
from pydantic import ValidationError
from ach_agent.config.schema import HindsightMemory, CodememMemory
from ach_agent.config.schema import RuntimeConfig  # adjust to the actual top-level config class name


def _base_cfg(memory: dict) -> dict:
    # Minimal valid config dict; fill required sibling blocks from an existing fixture/helper.
    from tests.config.helpers import minimal_config_dict  # reuse existing helper
    d = minimal_config_dict()
    d["memory"] = memory
    return d


def test_legacy_block_without_type_loads_as_hindsight():
    cfg = RuntimeConfig.model_validate(_base_cfg(
        {"endpoint": "http://mem:8080", "bank": "gitlab-pr-review", "mentalModels": ["m1"]}
    ))
    assert isinstance(cfg.memory, HindsightMemory)
    assert cfg.memory.type == "hindsight"
    assert cfg.memory.endpoint == "http://mem:8080"
    assert cfg.memory.bank == "gitlab-pr-review"


def test_codemem_block_loads():
    cfg = RuntimeConfig.model_validate(_base_cfg(
        {"type": "codemem", "dbPath": "/var/lib/codemem/agent.db"}
    ))
    assert isinstance(cfg.memory, CodememMemory)
    assert cfg.memory.type == "codemem"
    assert cfg.memory.db_path == "/var/lib/codemem/agent.db"


def test_codemem_rejects_relative_db_path():
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(_base_cfg({"type": "codemem", "dbPath": "../escape.db"}))


def test_codemem_rejects_hindsight_fields():
    # extra='forbid' — bank is not a codemem field
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(_base_cfg(
            {"type": "codemem", "dbPath": "/var/lib/codemem/a.db", "bank": "x"}
        ))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_memory_union.py -v`
Expected: FAIL (`ImportError: cannot import name 'HindsightMemory'` / `CodememMemory`).

> Note: if `tests/config/helpers.py::minimal_config_dict` does not exist, create it from an existing config fixture used elsewhere in `tests/config/`. Inspect a passing config test first and copy its minimal dict.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ach_agent/config/schema.py
# Replace the existing `class MemoryBlock(BaseModel): ...` with the union below.
# Keep the field order / aliases identical to the old MemoryBlock for the hindsight side.

from pathlib import PurePosixPath
from typing import Annotated, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


class HindsightMemory(BaseModel):
    """CONTRACT §2 memory block — Hindsight backend (fail-open §31). Legacy/default shape."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["hindsight"] = "hindsight"
    endpoint: str
    # Contract-reserved (CONTRACT §2): accepted, not yet consumed.
    mission: str = ""
    # Static memory bank_id (operator config — never inbound payload, T-04-03).
    bank: str = ""
    mental_models: list[str] = Field(default_factory=list, alias="mentalModels")


class CodememMemory(BaseModel):
    """CONTRACT §2 memory block — codemem backend (local stdio MCP, model-managed)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["codemem"]
    # Absolute path to the codemem SQLite DB on a persistent volume. Operator config
    # (trusted, like bank_id). NOT templated per-repo in v1; NOT from inbound payload.
    db_path: str = Field(alias="dbPath")
    # Contract-reserved (CONTRACT §2): accepted, not yet consumed.
    mission: str = ""

    @field_validator("db_path")
    @classmethod
    def _abs_no_escape(cls, v: str) -> str:
        p = PurePosixPath(v)
        if not p.is_absolute() or ".." in p.parts:
            raise ValueError("memory.codemem.db_path must be an absolute path with no '..'")
        return v


# Discriminated on `type`. Backward-compat: a legacy block with no `type` is defaulted
# to "hindsight" by the validator on the top-level config field (see below).
Memory = Annotated[HindsightMemory | CodememMemory, Field(discriminator="type")]
```

```python
# src/ach_agent/config/schema.py — on the top-level config class (the one with `memory: ... = None`)
# 1. change the annotation:
    memory: Memory | None = None

# 2. add a backward-compat default-type validator (place inside the same class):
    @field_validator("memory", mode="before")
    @classmethod
    def _default_memory_type(cls, v: object) -> object:
        # Legacy configs render the Hindsight shape without an explicit `type`.
        if isinstance(v, dict) and "type" not in v:
            return {**v, "type": "hindsight"}
        return v
```

> Remove the old `class MemoryBlock(BaseModel): ...` definition. Grep for `MemoryBlock` references (`grep -rn MemoryBlock src tests`) and update them to `Memory` / the concrete class. `main.py` references are handled in Task 4; update any others (e.g. type hints) now.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_memory_union.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full config + lint suite**

Run: `uv run pytest tests/config -v && uv run ruff check src/ach_agent/config/schema.py && uv run mypy --strict src/ach_agent/config/schema.py`
Expected: PASS / no errors. Fix any `MemoryBlock` import breakages surfaced here.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/config/schema.py tests/config/test_memory_union.py tests/config/helpers.py
git commit -m "feat(config): memory.type discriminated union (hindsight|codemem)"
```

---

### Task 2: Emit codemem `type:local` MCP entry in opencode.json

**Files:**
- Modify: `src/ach_agent/engine/lifecycle.py` (`EngineConfig` dataclass + `write_opencode_config`)
- Test: `tests/engine/test_codemem_opencode_config.py`

**Interfaces:**
- Consumes: `EngineConfig.codemem_db_path: str` (Task 4 sets it).
- Produces: opencode.json `mcp.codemem = {"type":"local","command":["codemem","mcp","--db-path",<db>],"enabled":True,"environment":{"CODEMEM_VIEWER":"0","CODEMEM_VIEWER_AUTO":"0"}}` when `codemem_db_path` is non-empty; absent otherwise.

- [ ] **Step 1: Write the failing test**

```python
# tests/engine/test_codemem_opencode_config.py
import json
from pathlib import Path
from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config


def _read_oc(home: Path) -> dict:
    return json.loads((home / ".config" / "opencode" / "opencode.json").read_text())


def test_codemem_local_entry_written(tmp_path):
    cfg = EngineConfig(model_base_url="http://127.0.0.1:9/v1", codemem_db_path="/var/lib/codemem/a.db")
    write_opencode_config(tmp_path, cfg)
    mcp = _read_oc(tmp_path)["mcp"]
    assert mcp["codemem"] == {
        "type": "local",
        "command": ["codemem", "mcp", "--db-path", "/var/lib/codemem/a.db"],
        "enabled": True,
        "environment": {"CODEMEM_VIEWER": "0", "CODEMEM_VIEWER_AUTO": "0"},
    }


def test_no_codemem_entry_when_unset(tmp_path):
    cfg = EngineConfig(model_base_url="http://127.0.0.1:9/v1")
    write_opencode_config(tmp_path, cfg)
    oc = _read_oc(tmp_path)
    assert "codemem" not in oc.get("mcp", {})
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_codemem_opencode_config.py -v`
Expected: FAIL (`TypeError: __init__() got an unexpected keyword argument 'codemem_db_path'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/ach_agent/engine/lifecycle.py — add to the EngineConfig dataclass (next to mcp_local_urls):
    # codemem (MEM/D-02): when set, opencode.json registers a LOCAL stdio MCP server that
    # opencode spawns as its own child: `codemem mcp --db-path <db>`. Empty → no codemem.
    # Static per-agent db path (operator config). Viewer is disabled via env (headless).
    codemem_db_path: str = ""
```

```python
# src/ach_agent/engine/lifecycle.py — in write_opencode_config, AFTER the existing
# `for sid, url in config.mcp_local_urls.items(): ...` loop and BEFORE `if mcp_block:`:
    if config.codemem_db_path:
        # type:local — opencode owns the codemem stdio child (1:1 with this opencode process).
        # SEC: no ek_; codemem is local. Viewer disabled (headless, N sessions).
        mcp_block["codemem"] = {
            "type": "local",
            "command": ["codemem", "mcp", "--db-path", config.codemem_db_path],
            "enabled": True,
            "environment": {"CODEMEM_VIEWER": "0", "CODEMEM_VIEWER_AUTO": "0"},
        }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_codemem_opencode_config.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Run engine suite + types**

Run: `uv run pytest tests/engine -v && uv run mypy --strict src/ach_agent/engine/lifecycle.py`
Expected: PASS / no errors.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py tests/engine/test_codemem_opencode_config.py
git commit -m "feat(engine): write codemem type:local MCP entry in opencode.json"
```

---

### Task 3: `prepare_codemem` — fail-open PATH probe in the memory adapter

**Files:**
- Modify: `src/ach_agent/memory/adapter.py` (add `prepare_codemem`)
- Test: `tests/memory/test_prepare_codemem.py`

**Interfaces:**
- Consumes: a `CodememMemory` (has `.db_path`).
- Produces: `prepare_codemem(memory_cfg) -> tuple[bool, str]` → `(True, db_path)` if `codemem` is on PATH; `(False, "")` otherwise (logs WARN, increments `MEMORY_DEGRADED`). Never raises (D-02).

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_prepare_codemem.py
from ach_agent.config.schema import CodememMemory
from ach_agent.memory.adapter import prepare_codemem


def test_available_when_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")
    cfg = CodememMemory(type="codemem", dbPath="/var/lib/codemem/a.db")
    assert prepare_codemem(cfg) == (True, "/var/lib/codemem/a.db")


def test_degrades_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    cfg = CodememMemory(type="codemem", dbPath="/var/lib/codemem/a.db")
    assert prepare_codemem(cfg) == (False, "")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/memory/test_prepare_codemem.py -v`
Expected: FAIL (`ImportError: cannot import name 'prepare_codemem'`).

- [ ] **Step 3: Write minimal implementation**

```python
# src/ach_agent/memory/adapter.py — add (top-level), reusing the existing _inc_memory_degraded():
import shutil

if TYPE_CHECKING:
    from ach_agent.config.schema import CodememMemory


def prepare_codemem(memory_cfg: "CodememMemory") -> tuple[bool, str]:
    """Return (available, db_path) for the codemem stdio MCP backend.

    codemem is a LOCAL stdio MCP that opencode spawns itself (no probe of a remote
    endpoint). Availability = the `codemem` binary is on PATH. Fail-open (MEM-02/D-02):
    if absent, degrade (no memory tools) and never raise.
    """
    if shutil.which("codemem") is None:
        log.warning(
            "codemem binary not on PATH — running degraded (MEM-02, D-02)",
            db_path=memory_cfg.db_path,
        )
        _inc_memory_degraded()
        return False, ""
    return True, memory_cfg.db_path
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/memory/test_prepare_codemem.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/memory/adapter.py tests/memory/test_prepare_codemem.py
git commit -m "feat(memory): prepare_codemem fail-open PATH probe"
```

---

### Task 4: Wire codemem into boot + engine_runner

**Files:**
- Modify: `src/ach_agent/main.py` (boot: set `engine_cfg.codemem_db_path`; engine_runner: branch Hindsight-only probe)
- Test: `tests/test_main_memory_dispatch.py`

**Interfaces:**
- Consumes: `cfg.memory` (`HindsightMemory | CodememMemory | None`), `prepare_codemem`, `prepare_memory`.
- Produces: when `memory.type == "codemem"` → `engine_cfg.codemem_db_path` set at boot (iff `codemem` on PATH), engine_runner does NOT call `prepare_memory` and writes NO `## Memory` prompt; when `hindsight` → unchanged behaviour.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_main_memory_dispatch.py
# Verifies engine_runner branches by memory.type. Adapt imports/signature to the real
# engine_runner; this exercises the branch that selects mcp_servers vs codemem_db_path.
import dataclasses
import pytest
from ach_agent.config.schema import HindsightMemory, CodememMemory
from ach_agent.engine.lifecycle import EngineConfig


@pytest.mark.asyncio
async def test_codemem_type_skips_hindsight_probe(monkeypatch):
    from ach_agent import main as m
    called = {"prepare_memory": False}

    async def _boom(_cfg):
        called["prepare_memory"] = True
        return (True, "## Memory\nx")

    monkeypatch.setattr("ach_agent.memory.adapter.prepare_memory", _boom)
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")

    # The unit under test is the dispatch branch. If engine_runner is hard to call in
    # isolation, extract the branch into a helper `select_memory_wiring(memory_cfg)` and
    # test that instead (preferred — see Step 3).
    cfg = CodememMemory(type="codemem", dbPath="/var/lib/codemem/a.db")
    mcp_servers, memory_prompt, codemem_db = m.select_memory_wiring(cfg, available=lambda n: "/usr/bin/codemem")
    assert mcp_servers == []
    assert memory_prompt == ""
    assert codemem_db == "/var/lib/codemem/a.db"
    assert called["prepare_memory"] is False


@pytest.mark.asyncio
async def test_hindsight_type_uses_probe(monkeypatch):
    from ach_agent import main as m

    async def _ok(_cfg):
        return (True, "## Memory\nx")

    monkeypatch.setattr("ach_agent.memory.adapter.prepare_memory", _ok)
    cfg = HindsightMemory(type="hindsight", endpoint="http://mem:8080")
    mcp_servers, memory_prompt, codemem_db = await m.select_memory_wiring_async(cfg)
    assert mcp_servers == ["http://mem:8080"]
    assert memory_prompt == "## Memory\nx"
    assert codemem_db == ""
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_memory_dispatch.py -v`
Expected: FAIL (`AttributeError: module 'ach_agent.main' has no attribute 'select_memory_wiring_async'`).

- [ ] **Step 3: Write minimal implementation**

Extract the dispatch into a small testable helper, then call it from engine_runner.

```python
# src/ach_agent/main.py — add near engine_runner:
async def select_memory_wiring_async(memory_cfg) -> tuple[list[str], str, str]:
    """Resolve (mcp_servers, memory_prompt, codemem_db_path) for one invocation.

    Hindsight: probe endpoint + fetch prompt (existing prepare_memory). Returns the
    endpoint as a remote MCP server iff reachable, plus the '## Memory' prompt section.
    codemem: no probe of a remote; codemem is a stdio-local MCP. Returns codemem_db_path
    iff the binary is on PATH (prepare_codemem), no prompt, no remote mcp server.
    """
    if memory_cfg is None:
        return [], "", ""
    if memory_cfg.type == "codemem":
        from ach_agent.memory.adapter import prepare_codemem

        available, db_path = prepare_codemem(memory_cfg)
        return [], "", (db_path if available else "")
    # hindsight (default)
    from ach_agent.memory.adapter import prepare_memory

    mem_available, memory_prompt = await prepare_memory(memory_cfg)
    mcp_servers = [memory_cfg.endpoint] if mem_available else []
    return mcp_servers, memory_prompt, ""
```

```python
# src/ach_agent/main.py — in engine_runner, REPLACE the existing block:
#     memory_prompt = ""
#     if memory_cfg is not None:
#         from ach_agent.memory.adapter import prepare_memory
#         mem_available, memory_prompt = await prepare_memory(memory_cfg)
#         mcp_servers = [memory_cfg.endpoint] if mem_available else []
#     else:
#         mcp_servers = []
# WITH:
        mcp_servers, memory_prompt, codemem_db = await select_memory_wiring_async(memory_cfg)
```

```python
# src/ach_agent/main.py — extend the per-invocation engine_cfg copy to carry codemem_db:
        if dataclasses.is_dataclass(engine_cfg) and not isinstance(engine_cfg, type):
            invocation_engine_cfg = dataclasses.replace(
                engine_cfg, mcp_servers=mcp_servers, codemem_db_path=codemem_db
            )
        else:
            invocation_engine_cfg = engine_cfg
            invocation_engine_cfg.mcp_servers = mcp_servers
            invocation_engine_cfg.codemem_db_path = codemem_db
```

```python
# src/ach_agent/main.py — the `memory_bank` line currently reads `cfg.memory.bank`.
# bank exists ONLY on HindsightMemory now. Guard it:
    memory_bank = (
        cfg.memory.bank
        if cfg.memory is not None and cfg.memory.type == "hindsight"
        else ""
    )
```

> For the sync test helper `select_memory_wiring` referenced in Step 1's first test: if you keep only the async helper, drop that first assertion's sync call and make both tests use `select_memory_wiring_async`. The async helper is the real one; do not ship a duplicate sync version (YAGNI).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_memory_dispatch.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite + types + lint**

Run: `uv run pytest -q && uv run mypy --strict src/ach_agent && uv run ruff check src/ach_agent`
Expected: PASS / no errors. Fix any remaining `cfg.memory.endpoint`/`.bank` accesses that assume the flat block.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_memory_dispatch.py
git commit -m "feat(main): dispatch memory wiring by memory.type (hindsight|codemem)"
```

---

### Task 5: Ship `codemem` in the runtime image

**Files:**
- Modify: `Dockerfile` (install Node 24 + codemem so `codemem` is on PATH)
- Modify: `.dockerignore` (ensure no `node_modules`/state leak; keep explicit COPY)
- Test: manual image build + `docker run ... codemem --version`

**Interfaces:**
- Produces: a runtime image where `which codemem` succeeds (opencode's clean-slate env keeps `PATH`, so a global install is reachable).

- [ ] **Step 1: Read the current Dockerfile**

Run: `sed -n '1,200p' Dockerfile`
Confirm where opencode/Node are already installed (opencode itself needs a runtime). Reuse that Node 24 layer if present; do not add a second Node.

- [ ] **Step 2: Add codemem install (explicit, pinned)**

```dockerfile
# In the runtime stage, after Node 24 is available (opencode already needs a JS runtime).
# Pin the version for reproducibility; bump deliberately.
RUN npm install -g codemem@<PINNED_VERSION> \
    && codemem --version
```

> Find `<PINNED_VERSION>`: run `npm view codemem version` locally and pin that exact value. Do NOT use a floating tag.

- [ ] **Step 3: Build and verify the binary is present**

Run:
```bash
docker build -t ach-agent:codemem-test .
docker run --rm ach-agent:codemem-test sh -lc 'command -v codemem && codemem mcp --help | head -5'
```
Expected: prints a codemem path and the `--db-path` option line.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile .dockerignore
git commit -m "build: install codemem CLI in runtime image"
```

---

### Task 6: Verify end-to-end with a hand-written config (local, no k8s)

**Files:**
- Create: `tests/integration/test_codemem_smoke.py` (opt-in / marked `integration`)
- Uses: a hand-written config with `memory: {type: codemem, dbPath: <tmp>}`

**Interfaces:**
- Consumes: everything above. Confirms opencode boots `--pure`, registers the codemem stdio MCP, and the model can list codemem tools.

- [ ] **Step 1: Write the smoke test (skipped unless `codemem` + `opencode` present)**

```python
# tests/integration/test_codemem_smoke.py
import shutil
import pytest

pytestmark = pytest.mark.integration

requires_bins = pytest.mark.skipif(
    shutil.which("codemem") is None or shutil.which("opencode") is None,
    reason="codemem and opencode binaries required",
)


@requires_bins
@pytest.mark.asyncio
async def test_opencode_registers_codemem_mcp(tmp_path):
    # Build EngineConfig with codemem_db_path -> tmp DB, launch via the engine, then assert
    # the opencode session exposes codemem tools (search/remember/timeline) via the
    # OpenCodeClient tool listing. Reuse the existing engine launch/test helpers.
    from ach_agent.engine.lifecycle import EngineConfig
    # ... launch using the project's existing engine test harness; assert a codemem.* tool id
    #     appears in the model's available tools. Keep this thin — it is a smoke test.
    assert True  # replace with the real assertion using the engine test helper
```

- [ ] **Step 2: WAL verification (concurrency)**

Run two codemem stdio servers against the SAME db file and confirm no `database is locked` on concurrent writes:
```bash
DB=$(mktemp -u --suffix=.db)
# open two writers via the MCP `remember` tool, or via codemem CLI if it exposes a write subcommand.
codemem mcp --db-path "$DB" &  P1=$!
codemem mcp --db-path "$DB" &  P2=$!
sleep 2; kill $P1 $P2 2>/dev/null
# Inspect journal mode:
sqlite3 "$DB" 'PRAGMA journal_mode;'
```
Expected: `wal`. If it is NOT `wal`, codemem does not enable WAL by default — file a follow-up (open question: how to force WAL; may need a codemem flag/env or accept single-writer-per-repo by pinning pool affinity). Record the result in the PR description.

- [ ] **Step 3: Commit**

```bash
git add tests/integration/test_codemem_smoke.py
git commit -m "test(integration): codemem MCP smoke + WAL check"
```

---

## Self-Review

- **Spec coverage:** `--pure` kept (Global Constraints, no task removes it ✓); discriminated union (Task 1 ✓); stdio `type:local` entry w/ `--db-path` + viewer-off (Task 2 ✓); fail-open PATH probe (Task 3 ✓); model-managed = no inject/no capture (no inject task exists, by design ✓); db_path static + anti-`..` validator (Task 1 ✓); ship codemem on PATH (Task 5 ✓); ek-hygiene (codemem inherits clean env, no secret in entry — Task 2 ✓); WAL flagged (Task 6 ✓).
- **Open decisions surfaced (NOT silently resolved):** (1) per-repo multi-tenant db_path templating = OUT of v1 (pooling rationale) — confirm acceptable; (2) WAL default — verified in Task 6, follow-up if not `wal`; (3) CONTRACT_v3 + ach-runtime rendering of `memory.type` = separate coordinated change (harness is backward-compatible meanwhile).
- **Type consistency:** `codemem_db_path` used identically in EngineConfig (Task 2), main.py (Task 4); `prepare_codemem` signature `(memory_cfg) -> (bool, str)` consistent across Task 3 and Task 4; `Memory`/`HindsightMemory`/`CodememMemory` names consistent across Tasks 1 and 4.
- **Placeholder scan:** `<PINNED_VERSION>` (Task 5) and the integration assertion (Task 6) are deliberately parameterized with explicit instructions to resolve them at execution — not silent TODOs.
