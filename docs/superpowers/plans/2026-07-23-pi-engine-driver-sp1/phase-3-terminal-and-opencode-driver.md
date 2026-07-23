# Phase 3 — Terminal contract carve-out + `OpencodeDriver`

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first. This phase touches **IP-adjacent code** (the terminal/repair loop). The strategy: implement the new engine-agnostic path, then **rewrite `run_invocation` to delegate to it** so the entire existing `test_lifecycle.py` suite exercises the new code — that is the regression gate (spec §4.3, §10).

**Goal:** Carve the harness-owned terminal contract (extract + ≤1 repair + step-budget wrap-up) out of `run_invocation` into `engine/base/terminal.py::run_contract_turn`, driven engine-agnostically off `TurnResult`. Add `engine/opencode/driver.py::OpencodeDriver` implementing `EngineDriver` (its `run_turn` returns a `TurnResult`; `launch`/`health`/`discard_session`/`compact_session`/`stop`/`skills_dir` delegate to the existing `lifecycle` functions). Repoint `run_invocation` to `run_contract_turn(OpencodeDriver(), …)`.

**Exit criterion:** `tests/engine/test_lifecycle.py` (all run_invocation/terminal/wrap-up/repair cases) + new driver/terminal unit tests + `make conformance` green.

**Files:**
- Create: `src/ach_agent/engine/opencode/driver.py`
- Create: `src/ach_agent/engine/base/terminal.py`
- Modify: `src/ach_agent/engine/lifecycle.py` — rewrite `run_invocation` (585-742) to delegate; remove `_terminal_object_hint` (573-582) after moving it; **keep** `consume_sse_after_send`, `_create_oc_session`, `discard_oc_session`, `compact_oc_session`, `launch`, `poll_ready` exactly as they are.
- Create: `tests/engine/test_opencode_driver.py`
- Create: `tests/engine/base/test_terminal.py`

**Interfaces:**
- Consumes: Phase 1 (`EngineConfig`, `TurnResult`, `EngineDriver`), Phase 2 (`engine/opencode/client.py`, `engine/opencode/events.py`).
- Produces (consumed by Phases 4, 6, 8): `OpencodeDriver`; `run_contract_turn(driver, server, *, conv_key, prompt, reuse, sessions, free_form, terminal_action, terminal_retries, on_text, on_tool, max_tool_calls, stats) -> dict`.

**Behavior-preservation notes (must match `run_invocation` exactly):**
1. `run_turn` calls `consume_sse_after_send` / `_create_oc_session` **through the `lifecycle` module namespace** (`import ach_agent.engine.lifecycle as oc; oc.consume_sse_after_send(...)`) so the ~15 `patch("ach_agent.engine.lifecycle.consume_sse_after_send")` sites in `test_lifecycle.py` still intercept it.
2. The **first** turn records into the caller's `stats`; the **wrap-up** and **repair** turns pass a throwaway `stats={}` so recorded usage/session reflect the first turn only (matches today — the old wrap-up/repair `consume_sse_after_send` calls passed no `stats`).
3. Wrap-up turn streams (`on_text`/`on_tool` forwarded); repair turn does **not** (both `None`) — matches `lifecycle.py:703-711` vs `738-740`.
4. `run_turn` writes both `stats["session_ref"]` and `stats["oc_session_id"]` (same value) so the stats sink + Phase 6 hygiene keep reading `oc_session_id`.

---

### Task 3.1: `engine/opencode/driver.py` — `OpencodeDriver`

- [ ] **Step 1: Write the failing test**

Create `tests/engine/test_opencode_driver.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.opencode.driver import OpencodeDriver


class _FakeClient:
    """Stands in for OpenCodeClient — isinstance() check in run_turn is bypassed via patch."""


class _FakeServer:
    def __init__(self) -> None:
        self._client = _FakeClient()

    def is_alive(self) -> bool:
        return True


@pytest.fixture(autouse=True)
def _accept_fake_client(monkeypatch: pytest.MonkeyPatch) -> None:
    # run_turn does `isinstance(client, OpenCodeClient)`; make the fake pass.
    import ach_agent.engine.opencode.driver as drv

    monkeypatch.setattr(drv, "OpenCodeClient", _FakeClient, raising=False)


async def test_run_turn_reuse_creates_and_records_session() -> None:
    sessions: dict[str, str] = {}
    stats: dict[str, Any] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session", return_value="ses_new") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="hello") as cs,
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(), conv_key="k1", prompt="p", reuse=True, sessions=sessions, stats=stats
        )
    assert result == TurnResult(text="hello", session_ref="ses_new", aborted=False)
    assert sessions["k1"] == "ses_new"
    assert stats["session_ref"] == "ses_new" and stats["oc_session_id"] == "ses_new"
    mk.assert_awaited_once()
    cs.assert_awaited_once()


async def test_run_turn_with_session_ref_bypasses_map() -> None:
    sessions: dict[str, str] = {}
    with (
        patch("ach_agent.engine.lifecycle._create_oc_session") as mk,
        patch("ach_agent.engine.lifecycle.consume_sse_after_send", return_value="wrapped"),
    ):
        result = await OpencodeDriver().run_turn(
            _FakeServer(), conv_key="k1", prompt="wrap", reuse=True, sessions=sessions,
            session_ref="ses_fixed", max_tool_calls=0, stats={},
        )
    assert result.session_ref == "ses_fixed"
    assert result.text == "wrapped"
    assert sessions == {}          # map never touched on the session_ref path
    mk.assert_not_awaited()        # no create on the continue path
```

