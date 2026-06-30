# prompt.system source (text | file) + `.ach-state` root — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `prompt.system` source the agent persona from a hydrated prompt file (`{type: file}`) or inline (`{type: text}`), resolved under a pinned `.ach-state` hydration root.

**Architecture:** `prompt.system` becomes a discriminated union (`type` required, no string shorthand). Hydrated `prompts`/`artifacts` move from `<mountPath>/{kind}` to `<home>/.ach-state/{kind}`; a `<workDir>/.ach-state` symlink gives the agent one stable path. The harness resolves `{type:file}` against `.ach-state` (reject absolute/`..`, missing = startup fail), then materializes the persona into `system_prompt.txt` exactly as today.

**Tech Stack:** Python 3.12, Pydantic v2 (discriminated union + `field_validator`), pytest. Spec: `docs/plan/CONTRACT_v3-ADDENDUM-prompt-source.md`.

## Global Constraints

- `prompt.system` MUST be `{type:"text",text} | {type:"file",file}`; `type` is required; **the plain-string form is rejected** (lockstep break — this is intended).
- `{type:file}` `file` is **relative to `<home>/.ach-state`**; absolute or any `..` part is a hard `ValidationError` at load; the resolved real path is re-checked to stay inside `.ach-state` at read time; a missing file is a **startup failure (`sys.exit(1)`)**, never fail-open.
- `compose` is unchanged: contract-reserved, accepted, not executed.
- `ACH_STATE = <home>/.ach-state`. Skills are UNCHANGED (`<home>/.config/opencode/skills/<name>`). Only `prompts`/`artifacts` relocate.
- ek-hygiene: nothing here logs or materializes the `ek_`. `.ach-state` stays under HOME, distinct from the mountPath ek material.
- All blocks keep `ConfigDict(extra="forbid")`. `uv run mypy --strict` and `uv run pytest` must pass. Pyright/LSP diagnostics in this repo are FALSE (wrong interpreter) — mypy is authoritative.

---

### Task 1: `PromptBlock` discriminated union + path-traversal validator

**Files:**
- Modify: `src/ach_agent/config/schema.py:97-106` (PromptBlock) + imports at `:14-18`
- Test: `tests/config/test_schema.py`

**Interfaces:**
- Produces: `SystemText{type:"text",text:str}`, `SystemFile{type:"file",file:str}`, `SystemPrompt = Annotated[SystemText|SystemFile, discriminator="type"]`, `PromptBlock.system: SystemPrompt | None = None`. Task 3 consumes `cfg.prompt.system` (a `SystemText | SystemFile | None`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/config/test_schema.py`:
```python
import pytest
from pydantic import ValidationError
from ach_agent.config.schema import PromptBlock, SystemText, SystemFile


def test_prompt_system_text_form():
    b = PromptBlock.model_validate({"system": {"type": "text", "text": "hi"}, "compose": "append"})
    assert isinstance(b.system, SystemText)
    assert b.system.text == "hi"


def test_prompt_system_file_form():
    b = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/p/x.md"}})
    assert isinstance(b.system, SystemFile)
    assert b.system.file == "prompts/p/x.md"


def test_prompt_system_string_shorthand_rejected():
    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": "an inline persona"})


def test_prompt_system_missing_type_rejected():
    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"text": "no type"}})


def test_prompt_system_file_absolute_rejected():
    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"type": "file", "file": "/etc/passwd"}})


def test_prompt_system_file_traversal_rejected():
    with pytest.raises(ValidationError):
        PromptBlock.model_validate({"system": {"type": "file", "file": "../../secrets/ek"}})


def test_prompt_system_omitted_is_none():
    assert PromptBlock.model_validate({"compose": "append"}).system is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/config/test_schema.py -k "prompt_system" -v`
Expected: FAIL (ImportError on `SystemText`/`SystemFile`, or string form currently accepted).

- [ ] **Step 3: Implement the union**

In `src/ach_agent/config/schema.py`, update imports:
```python
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator
```

Replace `PromptBlock` (lines 97-106) with:
```python
class SystemText(BaseModel):
    """Inline persona text."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["text"]
    text: str


class SystemFile(BaseModel):
    """Persona sourced from a hydrated prompt file under <home>/.ach-state."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["file"]
    file: str  # relative to <home>/.ach-state; absolute or ".." is rejected

    @field_validator("file")
    @classmethod
    def _no_escape(cls, v: str) -> str:
        p = PurePosixPath(v)
        if p.is_absolute() or ".." in p.parts:
            raise ValueError(
                "prompt.system.file must be a relative path under .ach-state, no '..'"
            )
        return v


