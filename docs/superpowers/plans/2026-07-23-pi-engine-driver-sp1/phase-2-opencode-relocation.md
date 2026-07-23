# Phase 2 — Opencode file relocation (behavior-preserving)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first. This phase does **physical file moves guarded by re-export shims and the existing test suite** — the safety net is that every test stays green with zero behavior change.

**Goal:** Relocate `client.py` → `engine/opencode/client.py`, and split `events.py` into the shared vocabulary (`engine/base/events.py`) + the opencode SSE parser (`engine/opencode/events.py`). Keep `engine/client.py` and `engine/events.py` as re-export shims so the ~600 existing import sites are untouched. Update only the **location-sensitive** `patch()`/`import` sites (module-level functions and `import`ed submodules — class-method patches survive a shim automatically).

**Exit criterion:** `uv run pytest tests/ -q` and `make conformance` are green with no source-behavior change.

**Why shims are mandatory:** `test_lifecycle.py` alone has ~40 `engine.lifecycle` references and ~15 `patch("ach_agent.engine.lifecycle.…")` sites; `engine.client`/`engine.events` are imported across `tests/conformance`, `tests/engine`, `tests/router`, `tests/stats`, and `main.py`. A big-bang import rename would break all of them. **Rule of thumb:** `patch("mod.SomeClass.method")` keeps working through a shim (same class object); `patch("mod.some_function")` and `patch("mod.submodule.x")` bind to the *module namespace* and must point at the symbol's new **definition** module.

**Files:**
- Create: `src/ach_agent/engine/opencode/__init__.py`, `src/ach_agent/engine/opencode/client.py`, `src/ach_agent/engine/opencode/events.py`
- Create: `src/ach_agent/engine/base/events.py`
- Replace with shims: `src/ach_agent/engine/client.py`, `src/ach_agent/engine/events.py`
- Modify (patch targets only): `tests/engine/test_client.py`, `tests/engine/test_pool.py`
- Modify (import repoint): `src/ach_agent/engine/lifecycle.py` (imports from `engine.events`/`engine.client` stay via shim — no change required unless mypy complains about the split; see Task 2.2 Step 4)

**Interfaces:**
- Produces: `engine/base/events.py` exports the SHARED vocab `OpenCodeToolUpdate`, `ToolStateRunning`, `ToolStateCompleted`, `ToolStateError`, `ToolState`, `OpenCodeUsage`, `EngineError`, `InvocationTimeout` — the types Pi's `engine/pi/events.py` (Phase 8) imports. `engine/opencode/events.py` exports the opencode SSE dataclasses + `ReplyAccumulator` + `_SendFailed` + the parser. `engine/opencode/client.py` exports `OpenCodeClient`, `find_free_port`, `release_port`, `_reserved_ports`.
- Consumes: Phase 1 (`engine/base/` package exists).

---

### Task 2.1: Move `client.py` → `engine/opencode/client.py`

- [ ] **Step 1: Create the `opencode` package**

Create `src/ach_agent/engine/opencode/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Opencode engine driver: HTTP/SSE client, SSE event parser, EngineDriver impl."""
```

- [ ] **Step 2: Physically move the file (preserve history)**

Run:

```bash
git mv src/ach_agent/engine/client.py src/ach_agent/engine/opencode/client.py
```

Inside `opencode/client.py`, repoint its internal import of the event types. It currently does `from ach_agent.engine.events import OpenCodeEvent, …` (client.py:27). Change that to import from the new locations once Task 2.2 lands; **for now** leave it as `from ach_agent.engine.events import …` (the events shim from Task 2.2 keeps it valid). No other edits to the file body.

- [ ] **Step 3: Write the shim `engine/client.py`**

Create a new `src/ach_agent/engine/client.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: OpenCodeClient + port helpers moved to engine/opencode/client.py (SP1).

Re-exported here so existing `from ach_agent.engine.client import …` sites keep resolving.
NOTE: module-level function/submodule `patch()` targets must use the opencode.client path.
"""
from ach_agent.engine.opencode.client import (  # noqa: F401
    OpenCodeClient,
    find_free_port,
    release_port,
    _reserved_ports,
)
```

(Include any other public name the current `client.py` exports — verify with `grep -nE "^def |^class |^async def |^_reserved_ports" src/ach_agent/engine/opencode/client.py` and add each to the shim.)

- [ ] **Step 4: Update the two location-sensitive test sites**

`tests/engine/test_client.py:389` patches the `socket` module *inside* `client`:

```python
# BEFORE: with patch("ach_agent.engine.client.socket.socket", FakeSocket):
# AFTER:
with patch("ach_agent.engine.opencode.client.socket.socket", FakeSocket):
```

`tests/engine/test_pool.py:316` patches the module-level `find_free_port`:

```python
# BEFORE: monkeypatch.setattr("ach_agent.engine.client.find_free_port", 12345)
# AFTER:
monkeypatch.setattr("ach_agent.engine.opencode.client.find_free_port", 12345)
```

Leave every `from ach_agent.engine.client import OpenCodeClient` and `patch("ach_agent.engine.client.OpenCodeClient.check_health")` **unchanged** — class-attribute patches resolve to the same class object through the shim.

- [ ] **Step 5: Run the client + pool + conformance tests**

Run: `uv run pytest tests/engine/test_client.py tests/engine/test_pool.py tests/conformance/test_inv06_startup_deadline.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add -A src/ach_agent/engine/ tests/engine/test_client.py tests/engine/test_pool.py
git commit -m "refactor(engine): relocate client.py to engine/opencode/client.py (shim kept)"
```

---

### Task 2.2: Split `events.py` → `base/events.py` (shared) + `opencode/events.py` (parser)

