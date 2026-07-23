# Phase 6 — Rewire `engine_runner` through the driver

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first. This is the seam's payoff: the harness now drives whatever `EngineDriver` `engine.type` selects. Opencode must be **byte-for-byte identical** end-to-end.

**Goal:** Select the driver at boot from `cfg.engine.type`, pass it to `EnginePool` and `_make_engine_runner`; replace the `run_invocation` call with `run_contract_turn(driver, …, sessions=pool.sessions)`; key post-turn hygiene on `stats["session_ref"]` via `driver.discard_session` / `driver.compact_session`. Introduce a `PiDriver` **stub** so `type: pi` selection type-checks (Phase 8 fills it in).

**Exit criterion:** `make conformance` + full suite green; opencode behavior unchanged; `type: pi` selects `PiDriver` (which is a stub that raises on `launch` until Phase 8).

**Files:**
- Create: `src/ach_agent/engine/pi/__init__.py`, `src/ach_agent/engine/pi/driver.py` (stub)
- Modify: `src/ach_agent/main.py` — `_make_engine_runner` signature (`_make_engine_runner` def ~630) + body (imports ~679, invocation ~816-829, hygiene ~850-881); boot driver selection + `EnginePool` + `EngineConfig` construction (~1372-1420); `_make_engine_runner(...)` call (~1450)
- Create: `tests/engine/pi/__init__.py`, `tests/engine/pi/test_driver_stub.py`

**Interfaces:**
- Consumes: Phases 3 (`OpencodeDriver`, `run_contract_turn`), 4 (`EnginePool(driver=…)`, `pool.sessions`), 5 (`cfg.engine.type`, `cfg.engine.pi`).
- Produces (consumed by Phase 8): `engine/pi/driver.py::PiDriver` (stub → real in Phase 8), the boot driver-selection block.

---

### Task 6.1: Pi stub package + boot driver selection

- [ ] **Step 1: Write the failing test**

Create `tests/engine/pi/__init__.py` (empty) and `tests/engine/pi/test_driver_stub.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ach_agent.engine.base.driver import EngineDriver
from ach_agent.engine.pi.driver import PiDriver


def test_pi_driver_satisfies_protocol() -> None:
    assert isinstance(PiDriver(), EngineDriver)
    assert PiDriver().engine_type == "pi"


async def test_pi_driver_launch_stub_raises_until_phase_8() -> None:
    from ach_agent.engine.base.driver import EngineConfig

    with pytest.raises(NotImplementedError):
        await PiDriver().launch(EngineConfig(engine_type="pi"), "k1")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/pi/test_driver_stub.py -q`
Expected: FAIL — `ModuleNotFoundError: …engine.pi.driver`.

- [ ] **Step 3: Create the Pi package stub**

`src/ach_agent/engine/pi/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Pi engine driver (pi --mode rpc). Real implementation lands in SP1 Phase 8."""
```

`src/ach_agent/engine/pi/driver.py` (stub — replaced in Phase 8):

```python
# SPDX-License-Identifier: Apache-2.0
"""PiDriver stub — satisfies EngineDriver so engine.type='pi' selection type-checks.
The real launch/run_turn/etc. land in SP1 Phase 8."""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, MutableMapping

from ach_agent.engine.base.driver import EngineConfig, TurnResult

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

_TODO = "Pi engine lands in SP1 Phase 8"


class PiDriver:
    engine_type = "pi"

    def skills_dir(self, home: Path) -> Path:
        return home / "pi" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        raise NotImplementedError(_TODO)

    async def health(self, server: ManagedServer) -> bool:
        raise NotImplementedError(_TODO)

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
        raise NotImplementedError(_TODO)

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        raise NotImplementedError(_TODO)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        raise NotImplementedError(_TODO)

    async def stop(self, server: ManagedServer) -> None:
        raise NotImplementedError(_TODO)
```

- [ ] **Step 4: Boot driver selection + pool + EngineConfig in `main.py`**

At the engine-pool build site (`main.py:1372-1420`):

Change the imports (1372-1373):

```python
    from ach_agent.engine.base.driver import EngineConfig, EngineDriver
    from ach_agent.engine.base.pool import EnginePool
    from ach_agent.engine.opencode.driver import OpencodeDriver
```

Add two fields to the `EngineConfig(...)` construction (1390-1414) — inside the call, alongside the others:

```python
        engine_type=cfg.engine.type,
        binary_path=(
            cfg.engine.pi.binary_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else "opencode"
        ),
```

Select the driver just before building the pool, and pass it in (replace `pool = EnginePool(oc_sessions=session_store)` at 1420):

```python
    if cfg.engine.type == "pi":
        from ach_agent.engine.pi.driver import PiDriver

        driver: EngineDriver = PiDriver()
    else:
        driver = OpencodeDriver()
    pool = EnginePool(driver=driver, sessions_map=session_store)
```

- [ ] **Step 5: Thread `driver` into `_make_engine_runner`**