# Discriminated on `type`: the string shorthand is intentionally NOT accepted (CONTRACT_v3
# ADDENDUM-prompt-source §1 — the operator renders the object form).
SystemPrompt = Annotated[SystemText | SystemFile, Field(discriminator="type")]


class PromptBlock(BaseModel):
    """CONTRACT §2 prompt block."""

    model_config = ConfigDict(extra="forbid")

    # text | file source; omitted → no persona (today's ""). The plain-string form is rejected.
    system: SystemPrompt | None = None
    # Contract-reserved (CONTRACT §2): the operator renders it; the harness accepts it but
    # does NOT yet execute layering. Do not remove without a coordinated CONTRACT_v3 change.
    compose: str = "append"
```

- [ ] **Step 4: Migrate existing test/fixtures that use the string form**

Run: `grep -rn '"system"\|system:' tests/ | grep -iv 'systemtext\|systemfile\|system_prompt' `
For every config built with `system: "<string>"` or `{"system": "<string>"}`, change to `{"type": "text", "text": "<string>"}`. The known one: `test_contract_reserved_fields_accepted` in `tests/config/test_schema.py` — update its `prompt` to the text form. Check `tests/conformance/` fixtures too.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/config/test_schema.py -v && uv run mypy --strict src/ach_agent/config/schema.py`
Expected: PASS, mypy clean.

- [ ] **Step 6: Commit**
```bash
git add src/ach_agent/config/schema.py tests/
git commit -m "feat(config): prompt.system text|file discriminated union (no string shorthand)"
```

---

### Task 2: `.ach-state` hydration root + workDir symlink

**Files:**
- Modify: `src/ach_agent/main.py` — add `ach_state_dir(home)` helper + `link_ach_state(home, work_dir)`; change the `fetch_context(...)` call (`main.py:586-591`)
- Test: `tests/engine/test_context.py` (create if absent)

**Interfaces:**
- Consumes: `resolve_engine_paths(cfg) -> (home, work_dir)` (main.py:215), `fetch_context(ctx, ek, root, skills_dir)` (engine/context.py:33).
- Produces: `ach_state_dir(home: str) -> Path` returning `Path(home)/".ach-state"`; `link_ach_state(home, work_dir) -> Path` (creates the dir + best-effort `<work_dir>/.ach-state` symlink, returns the real dir). Task 3 consumes `ach_state_dir(home)`.

- [ ] **Step 1: Write the failing test**

`tests/engine/test_context.py`:
```python
from pathlib import Path
from ach_agent.main import ach_state_dir, link_ach_state


def test_ach_state_dir_under_home(tmp_path):
    assert ach_state_dir(str(tmp_path)) == tmp_path / ".ach-state"


def test_link_ach_state_symlinks_workdir(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir(); work.mkdir()
    real = link_ach_state(str(home), str(work))
    assert real == home / ".ach-state"
    assert real.is_dir()
    link = work / ".ach-state"
    assert link.is_symlink()
    assert link.resolve() == real.resolve()


def test_link_ach_state_no_symlink_when_workdir_equals_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    real = link_ach_state(str(home), str(home))
    assert real == home / ".ach-state"
    assert not (home / ".ach-state" / ".ach-state").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/test_context.py -v`
Expected: FAIL (ImportError on `ach_state_dir`).

- [ ] **Step 3: Implement the helpers**

In `src/ach_agent/main.py`, just after `resolve_engine_paths` (ends at `:227`):
```python
def ach_state_dir(home: str) -> Path:
    """The single hydration state root: <home>/.ach-state (prompts + artifacts)."""
    return Path(home) / ".ach-state"


def link_ach_state(home: str, work_dir: str) -> Path:
    """Create <home>/.ach-state and, when workDir differs, a <workDir>/.ach-state symlink.

    The symlink gives the agent's shell (cwd = workDir) one stable path to hydrated
    artifacts; HOME stays the canonical read-only root. Best-effort: a symlink failure
    (e.g. unsupported FS) is non-fatal — the agent can still reach state under HOME.
    """
    state = ach_state_dir(home)
    state.mkdir(parents=True, exist_ok=True)
    if work_dir and Path(work_dir).resolve() != Path(home).resolve():
        link = Path(work_dir) / ".ach-state"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            try:
                link.symlink_to(state, target_is_directory=True)
            except OSError as e:
                log.warning("workDir .ach-state symlink failed (non-fatal)", error=str(e))
    return state
```
(`Path` and `log` are already imported in main.py.)

