# Phase 1 — Driver seam foundation

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Steps use `- [ ]`. Read [index.md](index.md) first — Global Constraints and the Shared interface contract apply to every task.

**Goal:** Introduce the `engine/base/` package with the `EngineDriver` protocol, the `TurnResult` type, and the relocated `EngineConfig` (gaining `engine_type`). Purely additive — no behavior changes, no file moves yet.

**Exit criterion:** New protocol/type tests pass; the entire existing suite + `make conformance` stay green (nothing imports the new package yet except the new test).

**Files:**
- Create: `src/ach_agent/engine/base/__init__.py`
- Create: `src/ach_agent/engine/base/driver.py`
- Modify: `src/ach_agent/engine/lifecycle.py:67-123` (replace the `EngineConfig` dataclass with a re-export)
- Create: `tests/engine/base/__init__.py`
- Create: `tests/engine/base/test_driver.py`

**Interfaces:**
- Produces (consumed by Phases 3, 4, 6, 8): `EngineConfig` (now in `base/driver.py`, `+engine_type: str = "opencode"`), `TurnResult`, `EngineDriver` (see index Shared interface contract for the canonical signatures).
- Consumes: `ManagedServer` (still in `lifecycle.py`) and `OpenCodeToolUpdate` (still in `engine/events.py`) — imported under `TYPE_CHECKING` only, so no import cycle and no dependency on later phases.

---

### Task 1.1: `engine/base/driver.py` — protocol, `TurnResult`, relocated `EngineConfig`

- [ ] **Step 1: Write the failing test**

Create `tests/engine/base/__init__.py` (empty) and `tests/engine/base/test_driver.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path
from typing import Any
from collections.abc import Callable, MutableMapping

from ach_agent.engine.base.driver import EngineConfig, EngineDriver, TurnResult


def test_engine_config_defaults_engine_type_opencode() -> None:
    cfg = EngineConfig()
    assert cfg.engine_type == "opencode"


def test_lifecycle_reexports_the_same_engine_config() -> None:
    # The shim in lifecycle.py must resolve to the SAME class object (identity), so
    # every existing `from ach_agent.engine.lifecycle import EngineConfig` is unaffected.
    from ach_agent.engine.lifecycle import EngineConfig as LifecycleEngineConfig

    assert LifecycleEngineConfig is EngineConfig


def test_turn_result_defaults() -> None:
    r = TurnResult(text="hi", session_ref="ses_1")
    assert r.text == "hi"
    assert r.session_ref == "ses_1"
    assert r.aborted is False


def test_stub_satisfies_engine_driver_protocol() -> None:
    class _Stub:
        engine_type = "opencode"

        def skills_dir(self, home: Path) -> Path:
            return home / "skills"

        async def launch(self, cfg: EngineConfig, session_key: str) -> Any:
            return object()

        async def health(self, server: Any) -> bool:
            return True

        async def run_turn(
            self,
            server: Any,
            *,
            conv_key: str,
            prompt: str,
            reuse: bool,
            sessions: MutableMapping[str, str],
            session_ref: str | None = None,
            on_text: Callable[[str], None] | None = None,
            on_tool: Callable[[Any], None] | None = None,
            max_tool_calls: int = 0,
            stats: dict[str, Any] | None = None,
        ) -> TurnResult:
            return TurnResult(text="", session_ref="ses_1")

        async def discard_session(self, server: Any, session_ref: str) -> None: ...
        async def compact_session(self, server: Any, session_ref: str) -> None: ...
        async def stop(self, server: Any) -> None: ...

    assert isinstance(_Stub(), EngineDriver)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/engine/base/test_driver.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.base'`.

- [ ] **Step 3: Create the `base` package + `driver.py`**

Create `src/ach_agent/engine/base/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Engine-agnostic seam: EngineDriver protocol, shared config, pool, terminal contract."""
```

Create `src/ach_agent/engine/base/driver.py`. **Copy the `EngineConfig` dataclass verbatim from `lifecycle.py:67-123`** (every field + its comments), then add the `engine_type` field, and add `TurnResult` + `EngineDriver`:

