# Phase 7 — Host the a2a egress facade (shared prerequisite)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first. This is net-new **shared** work (spec §6): `build_a2a_mcp_server` is built-but-not-hosted today (VERIFICATION DEBT, `main.py:1341-1357`). Hosting it makes a2a egress work for **opencode** (verified first) so Pi inherits a proven facade in Phase 8.

**Goal:** Add `A2AEgressFacade` (mirrors `RepoCheckoutFacade.start()` — a uvicorn-served `FastMCP` on loopback), host it at boot when `manifest.a2a_agents` is non-empty, stop it on shutdown, and append its loopback URL to the per-invocation `mcp_servers` list for **both** engines.

**Exit criterion:** `A2AEgressFacade` start/stop unit test green; the facade URL appears in the opencode `mcp` block (so opencode discovers a2a tools); full suite + conformance green.

**Files:**
- Modify: `src/ach_agent/engine/a2a_egress.py` (add `A2AEgressFacade`)
- Modify: `src/ach_agent/main.py` — replace the VERIFICATION-DEBT block (~1341-1357); declare/stop the facade (near `repo_facade` at ~1224); thread `a2a_facade_url` into `_make_engine_runner` (def ~630, append site ~709, call ~1450)
- Create: `tests/engine/test_a2a_facade.py`

**Interfaces:**
- Consumes: `build_a2a_tools`, `build_a2a_mcp_server`, `ToolSpec` (existing, `engine/a2a_egress.py`); the `RepoCheckoutFacade.start()` hosting pattern (`engine/repo_facade.py:83-101`).
- Produces (consumed by Phases 6-runner + 8): `a2a_facade_url` closure var — a `http://127.0.0.1:<port>/mcp` loopback URL appended to `mcp_servers`, which `write_opencode_config` (opencode) and `pi/mcp_json.py` (Pi, Phase 8) turn into a **remote** MCP entry. `ek_` stays in the tool handlers (`build_a2a_tools(ek=ek)`), never in any config.

---

### Task 7.1: `A2AEgressFacade`

- [ ] **Step 1: Write the failing test**

Create `tests/engine/test_a2a_facade.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re
from typing import Any

import httpx

from ach_agent.engine.a2a_egress import A2AEgressFacade, ToolSpec


async def _noop(prompt: str) -> dict[str, Any]:
    return {"ok": True}


async def test_facade_starts_on_loopback_and_stops_clean() -> None:
    tools = [ToolSpec(name="a2a_peer", description="call peer", handler=_noop)]
    facade = A2AEgressFacade(tools)
    url = await facade.start()
    try:
        assert re.fullmatch(r"http://127\.0\.0\.1:\d+/mcp", url)
        # The streamable-http MCP endpoint is live (GET without a session → 4xx, not a refusal).
        async with httpx.AsyncClient() as c:
            resp = await c.get(url)
        assert resp.status_code < 500
    finally:
        await facade.stop()


async def test_facade_with_no_tools_still_starts() -> None:
    facade = A2AEgressFacade([])
    url = await facade.start()
    await facade.stop()
    assert url.endswith("/mcp")
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/test_a2a_facade.py -q`
Expected: FAIL — `A2AEgressFacade` does not exist.

- [ ] **Step 3: Implement `A2AEgressFacade` in `a2a_egress.py`**

Append to `src/ach_agent/engine/a2a_egress.py` (after `build_a2a_mcp_server`):

```python
class A2AEgressFacade:
    """Hosts the a2a-egress FastMCP server on an ephemeral loopback port (SP1 §6).

    Mirrors RepoCheckoutFacade: opencode / pi-mcp-adapter point at this loopback URL; the
    ek_ stays in the tool handlers (build_a2a_tools(ek=...)) and never reaches any engine
    config. Retires the "built but not hosted" VERIFICATION DEBT (main.py Plan 3/4)."""

    def __init__(self, tools: list[ToolSpec]) -> None:
        self._mcp = build_a2a_mcp_server(tools)
        self._server: Any = None
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> str:
        """Bind on an ephemeral localhost port; return the MCP URL."""
        import uvicorn

        config = uvicorn.Config(
            self._mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        for _ in range(250):
            if self._server.started:
                break
            if self._task.done():
                self._task.result()
                break
            await asyncio.sleep(0.02)
        if not self._server.started:
            raise RuntimeError("a2a egress facade failed to start within 5s")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info("a2a egress facade started", port=port)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the task (shutdown sweep)."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._server = None
        self._task = None
```