Replace the `_create_oc_session`/`consume_sse_after_send` patches' `return_value` with `AsyncMock`-style if the harness needs it; both are `async def`, so `patch(..., return_value=X)` where the mock is awaited works with `unittest.mock`'s auto-async in 3.12. If not, use `new=AsyncMock(return_value=X)`.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/test_opencode_driver.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.opencode.driver'`.

- [ ] **Step 3: Implement `engine/opencode/driver.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""OpencodeDriver — the opencode implementation of EngineDriver (SP1 §4).

launch/health/discard/compact/stop delegate to the existing lifecycle helpers; run_turn is
the session-select + SSE-consume half of the old run_invocation, returning a TurnResult. The
terminal contract (extract/repair/wrap-up) lives once in engine/base/terminal.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, MutableMapping

import aiohttp

from ach_agent.engine.base.driver import EngineConfig, TurnResult
from ach_agent.engine.opencode.client import OpenCodeClient

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer


class OpencodeDriver:
    engine_type = "opencode"

    def skills_dir(self, home: Path) -> Path:
        # Opencode scans <home>/.config/opencode/skills (see engine/context.fetch_context).
        return home / ".config" / "opencode" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        import ach_agent.engine.lifecycle as oc
        from ach_agent.engine.opencode.client import find_free_port

        home = Path(cfg.home)
        home.mkdir(parents=True, exist_ok=True)
        port = find_free_port()
        server = await oc.launch(port, home, cfg, session_key)
        await oc.poll_ready(server, cfg.startup_timeout_seconds)
        return server

    async def health(self, server: ManagedServer) -> bool:
        client = server._client
        if isinstance(client, OpenCodeClient):
            try:
                return bool(await client.check_health())
            except Exception:  # noqa: BLE001
                return server.is_alive()
        return server.is_alive()

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
        import ach_agent.engine.lifecycle as oc  # namespace call keeps test patch targets valid

        client = server._client
        if not isinstance(client, OpenCodeClient):
            raise RuntimeError("ManagedServer has no client")
        stats = stats if stats is not None else {}

        async def _consume(oc_session_id: str) -> str:
            return await oc.consume_sse_after_send(
                client, oc_session_id, prompt,
                on_text=on_text, on_tool=on_tool, is_alive=server.is_alive,
                max_tool_calls=max_tool_calls, stats=stats,
            )

        # Repair/wrap-up: continue EXACTLY the given session; bypass the map + reuse (§4.3).
        if session_ref is not None:
            stats["session_ref"] = session_ref
            stats["oc_session_id"] = session_ref
            text = await _consume(session_ref)
            return TurnResult(text=text, session_ref=session_ref, aborted=bool(stats.get("aborted")))

        # First send: resolve conv_key → oc session id (create/reuse), 404-recreate retry.
        reused = False
        if reuse:
            cached = sessions.get(conv_key)
            if cached is None:
                oc_session_id = await oc._create_oc_session(client)
                sessions[conv_key] = oc_session_id
            else:
                oc_session_id, reused = cached, True
        else:
            oc_session_id = await oc._create_oc_session(client)
        stats["session_ref"] = oc_session_id
        stats["oc_session_id"] = oc_session_id
        try:
            text = await _consume(oc_session_id)
        except aiohttp.ClientResponseError as exc:
            if not (reused and exc.status == 404):
                raise
            oc_session_id = await oc._create_oc_session(client)
            sessions[conv_key] = oc_session_id
            stats["session_ref"] = oc_session_id
            stats["oc_session_id"] = oc_session_id
            text = await _consume(oc_session_id)
        return TurnResult(text=text, session_ref=oc_session_id, aborted=bool(stats.get("aborted")))

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        import ach_agent.engine.lifecycle as oc

        await oc.discard_oc_session(server, session_ref)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        import ach_agent.engine.lifecycle as oc

        await oc.compact_oc_session(server, session_ref)

    async def stop(self, server: ManagedServer) -> None:
        await server.stop()
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/engine/test_opencode_driver.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/opencode/driver.py tests/engine/test_opencode_driver.py
git commit -m "feat(engine): add OpencodeDriver (run_turn -> TurnResult, delegates to lifecycle)"
```