- [ ] **Step 4: Point hydration at `.ach-state`**

In `src/ach_agent/main.py`, just after `engine_home`/`engine_work_dir` are resolved (the `resolve_engine_paths` call, ~`:543`), add:
```python
    state_dir = link_ach_state(engine_home, engine_work_dir)
```
Then change the `fetch_context(...)` call (`main.py:586-591`) — replace the `Path(cfg.persistence.mount_path)` root argument with `state_dir`:
```python
        await fetch_context(
            manifest.context,
            ek,
            state_dir,
            Path(engine_home) / ".config" / "opencode" / "skills",
        )
```
(No change to `engine/context.py` — it already writes `root/<kind>/<name>`; `root` is now `.ach-state`.)

- [ ] **Step 5: Run tests**

Run: `uv run pytest tests/engine/test_context.py -v && uv run mypy --strict src/ach_agent/main.py`
Expected: PASS, mypy clean.

- [ ] **Step 6: Commit**
```bash
git add src/ach_agent/main.py tests/engine/test_context.py
git commit -m "feat(engine): hydrate prompts/artifacts under <home>/.ach-state (+ workDir symlink)"
```

---

### Task 3: Resolve `prompt.system` (text | file) into the persona string

**Files:**
- Modify: `src/ach_agent/main.py` — add `resolve_system_prompt(...)`; change the `EngineConfig(... system_prompt=...)` build (`main.py:714`)
- Test: `tests/engine/test_system_prompt.py`

**Interfaces:**
- Consumes: `PromptBlock.system` (`SystemText | SystemFile | None`, Task 1), `ach_state_dir(home)` (Task 2).
- Produces: `resolve_system_prompt(prompt_block, ach_state_dir: Path) -> str`.

- [ ] **Step 1: Write the failing tests**

`tests/engine/test_system_prompt.py`:
```python
import pytest
from pathlib import Path
from ach_agent.config.schema import PromptBlock
from ach_agent.main import resolve_system_prompt


def test_text_form_returns_inline(tmp_path):
    pb = PromptBlock.model_validate({"system": {"type": "text", "text": "persona X"}})
    assert resolve_system_prompt(pb, tmp_path) == "persona X"


def test_none_returns_empty(tmp_path):
    assert resolve_system_prompt(PromptBlock.model_validate({}), tmp_path) == ""
    assert resolve_system_prompt(None, tmp_path) == ""


def test_file_form_reads_under_state(tmp_path):
    f = tmp_path / "prompts" / "p" / "x.md"
    f.parent.mkdir(parents=True)
    f.write_text("from file", encoding="utf-8")
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/p/x.md"}})
    assert resolve_system_prompt(pb, tmp_path) == "from file"


def test_file_missing_exits(tmp_path):
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "prompts/none.md"}})
    with pytest.raises(SystemExit):
        resolve_system_prompt(pb, tmp_path)


def test_file_symlink_escape_exits(tmp_path):
    # a file that resolves outside .ach-state via a symlink is rejected at read time
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    state = tmp_path / "state"
    state.mkdir()
    (state / "evil.md").symlink_to(outside)
    pb = PromptBlock.model_validate({"system": {"type": "file", "file": "evil.md"}})
    with pytest.raises(SystemExit):
        resolve_system_prompt(pb, state)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/test_system_prompt.py -v`
Expected: FAIL (ImportError on `resolve_system_prompt`).

- [ ] **Step 3: Implement the resolver**

In `src/ach_agent/main.py`, near the other engine-path helpers:
```python
def resolve_system_prompt(prompt_block: Any, state_dir: Path) -> str:
    """Resolve prompt.system (text | file | None) into the persona string.

    text → the inline text. file → bytes of <state_dir>/<file>, with the resolved real
    path re-checked to stay inside state_dir (defense in depth over the schema validator,
    which only sees the literal path). A missing file is a hard startup failure: a persona
    the operator declared but hydration did not deliver is a misconfiguration, not fail-open.
    None → "" (no persona).
    """
    if prompt_block is None or prompt_block.system is None:
        return ""
    system = prompt_block.system
    if system.type == "text":
        return str(system.text)
    root = state_dir.resolve()
    target = (root / system.file).resolve()
    if not target.is_relative_to(root):
        log.error("prompt.system.file escapes .ach-state", file=system.file)
        sys.exit(1)
    if not target.is_file():
        log.error("prompt.system.file not found under .ach-state", path=str(target))
        sys.exit(1)
    return target.read_text(encoding="utf-8")
```
(`Any`, `Path`, `sys`, `log` already imported in main.py.)