`Any` is already imported at the top of `a2a_egress.py` (`from typing import TYPE_CHECKING, Any, cast`); `asyncio` too.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/engine/test_a2a_facade.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/a2a_egress.py tests/engine/test_a2a_facade.py
git commit -m "feat(engine): host a2a egress FastMCP on loopback (A2AEgressFacade)"
```

---

### Task 7.2: Wire the facade into boot + `mcp_servers` (both engines)

- [ ] **Step 1: Declare the facade beside `repo_facade` (main.py:1224-1225)**

```python
    repo_facade: Any = None
    repo_facade_url: str | None = None
    a2a_facade: Any = None
    a2a_facade_url: str | None = None
```

- [ ] **Step 2: Replace the VERIFICATION-DEBT block (main.py:1341-1357)**

```python
        # A2A egress (Plan 3, completed in SP1): expose peer agents as harness-hosted MCP
        # tools on a loopback FastMCP so BOTH engines discover them. The ek_ stays in the
        # harness (peer auth header via A2AAgentClient); only the loopback URL is written into
        # engine config. RTR-06: a2a-sdk imports stay function-scoped; import the builder lazily.
        if manifest.a2a_agents:
            from ach_agent.engine.a2a_egress import A2AEgressFacade, build_a2a_tools

            a2a_tools = build_a2a_tools(manifest.a2a_agents, ek=ek)
            a2a_facade = A2AEgressFacade(a2a_tools)
            a2a_facade_url = await a2a_facade.start()
            log.info(
                "a2a egress facade started",
                agent_count=len(manifest.a2a_agents),
                tool_count=len(a2a_tools),
                url=a2a_facade_url,
            )
```

- [ ] **Step 3: Stop the facade on shutdown**

Find where `repo_facade` is stopped in the shutdown/`finally` path:

```bash
grep -n "repo_facade" src/ach_agent/main.py
```

Beside that `await repo_facade.stop()` (guarded by `if repo_facade is not None`), add:

```python
            if a2a_facade is not None:
                await a2a_facade.stop()
```

(If `repo_facade` is stopped inside a `try/finally` or a dedicated shutdown helper, add the a2a stop in the same place and guard identically.)

- [ ] **Step 4: Append `a2a_facade_url` to `mcp_servers` in `engine_runner`**

At the `repo_facade_url` append (main.py:709-710), add the a2a facade right after:

```python
        if repo_facade_url:
            mcp_servers = [*mcp_servers, repo_facade_url]
        # a2a egress facade (SP1 §6): a static localhost MCP server carried on every invocation,
        # so the agent can call peer agents. Same wiring as the memory/repo facades.
        if a2a_facade_url:
            mcp_servers = [*mcp_servers, a2a_facade_url]
```

- [ ] **Step 5: Thread `a2a_facade_url` into `_make_engine_runner`**

In the `_make_engine_runner` **def** (main.py:630), add `a2a_facade_url: str | None = None` (beside `repo_facade_url`). At the call site (main.py:1450), pass `a2a_facade_url=a2a_facade_url`.

- [ ] **Step 6: Full suite + type-check + conformance**

Run: `uv run pytest tests/ -q && uv run mypy --strict src/ach_agent/ && make conformance`
Expected: all PASS. The no-a2a-agents path (all current tests) is a no-op (`a2a_facade_url` stays `None`; `mcp_servers` unchanged). `tests/conformance/test_inv13_egress_via_mcp.py` still passes (egress remains model-initiated via MCP).

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/main.py
git commit -m "feat(main): host + wire a2a egress facade into mcp_servers for both engines"
```

---

## Self-review (Phase 7)

- **Spec coverage:** §6 "a2a facade hosting (shared prerequisite)" — the facade is hosted on a localhost port and added to the mcp block; **both engines** gain working a2a egress; verified against opencode first (the URL flows through `write_opencode_config`'s remote-MCP path). Retires the VERIFICATION DEBT. Done.
- **Placeholders:** none — full `A2AEgressFacade`, exact main.py edits, real start/stop test.
- **`ek_` hygiene:** `build_a2a_tools(ek=ek)` keeps the ek in the handlers; only the loopback URL is written to config — matches §8 and the memory/repo facades.
- **Type consistency:** `a2a_facade_url: str | None` mirrors `repo_facade_url`; the `mcp_servers` append pattern is identical; `A2AEgressFacade(tools)` matches `build_a2a_tools(...) -> list[ToolSpec]`.