---

### Task 3.2: `engine/base/terminal.py` — engine-agnostic terminal contract

- [ ] **Step 1: Write the failing test**

Create `tests/engine/base/test_terminal.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.base.terminal import run_contract_turn


class _ScriptedDriver:
    """Returns queued TurnResults; records every run_turn call for assertions."""

    engine_type = "opencode"

    def __init__(self, results: list[TurnResult]) -> None:
        self._results = results
        self.calls: list[dict[str, Any]] = []

    async def run_turn(self, server: Any, **kw: Any) -> TurnResult:
        self.calls.append(kw)
        return self._results.pop(0)


async def test_happy_path_extracts_terminal_no_repair() -> None:
    drv = _ScriptedDriver([TurnResult(text='ok {"action":"none","text":"done"}', session_ref="ses_1")])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="none", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "none", "text": "done"}
    assert len(drv.calls) == 1  # no repair


async def test_free_form_returns_raw_text_no_extraction() -> None:
    drv = _ScriptedDriver([TurnResult(text="plain reply", session_ref="ses_1")])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=True, terminal_action="none", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "none", "text": "plain reply"}


async def test_aborted_runs_wrapup_on_same_session_ref() -> None:
    drv = _ScriptedDriver([
        TurnResult(text="partial, no terminal", session_ref="ses_9", aborted=True),
        TurnResult(text='{"action":"none","text":"wrapped"}', session_ref="ses_9"),
    ])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="none", terminal_retries=1, max_tool_calls=80, stats={},
    )
    assert obj == {"action": "none", "text": "wrapped"}
    assert drv.calls[1]["session_ref"] == "ses_9"      # wrap-up continued the SAME session
    assert drv.calls[1]["max_tool_calls"] == 0          # budget off on wrap-up


async def test_missing_terminal_triggers_one_repair() -> None:
    drv = _ScriptedDriver([
        TurnResult(text="no json here", session_ref="ses_2"),
        TurnResult(text='{"action":"a2a_reply","text":"fixed"}', session_ref="ses_2"),
    ])
    obj = await run_contract_turn(
        drv, object(), conv_key="k", prompt="p", reuse=True, sessions={},
        free_form=False, terminal_action="a2a_reply", terminal_retries=1, max_tool_calls=0, stats={},
    )
    assert obj == {"action": "a2a_reply", "text": "fixed"}
    assert drv.calls[1]["session_ref"] == "ses_2" and drv.calls[1]["on_text"] is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/base/test_terminal.py -q`
Expected: FAIL — `ModuleNotFoundError: …base.terminal`.

- [ ] **Step 3: Implement `engine/base/terminal.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Engine-agnostic terminal contract (SP1 §4.3): text-extract + Pydantic + <=1 repair, plus
the step-budget wrap-up turn. Runs ONCE for every engine (matches the "structured output is
harness-validated" constraint). free_form channels (--tui) skip extraction."""
from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from ach_agent.engine.base.driver import EngineDriver
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

log = structlog.get_logger(__name__)


def _terminal_object_hint(action: str) -> str:
    """The single terminal JSON object we ask the model to emit on a wrap/repair turn.

    a2a turns demand a2a_reply; async turns demand none. Showing only the ONE action the
    channel expects means an a2a repair turn never re-exposes 'none'."""
    if action == "a2a_reply":
        return '{"action":"a2a_reply","text":"..."}'
    return '{"action":"none","text":"..."}'


async def run_contract_turn(
    driver: EngineDriver,
    server: ManagedServer,
    *,
    conv_key: str,
    prompt: str,
    reuse: bool,
    sessions: MutableMapping[str, str],
    free_form: bool,
    terminal_action: str,
    terminal_retries: int,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    max_tool_calls: int = 0,
    stats: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from ach_agent.engine.validator import extract_terminal

    stats = stats if stats is not None else {}
    result = await driver.run_turn(
        server, conv_key=conv_key, prompt=prompt, reuse=reuse, sessions=sessions,
        on_text=on_text, on_tool=on_tool, max_tool_calls=max_tool_calls, stats=stats,
    )
    text = result.text

    if result.aborted:
        # Step-budget abort: the turn was cut mid-tool-loop and usually lacks a terminal object.
        # Run ONE wrap-up turn (budget OFF, SAME session) so the model emits a clean terminal
        # object. Throwaway stats so recorded usage/session reflect the first turn (matches old
        # run_invocation, which passed no stats to the wrap-up consume).
        log.warning("step-budget abort — running wrap-up turn", session_id=conv_key)
        wrap = (
            "You have reached your tool-call budget for this turn. Do NOT call any more tools. "
            "Reply now with ONLY the terminal JSON object "
            f"({_terminal_object_hint(terminal_action)}) "
            "summarizing what you found and did."
        )
        result = await driver.run_turn(
            server, conv_key=conv_key, prompt=wrap, reuse=reuse, sessions=sessions,
            session_ref=result.session_ref, on_text=on_text, on_tool=on_tool,
            max_tool_calls=0, stats={},
        )
        text = result.text

    # Free-form (--tui): no terminal contract — return the raw reply verbatim.
    if free_form:
        return {"action": "none", "text": text}

    obj = extract_terminal(text)
    if obj is None and terminal_retries > 0:
        repair = f"Reply with ONLY a terminal JSON object: {_terminal_object_hint(terminal_action)}."
        result = await driver.run_turn(
            server, conv_key=conv_key, prompt=repair, reuse=reuse, sessions=sessions,
            session_ref=result.session_ref, on_text=None, on_tool=None, max_tool_calls=0, stats={},
        )
        obj = extract_terminal(result.text)
        text = result.text
    return obj if obj is not None else {"action": "none", "text": text}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/engine/base/test_terminal.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/base/terminal.py tests/engine/base/test_terminal.py
git commit -m "feat(engine): carve terminal contract into base/terminal.run_contract_turn"
```