```python
# SPDX-License-Identifier: Apache-2.0
"""EngineDriver seam — the symmetric abstraction over opencode and Pi (SP1 §4.2).

`router/lane.py` calls the engine only as the opaque injected `engine_runner`; it never
imports anything here (D-08 / RTR-06). All engine specifics live behind `EngineDriver`.
"""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ach_agent.engine.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer


@dataclass
class EngineConfig:
    """Rendered runtime config — engine section (CONTRACT.md §2).

    Fields extended by later plans as needed.
    """

    binary_path: str = "opencode"
    # <<< COPY every field + comment from the current lifecycle.py EngineConfig (67-123):
    #     home, work_dir, model, model_type, params, system_prompt, compose, steps,
    #     startup_timeout_seconds, max_invocation_seconds, mcp_servers, model_base_url,
    #     mcp_local_urls, codemem_db_path, codemem_project, extra_mcp_servers,
    #     forward_env, exclude_tools  — UNCHANGED. >>>

    # SP1: which driver runs this config. "opencode" | "pi". Selects the EngineDriver in
    # _make_engine_runner (main.py) and namespaces the pool sessions map (base/pool.py) so an
    # opencode ses_ id and a Pi session-file path never collide on a persisted home.
    engine_type: str = "opencode"


@dataclass
class TurnResult:
    """Result of ONE prompt turn (SP1 §4.3, the Fine boundary).

    `text` is the raw final assistant text — NOT validated here. `session_ref` is the
    engine-native handle the turn ran in (opencode: ``ses_…`` id; Pi: session-file path);
    ``base/terminal.py`` targets repair/wrap-up turns at it and post-turn hygiene keys on it.
    `aborted` is set when the step budget (``max_tool_calls``) cut the turn — such a turn
    usually lacks a terminal object, so ``base/terminal.py`` runs one wrap-up turn.
    """

    text: str
    session_ref: str
    aborted: bool = False


@runtime_checkable
class EngineDriver(Protocol):
    """Everything the harness needs from an engine, symmetric across opencode and Pi."""

    engine_type: str

    def skills_dir(self, home: Path) -> Path:
        """The SHARED skills extract dir under ``home``. No ``session_key``: hydration runs
        ONCE at boot (main.py:1240) before any key exists; every per-key config points here."""
        ...

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer: ...

    async def health(self, server: ManagedServer) -> bool: ...

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],
        session_ref: str | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
        max_tool_calls: int = 0,
        stats: dict[str, Any] | None = None,
    ) -> TurnResult:
        """Run ONE prompt. If ``session_ref`` is given, continue exactly that engine session
        (repair/wrap-up) and bypass ``conv_key``/``reuse``/the map. Writes the final ref into
        ``stats['session_ref']`` (opencode also writes ``stats['oc_session_id']``)."""
        ...

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None: ...
    async def compact_session(self, server: ManagedServer, session_ref: str) -> None: ...
    async def stop(self, server: ManagedServer) -> None: ...
```

- [ ] **Step 4: Repoint `lifecycle.py` to re-export `EngineConfig`**

In `src/ach_agent/engine/lifecycle.py`, delete the `@dataclass class EngineConfig: …` block (lines 67-123) and replace it with a re-export near the top imports:

```python
# EngineConfig now lives in engine/base/driver.py (SP1 seam). Re-exported here so every
# existing `from ach_agent.engine.lifecycle import EngineConfig` keeps resolving unchanged.
from ach_agent.engine.base.driver import EngineConfig  # noqa: E402  (kept beside other engine imports)
```

Place this import with the other `from ach_agent.engine.*` imports (not under `TYPE_CHECKING`). Keep `ManagedServer` and everything else in `lifecycle.py` exactly as-is. `base/driver.py` only imports `ManagedServer` under `TYPE_CHECKING`, so there is no runtime import cycle.

- [ ] **Step 5: Run the new test + the engine suite**

Run: `uv run pytest tests/engine/base/test_driver.py tests/engine/ -q`
Expected: PASS (new tests green; existing engine tests unaffected).

- [ ] **Step 6: Type-check**

Run: `uv run mypy --strict src/ach_agent/engine/base/ src/ach_agent/engine/lifecycle.py`
Expected: no errors.

- [ ] **Step 7: Conformance gate**

Run: `make conformance`
Expected: all 11 invariants PASS (nothing behavioral changed).

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/engine/base/ tests/engine/base/ src/ach_agent/engine/lifecycle.py
git commit -m "feat(engine): add EngineDriver seam (base/driver.py) + relocate EngineConfig"
```

---

## Self-review (Phase 1)

- **Spec coverage:** §4.2 protocol shape (with the review corrections: `skills_dir(home)` no `session_key`; `session_ref` continue-affordance; `discard_session`/`compact_session` on the driver) — all present. `engine_type` field enables the §5.4 namespacing done in Phase 4.
- **Placeholders:** the only `<<< COPY … >>>` marker is a deliberate "relocate verbatim" instruction for a 57-line dataclass; reproducing it here would risk drift from the file. The exact source range (`lifecycle.py:67-123`) is given and the identity test (`test_lifecycle_reexports_the_same_engine_config`) proves the move preserved the class.
- **Type consistency:** `run_turn` keyword-only signature matches the index contract and the calls made in Phases 3/6/8 (`sessions=`, `session_ref=`, `stats=`). `TurnResult` fields (`text`, `session_ref`, `aborted`) match every consumer.