- [ ] **Step 4: Wire it into the EngineConfig build**

In `src/ach_agent/main.py:714`, replace:
```python
        system_prompt=cfg.prompt.system if cfg.prompt else "",
```
with:
```python
        system_prompt=resolve_system_prompt(cfg.prompt, state_dir),
```
(`state_dir` is in scope from Task 2's `link_ach_state` call at ~`:543`.)

- [ ] **Step 5: Run tests + a focused mypy**

Run: `uv run pytest tests/engine/test_system_prompt.py -v && uv run mypy --strict src/ach_agent/main.py`
Expected: PASS, mypy clean.

- [ ] **Step 6: Commit**
```bash
git add src/ach_agent/main.py tests/engine/test_system_prompt.py
git commit -m "feat(engine): resolve prompt.system text|file into persona (file under .ach-state)"
```

---

### Task 4: Migrate contract docs, examples, and the test agent config

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (§2 prompt line `:83`, §2 prose `:132-136`, §3 layout `:176-177`)
- Modify: `example.yaml`, `docs/configuration.md`, `docs/getting-started.md`, `docker/quickstart/config.yaml`
- Modify (external repo): `../ach-agent-test/config.yaml`

**Interfaces:** none (docs/config only). No test code; verification is schema-validation of the migrated configs.

- [ ] **Step 1: Update `CONTRACT_v3.md`**

- `:83` replace with the object form:
  ```jsonc
  "prompt": { "system": { "type": "text", "text": "…agent persona (markdown ok)…" }, "compose": "append" },
  ```
- In the §2 prose (`:132-136`), and §3 (`:176-177`), replace the prompts/artifacts location with `<home>/.ach-state/{prompts,artifacts}/<name>` and reference the addendum. Add one line: "`prompt.system` is a typed source (`text`|`file`); see `CONTRACT_v3-ADDENDUM-prompt-source.md`."

- [ ] **Step 2: Migrate `example.yaml`**

Replace the `prompt:` block with the text form, and add a commented file-form alternative:
```yaml
prompt:
  system:
    type: text
    text: "You are a senior code reviewer for the platform team."
  # file form — source the persona from a hydrated prompt (relative to <home>/.ach-state):
  # system:
  #   type: file
  #   file: prompts/<prompt-name>/<file>.md
  compose: append                       # reserved: accepted, prompt-layering not yet executed
```

- [ ] **Step 3: Migrate `docs/configuration.md` + `docs/getting-started.md`**

- In `docs/configuration.md`: update the `prompt` row of the Blocks table (`system` is now `{type:text,text}|{type:file,file}`; file is relative to `<home>/.ach-state`), and update the full-example `prompt:` block to the text form (mirror Step 2).
- In `docs/getting-started.md`: change any `system: "..."` to the text form.

- [ ] **Step 4: Migrate `docker/quickstart/config.yaml`**

Change its `prompt.system` string to:
```yaml
prompt:
  system:
    type: text
    text: "<existing persona text>"
  compose: append
```

- [ ] **Step 5: Migrate the test agent (file form — the original ask)**

In `../ach-agent-test/config.yaml`, switch `prompt.system` to the file form pointing at the hydrated prompt:
```yaml
prompt:
  system:
    type: file
    file: prompts/claude-plugins-ackstorm-prompt1/example1.md
  compose: append
```
NOTE: this requires the hydrated prompt `claude-plugins-ackstorm-prompt1` to actually contain `example1.md`. If hydration delivers a different filename, the harness will exit at boot with "prompt.system.file not found under .ach-state" — adjust the `file:` to the real path then.

- [ ] **Step 6: Verify every migrated config validates**

Run:
```bash
uv run python - <<'PY'
import yaml
from ach_agent.config.schema import AgentConfig
for p in ["example.yaml", "docker/quickstart/config.yaml"]:
    AgentConfig.model_validate(yaml.safe_load(open(p)))
    print("OK", p)
PY
```
Expected: `OK example.yaml` / `OK docker/quickstart/config.yaml`. (The `../ach-agent-test` file lives in another repo; validate it manually with the same snippet if desired.)

- [ ] **Step 7: Commit**
```bash
git add docs/ example.yaml docker/quickstart/config.yaml
git commit -m "docs(contract): migrate prompt.system to typed source + .ach-state layout"
```
(Commit `../ach-agent-test/config.yaml` separately in that repo.)