---

### Task 3.3: Repoint `run_invocation` to delegate (regression gate)

- [ ] **Step 1: Rewrite `run_invocation` in `lifecycle.py`**

Replace the whole body of `run_invocation` (585-742) — keep the signature identical — with a delegation. Also **delete** `_terminal_object_hint` (573-582) from `lifecycle.py` (now in `base/terminal.py`); if any test imports `lifecycle._terminal_object_hint`, add `from ach_agent.engine.base.terminal import _terminal_object_hint  # noqa: F401` to `lifecycle.py` instead of keeping the definition.

```python
async def run_invocation(
    server: ManagedServer,
    session_id: str,
    prompt: str,
    terminal_retries: int,
    terminal_action: str = "none",
    free_form: bool = False,
    on_text: Callable[[str], None] | None = None,
    on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
    reuse: bool = True,
    max_tool_calls: int = 0,
    stats: dict[str, Any] | None = None,
    oc_sessions: MutableMapping[str, str] | None = None,
) -> dict[str, Any]:
    """Back-compat opencode entrypoint — now a thin delegation to the engine-agnostic
    terminal contract (SP1 §4.3). Kept so existing callers/tests are unchanged; Phase 6
    switches engine_runner to call run_contract_turn directly."""
    from ach_agent.engine.base.terminal import run_contract_turn
    from ach_agent.engine.opencode.driver import OpencodeDriver

    sessions = oc_sessions if oc_sessions is not None else server._sessions
    return await run_contract_turn(
        OpencodeDriver(),
        server,
        conv_key=session_id,
        prompt=prompt,
        reuse=reuse,
        sessions=sessions,
        free_form=free_form,
        terminal_action=terminal_action,
        terminal_retries=terminal_retries,
        on_text=on_text,
        on_tool=on_tool,
        max_tool_calls=max_tool_calls,
        stats=stats,
    )
```

- [ ] **Step 2: Run the full opencode regression suite**

Run: `uv run pytest tests/engine/test_lifecycle.py -q`
Expected: PASS — every run_invocation / terminal / wrap-up / repair / 404-recreate case is green through the new path. If a wrap-up/repair test asserts recorded usage came only from the first turn, note 3 confirms the throwaway `stats={}` preserves that.

- [ ] **Step 3: Full suite + type-check + conformance**

Run: `uv run pytest tests/ -q && uv run mypy --strict src/ach_agent/engine/ && make conformance`
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py
git commit -m "refactor(engine): run_invocation delegates to run_contract_turn(OpencodeDriver)"
```

---

## Self-review (Phase 3)

- **Spec coverage:** §4.3 Fine boundary — `run_turn` returns raw `TurnResult`; the terminal loop lives once in `base/terminal.py`; repair **and** step-budget wrap-up target the SAME `session_ref` even when `reuse=False`; `free_form` skips extraction. All present. Review-corrected wrap-up (driven off `TurnResult.aborted`, engine-agnostic) is honored.
- **Placeholders:** none — full code for `driver.py`, `terminal.py`, and the `run_invocation` delegation; real unit tests for both new modules.
- **Behavior preservation:** the 4 numbered notes lock the exact matches (namespace-call for patch targets, throwaway stats on wrap-up/repair, stream-on-wrap/silent-on-repair, dual `session_ref`/`oc_session_id`). The existing `test_lifecycle.py` suite is the gate.
- **Type consistency:** `run_contract_turn` and `OpencodeDriver.run_turn` signatures match the index contract and the Phase 6 call site (`sessions=`, `terminal_action=`, `stats=`).