At the `_make_engine_runner(...)` call (1450), add `driver=driver`. In the `_make_engine_runner` **def** (`main.py:630`), add a `driver: EngineDriver` parameter (import `EngineDriver` for the annotation at the top of `main.py`, or under `TYPE_CHECKING`).

- [ ] **Step 6: Run selection tests + conformance**

Run: `uv run pytest tests/engine/pi/test_driver_stub.py -q && uv run mypy --strict src/ach_agent/engine/pi/ && make conformance`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/engine/pi/ tests/engine/pi/ src/ach_agent/main.py
git commit -m "feat(engine): select EngineDriver by engine.type at boot (+ PiDriver stub)"
```

---

### Task 6.2: `engine_runner` uses `run_contract_turn` + driver hygiene

- [ ] **Step 1: Swap the invocation imports (main.py:679)**

Replace:

```python
    from ach_agent.engine.lifecycle import compact_oc_session, discard_oc_session, run_invocation
```

with:

```python
    from ach_agent.engine.base.terminal import run_contract_turn
```

(`InvocationTimeout` and `build_session_stat` imports on the surrounding lines are unchanged.)

- [ ] **Step 2: Replace the `run_invocation(...)` call (main.py:816-829)**

```python
            turn_stats: dict[str, Any] = {}
            obj = await run_contract_turn(
                driver,
                server,
                conv_key=conv_key,
                prompt=full_prompt,
                reuse=reuse,
                sessions=pool.sessions,
                free_form=free_form,
                terminal_action=_terminal_action,
                terminal_retries=terminal_output_retries,
                on_text=on_text,
                on_tool=on_tool,
                max_tool_calls=max_tool_calls,
                stats=turn_stats,
            )
```

- [ ] **Step 3: Rekey post-turn hygiene on `session_ref` via the driver (main.py:850-881)**

Replace the hygiene block. Change `_oc_sid = turn_stats.get("oc_session_id", "")` to `_sid = turn_stats.get("session_ref", "")`, and route through the driver:

```python
            _sid = turn_stats.get("session_ref", "")
            if _sid and not reuse:
                # key='none' (or empty template render): stateless turn leaves no residue.
                await driver.discard_session(server, _sid)
            elif (
                _sid
                and session_cfg is not None
                and session_cfg.max_tokens is not None
                and getattr(_usage, "input_tokens", 0) > session_cfg.max_tokens
            ):
                if session_cfg.overflow == "compact":
                    log.info(
                        "session: maxTokens exceeded — compacting",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    await driver.compact_session(server, _sid)
                else:  # rotate: drop the map entry + delete the old session (clean)
                    log.info(
                        "session: maxTokens exceeded — rotating",
                        session_key=event.session_key,
                        session_ref=_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    pool.sessions.pop(conv_key, None)
                    await driver.discard_session(server, _sid)
```

(`_usage = turn_stats.get("usage")` a few lines above is unchanged; the stats-sink `build_session_stat(...)` call below reads `turn_stats["oc_session_id"]`, which `run_turn` still writes — leave it.)

- [ ] **Step 4: Full suite + type-check + conformance**

Run: `uv run pytest tests/ -q && uv run mypy --strict src/ach_agent/ && make conformance`
Expected: all PASS. Opencode paths are unchanged (driver defaults to opencode; `run_contract_turn(OpencodeDriver())` is exactly what `run_invocation` now delegates to, so integration/conformance behavior is identical).

If a test patched `ach_agent.main.run_invocation` or `main.discard_oc_session`/`compact_oc_session`, repoint it: patch `ach_agent.engine.base.terminal.run_contract_turn` or assert on the fake driver's `discard_session`/`compact_session` instead. Grep `grep -rn "run_invocation\|discard_oc_session\|compact_oc_session" tests/` and fix stragglers.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/main.py tests/
git commit -m "refactor(main): engine_runner drives via EngineDriver + run_contract_turn; hygiene on session_ref"
```

---

## Self-review (Phase 6)

- **Spec coverage:** §4.2 — `_make_engine_runner` selects the driver by `cfg.engine.type`; everything else in the closure (memory wiring, prompt build, terminal-action selection, session/reuse decision, stats) is unchanged; post-turn hygiene swaps its `discard_oc_session`/`compact_oc_session` for `driver.discard_session`/`driver.compact_session`, keyed on `session_ref`. All present.
- **Placeholders:** none — full stub, exact main.py edits with line anchors, explicit straggler grep.
- **Behavior preservation:** opencode is identical because `run_contract_turn(OpencodeDriver(), …)` is the same code `run_invocation` already delegates to (Phase 3). The rotate map-pop moves from `pool.oc_sessions.pop(conv_key)` to `pool.sessions.pop(conv_key)` — same map, now engine-namespaced-transparent.
- **Type consistency:** `run_contract_turn(driver, server, conv_key=…, sessions=pool.sessions, terminal_action=_terminal_action, stats=turn_stats)` matches the Phase 3 signature; `driver.discard_session(server, _sid)` / `driver.compact_session(server, _sid)` match the protocol.