- [ ] **Step 1: Create `engine/base/events.py` with the SHARED vocab**

Move these definitions **verbatim** out of `engine/events.py` into a new `src/ach_agent/engine/base/events.py` (with the SPDX header + `from __future__ import annotations` + `from dataclasses import dataclass` + `from typing import Any`):

- `ToolStateRunning`, `ToolStateCompleted`, `ToolStateError`, and the `ToolState = … | … | …` union (events.py:104-126)
- `OpenCodeToolUpdate` (events.py:129-144)
- `OpenCodeUsage` (events.py:159-175)
- `EngineError` and `InvocationTimeout` (search `class EngineError`, `class InvocationTimeout` — they are engine-agnostic error/timeout types the router and `main.py` import)

Docstring at top:

```python
"""Shared engine-event vocabulary (SP1 §9).

The tool-update / usage / error types BOTH drivers produce into. opencode's SSE parser
(engine/opencode/events.py) and Pi's JSONL mapper (engine/pi/events.py) construct these,
so the harness's on_tool sink and stats mapping stay identical across engines. Field names
keep the OpenCode* prefix (surgical — renaming ripples through channels + the debug console);
Pi fills them best-effort (§5.3)."""
```

- [ ] **Step 2: Move the opencode SSE-specific code to `engine/opencode/events.py`**

Run:

```bash
git mv src/ach_agent/engine/events.py src/ach_agent/engine/opencode/events.py
```

In `opencode/events.py`, **delete** the definitions that moved to `base/events.py` (Step 1) and add, near the top:

```python
from ach_agent.engine.base.events import (
    EngineError,
    InvocationTimeout,
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)
```

Everything opencode-specific stays: `OpenCodeTextUpdate`, `OpenCodeUserMessage`, `OpenCodeStreamReady`, `OpenCodeSessionIdle`, `OpenCodeSessionError`, the `OpenCodeEvent` union, `ReplyAccumulator`, `_SendFailed`, `_parse_tool_state`, `parse_event`, and the reader helpers — all unchanged.

- [ ] **Step 3: Write the shim `engine/events.py`**

Create a new `src/ach_agent/engine/events.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: event vocab split into engine/base/events.py (shared) and
engine/opencode/events.py (opencode SSE parser) in SP1. Re-exported so existing
`from ach_agent.engine.events import …` sites keep resolving."""
from ach_agent.engine.base.events import (  # noqa: F401
    EngineError,
    InvocationTimeout,
    OpenCodeToolUpdate,
    OpenCodeUsage,
    ToolState,
    ToolStateCompleted,
    ToolStateError,
    ToolStateRunning,
)
from ach_agent.engine.opencode.events import (  # noqa: F401
    OpenCodeEvent,
    OpenCodeSessionError,
    OpenCodeSessionIdle,
    OpenCodeStreamReady,
    OpenCodeTextUpdate,
    OpenCodeUserMessage,
    ReplyAccumulator,
    _SendFailed,
    parse_event,
)
```

(Verify the exported-name list against `grep -nE "^class |^def |^_SendFailed|^ReplyAccumulator|^OpenCodeEvent" src/ach_agent/engine/opencode/events.py` and add any public name a test imports — e.g. `_await_subscription_ready` is in `lifecycle.py`, not here, so it stays out.)

- [ ] **Step 4: Repoint `opencode/client.py`'s internal import**

Now that the parser lives in `opencode/events.py`, change `opencode/client.py`'s `from ach_agent.engine.events import …` to `from ach_agent.engine.opencode.events import …` (same names). This keeps the opencode subpackage self-referential and lets mypy see the real module. (Alternatively leave it pointing at the shim — both resolve; the direct path is cleaner.)

- [ ] **Step 5: Repoint any location-sensitive event patch sites**

Run the suite (Step 6). For **module-level function** patches that now miss (`patch("ach_agent.engine.events.parse_event")` or a `patch("ach_agent.engine.events.<func>")` — grep `patch("ach_agent.engine.events`), repoint to `ach_agent.engine.opencode.events.<func>`. Plain `from ach_agent.engine.events import <Type>` and dataclass/`isinstance` uses need **no** change (resolved via the shim). `main.py:678` (`from ach_agent.engine.events import InvocationTimeout`) and `main.py:39` (`OpenCodeToolUpdate`) resolve through the shim — leave them, or repoint to `engine.base.events` for cleanliness (optional).

- [ ] **Step 6: Full suite + type-check + conformance**

Run: `uv run pytest tests/ -q`
Expected: PASS. If a `patch()` misses, repoint it per Step 5 (the failure names the exact target).

Run: `uv run mypy --strict src/ach_agent/engine/`
Expected: no errors.

Run: `make conformance`
Expected: all PASS.

- [ ] **Step 7: Commit**

```bash
git add -A src/ach_agent/engine/ tests/
git commit -m "refactor(engine): split events.py into base/events.py + opencode/events.py (shims kept)"
```

---

## Self-review (Phase 2)

- **Spec coverage:** §4.1 places `client.py` and `events.py` under `opencode/`, and the "same event shape" both drivers target under a shared home (`base/events.py`). Done.
- **Placeholders:** none. Verbatim moves are specified by exact symbol lists + source line ranges; the shims are complete; the two known location-sensitive test edits are given in full, and Step 5/6 make the "repoint stragglers" loop explicit and self-verifying (the suite names any missed target).
- **Behavior:** zero source-behavior change — this is pure relocation. The existing tests are the regression gate; conformance must stay green.
- **Risk noted:** `patch()` location sensitivity is the one hazard; the class-method-vs-module-function rule and the explicit straggler loop bound it.
