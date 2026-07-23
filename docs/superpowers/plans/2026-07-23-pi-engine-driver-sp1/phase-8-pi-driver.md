# Phase 8 — The Pi driver (`engine/pi/`)

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development` or `superpowers:executing-plans`. Read [index.md](index.md) first. This phase replaces the Phase-6 `PiDriver` stub with the real engine and reaches the SP1 exit criterion: **egress parity with opencode.**

**Goal:** Implement `engine/pi/`: the JSONL RPC transport, the Pi-event → shared-vocab mapper, the `models.json` / `mcp.json` / `settings.json` builders, the clean-slate `build_pi_env`, and `PiDriver` (launch, run_turn with durable sessions + bounds/abort, health, discard/compact, stop). MCP egress rides one vendored **pi-mcp-adapter** `mcp.json` carrying memory / repo / a2a facades + proxied external MCP + passthrough + codemem.

**Exit criterion:** Pi driver units (against a fake `pi` subprocess replaying JSONL fixtures) + `models.json`/`mcp.json` generation tests + one real-`pi` e2e (with `ek_`-hygiene assertions) green. `make lint` + full suite + `make conformance` green.

**External-schema note (read once):** Pi's RPC command/event field names live in `engine/pi/protocol.py` as constants. Unit tests build fixtures from those same constants, so they validate **logic**, not brittle literals. If the real-`pi` e2e (Task 8.7) reveals a different field name, fix it once in `protocol.py`. Sources: Pi docs `packages/coding-agent/docs/{rpc,json,skills,models,custom-provider,extensions,settings}.md` (spec §13); pi-mcp-adapter README.

**Files (all new unless noted):**
- `src/ach_agent/engine/pi/protocol.py` — wire constants
- `src/ach_agent/engine/pi/rpc.py` — `PiRpcClient`
- `src/ach_agent/engine/pi/events.py` — event mapping
- `src/ach_agent/engine/pi/models_json.py` — `build_models_json`
- `src/ach_agent/engine/pi/mcp_json.py` — `build_mcp_json`
- `src/ach_agent/engine/pi/config.py` — `build_pi_settings`, `build_pi_env`
- `src/ach_agent/engine/pi/driver.py` — **replace** the Phase-6 stub with the real `PiDriver`
- Tests under `tests/engine/pi/` + `tests/e2e/test_pi_e2e.py`
- `docs/references/2026-07-DD-pi-engine-driver.md` + a row in `docs/references/README.md`

**Interfaces:**
- Consumes: Phases 1 (`EngineConfig`, `TurnResult`), 2 (`base/events` vocab: `OpenCodeToolUpdate`, `ToolState*`, `OpenCodeUsage`), 3 (`ManagedServer` reused as the transport handle), 5 (`cfg.engine.pi`), 7 (a2a facade URL in `cfg.mcp_servers`).
- Produces: a fully egress-capable `PiDriver` selected by `engine.type: pi` (Phase 6 wiring).

---

### Task 8.1: `pi/protocol.py` + `pi/rpc.py` (JSONL transport)

- [ ] **Step 1: Write the failing test**

Create `tests/engine/pi/test_rpc.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ach_agent.engine.pi.rpc import PiRpcClient, PiRpcError


class _FakeStdout:
    """Feeds pre-baked bytes, then EOF, mimicking asyncio.StreamReader.read(n)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, n: int) -> bytes:
        if self._chunks:
            return self._chunks.pop(0)
        return b""  # EOF


class _FakeStdin:
    def __init__(self) -> None:
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


class _FakeProc:
    def __init__(self, stdout: _FakeStdout) -> None:
        self.stdout = stdout
        self.stdin = _FakeStdin()


async def test_recv_parses_lf_framed_json_across_chunk_boundaries() -> None:
    # A JSON object split across two reads, plus a trailing \r that must be stripped (not split).
    proc = _FakeProc(_FakeStdout([b'{"type":"a","x":', b'1}\r\n{"type":"b"}\n']))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    assert await client.recv() == {"type": "a", "x": 1}
    assert await client.recv() == {"type": "b"}
    eof = await client.recv()
    assert eof["type"] == "__eof__"
    await client.close()


async def test_send_writes_one_lf_terminated_line() -> None:
    proc = _FakeProc(_FakeStdout([]))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    await client.send({"type": "prompt", "message": "hi"})
    assert proc.stdin.written == [b'{"type":"prompt","message":"hi"}\n']
    await client.close()


async def test_invalid_json_line_raises() -> None:
    proc = _FakeProc(_FakeStdout([b"not json\n"]))
    client = PiRpcClient(proc)  # type: ignore[arg-type]
    with pytest.raises(PiRpcError):
        await client.recv()
    await client.close()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/pi/test_rpc.py -q`
Expected: FAIL — `…pi.rpc` missing.

- [ ] **Step 3: Implement `pi/protocol.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Pi --mode rpc wire vocabulary (packages/coding-agent/docs/rpc.md + json.md).

Isolated here so a real-pi mismatch is a one-line fix and unit-test fixtures stay in sync."""
from __future__ import annotations

# Commands (harness → pi, over stdin)
CMD_PROMPT = "prompt"
CMD_ABORT = "abort"
CMD_NEW_SESSION = "new_session"
CMD_SWITCH_SESSION = "switch_session"

# Events (pi → harness, over stdout)
EV_MESSAGE_UPDATE = "message_update"          # wraps assistantMessageEvent (text_delta lives nested)
EV_ASSISTANT_INNER = "assistantMessageEvent"  # key inside message_update
EV_INNER_TEXT_DELTA = "text_delta"            # inner assistantMessageEvent.type carrying text
EV_TOOL_START = "tool_execution_start"
EV_TOOL_END = "tool_execution_end"
EV_AGENT_SETTLED = "agent_settled"            # the SAFE terminal event (NOT agent_end)
EV_SESSION_CREATED = "session_created"        # carries the new session-file path
EV_EOF = "__eof__"                            # synthetic: pi stdout closed

# Field names (best-effort camelCase per docs; confirmed by the e2e)
F_SESSION_PATH = "sessionPath"
F_TEXT = "text"
F_TOOL_NAME = "toolName"
F_CALL_ID = "callId"
F_INPUT = "input"
F_OUTPUT = "output"
F_ERROR = "error"
F_TITLE = "title"
```

- [ ] **Step 4: Implement `pi/rpc.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""JSONL RPC over a `pi --mode rpc` subprocess's stdin/stdout (SP1 §5.2).

Strict LF framing: split ONLY on the b"\\n" byte and strip a trailing b"\\r" — never a
text-mode readline (which also splits on U+2028/U+2029 and lone \\r). A background reader task
parses stdout lines into an asyncio.Queue so per-turn consumers can drain across turns."""
from __future__ import annotations

import asyncio
import contextlib
import json
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.engine.pi.protocol import EV_EOF

if TYPE_CHECKING:
    from asyncio.subprocess import Process

log = structlog.get_logger(__name__)


class PiRpcError(Exception):
    """Malformed JSONL from pi, or the process ended mid-turn."""


class PiRpcClient:
    def __init__(self, proc: Process) -> None:
        self._proc = proc
        self._q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read_loop())

    async def send(self, cmd: dict[str, Any]) -> None:
        stdin = self._proc.stdin
        if stdin is None:
            raise PiRpcError("pi stdin is closed")
        stdin.write((json.dumps(cmd, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))
        await stdin.drain()

    async def recv(self) -> dict[str, Any]:
        item = await self._q.get()
        if item.get("type") == EV_EOF:
            return item
        if "__error__" in item:
            raise PiRpcError(str(item["__error__"]))
        return item

    async def _read_loop(self) -> None:
        stdout = self._proc.stdout
        assert stdout is not None
        buf = b""
        while True:
            chunk = await stdout.read(65536)
            if not chunk:
                if buf.strip():
                    await self._emit(buf)
                await self._q.put({"type": EV_EOF})
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                line = line.rstrip(b"\r")  # strip trailing CR only; do NOT split on it
                if line.strip():
                    await self._emit(line)

    async def _emit(self, raw: bytes) -> None:
        try:
            obj = json.loads(raw.decode("utf-8"))
            if not isinstance(obj, dict):
                raise ValueError("event is not a JSON object")
        except (ValueError, UnicodeDecodeError) as exc:
            await self._q.put({"__error__": f"invalid JSONL from pi: {raw!r} ({exc})"})
            return
        await self._q.put(obj)

    async def close(self) -> None:
        self._reader.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self._reader
        stdin = self._proc.stdin
        if stdin is not None:
            with contextlib.suppress(Exception):
                stdin.close()
```

- [ ] **Step 5: Run + commit**

Run: `uv run pytest tests/engine/pi/test_rpc.py -q` → PASS.

```bash
git add src/ach_agent/engine/pi/protocol.py src/ach_agent/engine/pi/rpc.py tests/engine/pi/test_rpc.py
git commit -m "feat(pi): JSONL RPC client (strict LF framing) + wire protocol constants"
```

---

### Task 8.2: `pi/events.py` (Pi event → shared vocab)

- [ ] **Step 1: Write the failing test**

Create `tests/engine/pi/test_events.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from ach_agent.engine.base.events import OpenCodeToolUpdate, ToolStateCompleted, ToolStateRunning
from ach_agent.engine.pi import events as pe
from ach_agent.engine.pi.protocol import (
    EV_AGENT_SETTLED, EV_ASSISTANT_INNER, EV_INNER_TEXT_DELTA, EV_MESSAGE_UPDATE,
    EV_TOOL_END, EV_TOOL_START,
)


def test_text_delta_unwraps_nested_assistant_message_event() -> None:
    ev = {"type": EV_MESSAGE_UPDATE, EV_ASSISTANT_INNER: {"type": EV_INNER_TEXT_DELTA, "text": "hi"}}
    assert pe.pi_text_delta(ev) == "hi"


def test_non_text_message_update_returns_none() -> None:
    ev = {"type": EV_MESSAGE_UPDATE, EV_ASSISTANT_INNER: {"type": "reasoning_delta", "text": "x"}}
    assert pe.pi_text_delta(ev) is None


def test_tool_start_maps_to_running_update() -> None:
    ev = {"type": EV_TOOL_START, "toolName": "gitlab_mr", "callId": "c1", "input": {"a": 1}}
    tu = pe.pi_tool_update(ev, "ses_1")
    assert isinstance(tu, OpenCodeToolUpdate)
    assert tu.tool_name == "gitlab_mr" and tu.call_id == "c1"
    assert isinstance(tu.state, ToolStateRunning) and tu.state.status == "running"


def test_tool_end_maps_to_completed_update() -> None:
    ev = {"type": EV_TOOL_END, "toolName": "gitlab_mr", "callId": "c1", "output": "done"}
    tu = pe.pi_tool_update(ev, "ses_1")
    assert isinstance(tu.state, ToolStateCompleted) and tu.state.output == "done"


def test_is_settled() -> None:
    assert pe.is_settled({"type": EV_AGENT_SETTLED}) is True
    assert pe.is_settled({"type": "agent_end"}) is False   # NOT terminal — retries may follow
```

- [ ] **Step 2: Run → FAIL** (`…pi.events` missing).

- [ ] **Step 3: Implement `pi/events.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Map Pi JSONL events onto the shared engine vocab (SP1 §5.3, §9) — same on_tool/usage sink
shape as the opencode SSE path, so stats + the debug console stay engine-agnostic."""
from __future__ import annotations

from typing import Any

from ach_agent.engine.base.events import (
    OpenCodeToolUpdate, OpenCodeUsage, ToolState, ToolStateCompleted, ToolStateError, ToolStateRunning,
)
from ach_agent.engine.pi.protocol import (
    EV_AGENT_SETTLED, EV_ASSISTANT_INNER, EV_INNER_TEXT_DELTA, EV_MESSAGE_UPDATE, EV_TOOL_END,
    EV_TOOL_START, F_CALL_ID, F_ERROR, F_INPUT, F_OUTPUT, F_TEXT, F_TITLE, F_TOOL_NAME,
)


def pi_text_delta(ev: dict[str, Any]) -> str | None:
    """Return the streamed text delta, unwrapping message_update → assistantMessageEvent."""
    if ev.get("type") != EV_MESSAGE_UPDATE:
        return None
    inner = ev.get(EV_ASSISTANT_INNER) or {}
    if isinstance(inner, dict) and inner.get("type") == EV_INNER_TEXT_DELTA:
        text = inner.get(F_TEXT, "")
        return str(text) if text else None
    return None


def pi_tool_update(ev: dict[str, Any], session_ref: str) -> OpenCodeToolUpdate | None:
    """Map tool_execution_start/end → the shared OpenCodeToolUpdate (opencode-shaped fields
    filled best-effort; message_id is empty for Pi)."""
    kind = ev.get("type")
    if kind not in (EV_TOOL_START, EV_TOOL_END):
        return None
    call_id = str(ev.get(F_CALL_ID, "") or "")
    tool_name = str(ev.get(F_TOOL_NAME, "") or "")
    state: ToolState
    if kind == EV_TOOL_START:
        state = ToolStateRunning(input=ev.get(F_INPUT), title=str(ev.get(F_TITLE, "")))
    elif ev.get(F_ERROR):
        state = ToolStateError(error=str(ev.get(F_ERROR)), input=ev.get(F_INPUT))
    else:
        state = ToolStateCompleted(output=str(ev.get(F_OUTPUT, "")), input=ev.get(F_INPUT))
    return OpenCodeToolUpdate(
        session_id=session_ref, part_id=call_id, message_id="",
        tool_name=tool_name, call_id=call_id, state=state,
    )


def pi_usage(ev: dict[str, Any], session_ref: str) -> OpenCodeUsage | None:
    """Map a per-message usage event → OpenCodeUsage (same sink as opencode). Returns None if
    the event carries no usage block."""
    usage = ev.get("usage")
    if not isinstance(usage, dict):
        return None
    return OpenCodeUsage(
        session_id=session_ref,
        message_id=str(ev.get("messageId", "")),
        input_tokens=int(usage.get("inputTokens", 0) or 0),
        output_tokens=int(usage.get("outputTokens", 0) or 0),
        cache_read=int(usage.get("cacheReadTokens", 0) or 0),
        cache_write=int(usage.get("cacheWriteTokens", 0) or 0),
        cost=float(usage.get("costUsd", 0.0) or 0.0),
        duration_ms=int(usage.get("durationMs", 0) or 0),
    )


def is_settled(ev: dict[str, Any]) -> bool:
    return ev.get("type") == EV_AGENT_SETTLED
```

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add src/ach_agent/engine/pi/events.py tests/engine/pi/test_events.py
git commit -m "feat(pi): map Pi events onto the shared engine vocab (text/tool/usage/settle)"
```

---

### Task 8.3: `pi/models_json.py`

- [ ] **Step 1: Write the failing test** — `tests/engine/pi/test_models_json.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.models_json import build_models_json


def test_provider_api_mapping_and_no_ek(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    cfg = EngineConfig(model="gemini-flash-latest", model_type="gemini",
                       model_base_url="http://127.0.0.1:9001/gemini/v1beta")
    doc, provider = build_models_json(cfg)
    blob = json.dumps(doc)
    assert "ek_secret" not in blob                    # ek NEVER in models.json
    assert doc[provider]["api"] == "google-generative-ai"
    assert doc[provider]["baseUrl"] == "http://127.0.0.1:9001/gemini/v1beta"
    assert doc[provider]["apiKey"] == "local-proxy"   # dummy placeholder
    assert doc[provider]["headers"] == {}             # proxy injects ek harness-side


def test_openai_and_anthropic_api_kinds() -> None:
    doc_o, p_o = build_models_json(EngineConfig(model_type="openai", model_base_url="http://x/v1"))
    doc_a, p_a = build_models_json(EngineConfig(model_type="anthropic", model_base_url="http://x/anthropic"))
    assert doc_o[p_o]["api"] == "openai-completions"
    assert doc_a[p_a]["api"] == "anthropic-messages"
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement `pi/models_json.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Build Pi's models.json (SP1 §5.1). Points baseUrl at the localhost model proxy (which
injects the ek_); apiKey is a dummy and headers are empty — the ek_ is NEVER written here.
Reuses EngineConfig.model_base_url verbatim (already `{proxy}/{prefix}` per model.type)."""
from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import EngineConfig

# model.type → (Pi provider name, Pi api kind). api kinds per Pi custom-provider.md.
_PI_PROVIDER_BY_TYPE: dict[str, tuple[str, str]] = {
    "openai": ("ach-openai", "openai-completions"),
    "gemini": ("ach-gemini", "google-generative-ai"),
    "anthropic": ("ach-anthropic", "anthropic-messages"),
}


def build_models_json(cfg: EngineConfig) -> tuple[dict[str, Any], str]:
    """Return (models.json dict, provider_name). Provider name is passed to `pi --provider`."""
    provider, api = _PI_PROVIDER_BY_TYPE.get(cfg.model_type, _PI_PROVIDER_BY_TYPE["openai"])
    doc: dict[str, Any] = {
        provider: {
            "api": api,
            "baseUrl": cfg.model_base_url,  # localhost proxy; already carries the right prefix
            "apiKey": "local-proxy",         # dummy — proxy injects the real ek_
            "headers": {},                   # NO ek_ (SEC-01 / §8)
        }
    }
    return doc, provider
```

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add src/ach_agent/engine/pi/models_json.py tests/engine/pi/test_models_json.py
git commit -m "feat(pi): build models.json (provider/api mapping, dummy key, no ek)"
```

---

### Task 8.4: `pi/mcp_json.py`

- [ ] **Step 1: Write the failing test** — `tests/engine/pi/test_mcp_json.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import json

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.mcp_json import build_mcp_json


def test_facades_and_proxy_are_remote_loopback_entries(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    cfg = EngineConfig(
        mcp_servers=["http://127.0.0.1:7001/mcp", "http://127.0.0.1:7002/mcp"],  # memory + a2a facades
        mcp_local_urls={"gitlab": "http://127.0.0.1:7003/mcp/gitlab"},           # proxied external
        exclude_tools=["dangerous_tool"],
    )
    doc = build_mcp_json(cfg)
    blob = json.dumps(doc)
    assert "ek_secret" not in blob                                  # ek NEVER in mcp.json
    servers = doc["mcpServers"]
    assert servers["facade-0"] == {"url": "http://127.0.0.1:7001/mcp"}
    assert servers["gitlab"] == {"url": "http://127.0.0.1:7003/mcp/gitlab"}
    assert doc["settings"]["directTools"] is True
    assert doc["settings"]["excludeTools"] == ["dangerous_tool"]
    assert doc["settings"]["sampling"] is False and doc["settings"]["elicitation"] is False


def test_codemem_is_local_stdio() -> None:
    cfg = EngineConfig(codemem_db_path="/data/mem.db", codemem_project="proj")
    servers = build_mcp_json(cfg)["mcpServers"]
    assert servers["codemem"]["command"] == "codemem"
    assert servers["codemem"]["args"] == ["mcp", "--db-path", "/data/mem.db"]
    assert servers["codemem"]["env"]["CODEMEM_PROJECT"] == "proj"


def test_passthrough_opencode_local_entry_converted_to_pi_shape() -> None:
    cfg = EngineConfig(extra_mcp_servers={
        "fs": {"type": "local", "command": ["docker", "run", "mcp/fs"], "enabled": True,
               "environment": {"K": "v"}},
    })
    servers = build_mcp_json(cfg)["mcpServers"]
    assert servers["fs"] == {"command": "docker", "args": ["run", "mcp/fs"], "env": {"K": "v"}}
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement `pi/mcp_json.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Build pi-mcp-adapter's mcp.json (SP1 §6). Carries every egress path in ONE file:
facades (memory/repo/a2a) + proxied external MCP as REMOTE loopback entries, passthrough as
direct entries, codemem as a LOCAL stdio child. The ek_ is NEVER written — facades/proxy inject
it harness-side (identical hygiene to opencode.json). The adapter `settings` block carries the
headless knobs (directTools/excludeTools/sampling/elicitation) — those are ADAPTER settings,
NOT Pi settings.json."""
from __future__ import annotations

from typing import Any

from ach_agent.engine.base.driver import EngineConfig


def _passthrough_to_pi(entry: dict[str, Any]) -> dict[str, Any]:
    """Convert an opencode-shaped passthrough entry (from mcp_passthrough.to_opencode_entry)
    into pi-mcp-adapter shape: local → {command, args, env}; remote → {url, headers}."""
    if entry.get("type") == "local":
        cmd = list(entry.get("command", []))
        out: dict[str, Any] = {"command": cmd[0] if cmd else "", "args": cmd[1:]}
        if entry.get("environment"):
            out["env"] = entry["environment"]
        return out
    out = {"url": entry.get("url", "")}
    if entry.get("headers"):
        out["headers"] = entry["headers"]
    return out


def build_mcp_json(cfg: EngineConfig) -> dict[str, Any]:
    servers: dict[str, dict[str, Any]] = {}
    # Memory / repo / a2a facades (present iff wired) → remote at the localhost facade.
    for i, url in enumerate(cfg.mcp_servers):
        servers[f"facade-{i}"] = {"url": url}
    # Proxied external MCP (McpProxy) → remote at the localhost proxy.
    for sid, url in cfg.mcp_local_urls.items():
        servers[sid] = {"url": url}
    # Passthrough local/remote → direct entries (opencode-shape converted to pi-shape).
    for name, entry in cfg.extra_mcp_servers.items():
        servers[name] = _passthrough_to_pi(entry)
    # codemem → local stdio child (mirrors opencode's type=local).
    if cfg.codemem_db_path:
        servers["codemem"] = {
            "command": "codemem",
            "args": ["mcp", "--db-path", cfg.codemem_db_path],
            "env": {
                "CODEMEM_VIEWER": "0",
                "CODEMEM_VIEWER_AUTO": "0",
                "CODEMEM_PROJECT": cfg.codemem_project,
            },
        }
    return {
        "mcpServers": servers,
        # Adapter (NOT Pi) settings — headless knobs (§6).
        "settings": {
            "directTools": True,                       # named tools in the system prompt (parity)
            "excludeTools": list(cfg.exclude_tools),   # capability.filter.exclude withholdings
            "sampling": False,                          # no MCP-sampling callbacks headless
            "elicitation": False,                       # no interactive elicitation headless
        },
    }
```

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add src/ach_agent/engine/pi/mcp_json.py tests/engine/pi/test_mcp_json.py
git commit -m "feat(pi): build pi-mcp-adapter mcp.json (facades/proxy remote, codemem local, no ek)"
```

---

### Task 8.5: `pi/config.py` (`build_pi_settings` + `build_pi_env`)

- [ ] **Step 1: Write the failing test** — `tests/engine/pi/test_config.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from pathlib import Path

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.config import build_pi_env, build_pi_settings


def test_settings_defaults_trust_always_and_wires_skills_and_adapter() -> None:
    s = build_pi_settings(skills_dir=Path("/home/skills"), mcp_adapter_path="/vendor/pi-mcp-adapter")
    assert s["defaultProjectTrust"] == "always"       # valid values ask|always|never
    assert s["skills"] == ["/home/skills"]
    assert s["packages"] == ["/vendor/pi-mcp-adapter"]


def test_env_is_clean_slate_and_never_carries_ek(monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("MY_CA", "/etc/ca.pem")
    cfg = EngineConfig(forward_env=["MY_CA"])
    env = build_pi_env(Path("/home/agent/pi/k1"), cfg)
    assert "ACH_TOKEN" not in env and "ek_secret" not in env.values()
    assert env["PATH"] == "/usr/bin"
    assert env["MY_CA"] == "/etc/ca.pem"               # forwarded by name only
    assert env["PI_CODING_AGENT_DIR"] == "/home/agent/pi/k1"
    assert env["HOME"] == "/home/agent/pi/k1" and env["GIT_TERMINAL_PROMPT"] == "0"
```

- [ ] **Step 2: Run → FAIL. Step 3: Implement `pi/config.py`**

```python
# SPDX-License-Identifier: Apache-2.0
"""Pi settings.json + clean-slate subprocess env (SP1 §5.1, §8). Mirrors build_opencode_env's
ek-hygiene: the ek_ is NEVER in the env unless the operator explicitly forwards it (and it must
not — the localhost proxy injects it)."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from ach_agent.engine.base.driver import EngineConfig

# Same benign CLI basics as opencode; secrets (ACH_TOKEN/ACH_API_KEY, provider keys) are ABSENT.
_PI_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {"PATH", "SHELL", "USER", "LOGNAME", "HOSTNAME", "LANG", "LANGUAGE", "TERM", "TZ"}
)


def build_pi_settings(skills_dir: Path, mcp_adapter_path: str) -> dict[str, Any]:
    """Pi's $PI_CODING_AGENT_DIR/settings.json. `defaultProjectTrust: always` so Pi never
    blocks on the project-trust prompt headless; `packages` references the vendored
    pi-mcp-adapter (never a runtime `pi install`)."""
    return {
        "skills": [str(skills_dir)],
        "defaultProjectTrust": "always",
        "packages": [mcp_adapter_path] if mcp_adapter_path else [],
    }


def build_pi_env(agent_dir: Path, cfg: EngineConfig) -> dict[str, str]:
    env: dict[str, str] = {n: os.environ[n] for n in _PI_ENV_ALLOWLIST if n in os.environ}
    for name in cfg.forward_env:  # operator exceptions, by NAME (never the ek_)
        value = os.environ.get(name)
        if value is not None:
            env[name] = value
    # Pinned last — override anything inherited/forwarded.
    env["HOME"] = str(agent_dir)
    env["TMPDIR"] = "/tmp"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["PI_CODING_AGENT_DIR"] = str(agent_dir)  # the isolation primitive (OPENCODE_CONFIG analog)
    return env
```

- [ ] **Step 4: Run → PASS. Commit.**

```bash
git add src/ach_agent/engine/pi/config.py tests/engine/pi/test_config.py
git commit -m "feat(pi): settings.json (trust=always) + clean-slate build_pi_env (no ek)"
```

---

### Task 8.6: `pi/driver.py` — the real `PiDriver`

Replace the Phase-6 stub with the real implementation. `PiDriver` reuses the existing `ManagedServer` (its `_client` holds the `PiRpcClient`; `port=0`, unused) and `_key_suffix` from `lifecycle`.

- [ ] **Step 1: Write the failing test** — `tests/engine/pi/test_driver.py` (fake `pi` via a fake `ManagedServer` whose `_client` is a scripted `PiRpcClient`-like object):

```python
# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.pi.driver import PiDriver
from ach_agent.engine.pi.protocol import (
    EV_AGENT_SETTLED, EV_ASSISTANT_INNER, EV_INNER_TEXT_DELTA, EV_MESSAGE_UPDATE,
    EV_SESSION_CREATED, EV_TOOL_START, F_SESSION_PATH,
)


class _ScriptedClient:
    """Replays queued Pi events on recv(); records sent commands."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.sent: list[dict[str, Any]] = []

    async def send(self, cmd: dict[str, Any]) -> None:
        self.sent.append(cmd)

    async def recv(self) -> dict[str, Any]:
        return self._events.pop(0) if self._events else {"type": "__eof__"}

    async def close(self) -> None:
        return None


class _Server:
    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    def is_alive(self) -> bool:
        return True


def _text(s: str) -> dict[str, Any]:
    return {"type": EV_MESSAGE_UPDATE, EV_ASSISTANT_INNER: {"type": EV_INNER_TEXT_DELTA, "text": s}}


async def test_new_session_then_prompt_accumulates_text() -> None:
    client = _ScriptedClient([
        {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/abc.json"},  # ack for new_session
        _text("hel"), _text("lo"),
        {"type": EV_AGENT_SETTLED},
    ])
    sessions: dict[str, str] = {}
    stats: dict[str, Any] = {}
    result = await PiDriver().run_turn(
        _Server(client), conv_key="k", prompt="p", reuse=True, sessions=sessions, stats=stats,
    )
    assert result == TurnResult(text="hello", session_ref="/s/abc.json", aborted=False)
    assert sessions["k"] == "/s/abc.json"
    assert client.sent[0]["type"] == "new_session"
    assert client.sent[1] == {"type": "prompt", "message": "p"}


async def test_session_ref_switches_and_bypasses_map() -> None:
    client = _ScriptedClient([_text("wrapped"), {"type": EV_AGENT_SETTLED}])
    sessions: dict[str, str] = {}
    result = await PiDriver().run_turn(
        _Server(client), conv_key="k", prompt="wrap", reuse=True, sessions=sessions,
        session_ref="/s/fixed.json", max_tool_calls=0, stats={},
    )
    assert result.session_ref == "/s/fixed.json" and result.text == "wrapped"
    assert sessions == {}
    assert client.sent[0] == {"type": "switch_session", "sessionPath": "/s/fixed.json"}


async def test_max_tool_calls_aborts_and_flags() -> None:
    client = _ScriptedClient([
        {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/a.json"},
        {"type": EV_TOOL_START, "toolName": "t", "callId": "c1"},
        {"type": EV_TOOL_START, "toolName": "t", "callId": "c2"},
        {"type": EV_AGENT_SETTLED},
    ])
    stats: dict[str, Any] = {}
    result = await PiDriver().run_turn(
        _Server(client), conv_key="k", prompt="p", reuse=True, sessions={}, max_tool_calls=2, stats=stats,
    )
    assert result.aborted is True
    assert {"type": "abort"} in client.sent


async def test_cancel_sends_abort() -> None:
    class _Hanging(_ScriptedClient):
        async def recv(self) -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

    client = _Hanging([{"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/a.json"}])
    task = asyncio.ensure_future(
        PiDriver().run_turn(_Server(client), conv_key="k", prompt="p", reuse=False, sessions={}, stats={})
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {"type": "abort"} in client.sent
```

- [ ] **Step 2: Run → FAIL** (stub raises `NotImplementedError`).

- [ ] **Step 3: Implement the real `pi/driver.py`** (replace the whole stub file)

```python
# SPDX-License-Identifier: Apache-2.0
"""PiDriver — the Pi implementation of EngineDriver (SP1 §5). Drives `pi --mode rpc` over JSONL;
durable sessions via --session-dir; bounds/abort enforced at the transport. MCP egress rides the
vendored pi-mcp-adapter mcp.json. The ek_ never reaches Pi (loopback proxy/facades inject it)."""
from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, MutableMapping

import structlog

from ach_agent.engine.base.driver import EngineConfig, TurnResult
from ach_agent.engine.pi import events as pe
from ach_agent.engine.pi.config import build_pi_env, build_pi_settings
from ach_agent.engine.pi.mcp_json import build_mcp_json
from ach_agent.engine.pi.models_json import build_models_json
from ach_agent.engine.pi.protocol import (
    CMD_ABORT, CMD_NEW_SESSION, CMD_PROMPT, CMD_SWITCH_SESSION, EV_EOF, EV_SESSION_CREATED,
    F_SESSION_PATH,
)
from ach_agent.engine.pi.rpc import PiRpcClient, PiRpcError

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

log = structlog.get_logger(__name__)

# Vendored pi-mcp-adapter path baked into the image (SP2 pins the exact version). Overridable
# via engine.pi.mcpAdapterPath. Falls back to this when config leaves it empty.
_DEFAULT_PI_MCP_ADAPTER = "/opt/pi-mcp-adapter"


class PiDriver:
    engine_type = "pi"

    def skills_dir(self, home: Path) -> Path:
        # Shared skills dir under the Pi home; per-key settings.json points here (§4.2).
        return home / "pi" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        from ach_agent.engine.lifecycle import ManagedServer, _key_suffix

        agent_dir = Path(cfg.home) / "pi" / _key_suffix(session_key)
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "sessions").mkdir(exist_ok=True)

        models_doc, provider = build_models_json(cfg)
        # engine.pi.mcpAdapterPath is threaded onto cfg by main (Step 4); fall back to the image default.
        adapter_path = cfg.pi_mcp_adapter_path or _DEFAULT_PI_MCP_ADAPTER
        settings_doc = build_pi_settings(self.skills_dir(Path(cfg.home)), adapter_path)
        mcp_doc = build_mcp_json(cfg)
        (agent_dir / "models.json").write_text(json.dumps(models_doc, indent=2), encoding="utf-8")
        (agent_dir / "settings.json").write_text(json.dumps(settings_doc, indent=2), encoding="utf-8")
        (agent_dir / "mcp.json").write_text(json.dumps(mcp_doc, indent=2), encoding="utf-8")

        binary = shutil.which(cfg.binary_path)
        if not binary:
            raise RuntimeError(f"pi binary not found: {cfg.binary_path!r}")
        work_dir = Path(cfg.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            binary, "--mode", "rpc", "--provider", provider, "--model", cfg.model,
            "--session-dir", str(agent_dir / "sessions"),
            cwd=str(work_dir),
            env=build_pi_env(agent_dir, cfg),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # process-group kill safety (H-03)
        )
        server = ManagedServer(port=0, ephemeral_home=agent_dir)
        server._process = proc
        server._client = PiRpcClient(proc)
        asyncio.create_task(self._drain_stderr(proc, server))
        # Startup sanity: if pi died immediately, surface it now.
        await asyncio.sleep(0)
        if proc.returncode is not None:
            raise RuntimeError(f"pi exited immediately (rc={proc.returncode})")
        return server

    @staticmethod
    async def _drain_stderr(proc: Any, server: ManagedServer) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await stderr.readline()
                if not line:
                    return
                server._stderr_tail.append(line.decode("utf-8", "replace").rstrip("\n"))

    async def health(self, server: ManagedServer) -> bool:
        # Process alive is the authoritative cheap check; a full RPC roundtrip is unnecessary
        # (a dead process is caught by is_alive; the pool replaces it).
        return server.is_alive()

    async def _new_session(self, client: Any) -> str:
        await client.send({"type": CMD_NEW_SESSION})
        while True:
            ev = await client.recv()
            if ev.get("type") == EV_EOF:
                raise PiRpcError("pi ended before session_created")
            if ev.get("type") == EV_SESSION_CREATED:
                return str(ev.get(F_SESSION_PATH, "") or "")

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
        client = server._client
        stats = stats if stats is not None else {}

        # 1. Session select (mirrors opencode's pool-map flow, §5.3).
        if session_ref is not None:
            ref = session_ref
            await client.send({"type": CMD_SWITCH_SESSION, F_SESSION_PATH: ref})
        elif reuse:
            cached = sessions.get(conv_key)
            if cached is None:
                ref = await self._new_session(client)
                sessions[conv_key] = ref
            else:
                ref = cached
                await client.send({"type": CMD_SWITCH_SESSION, F_SESSION_PATH: ref})
        else:
            ref = await self._new_session(client)
        stats["session_ref"] = ref

        # 2. Send the prompt (safe: the lane serializes; repair/wrap sends wait for settle).
        await client.send({"type": CMD_PROMPT, "message": prompt})

        # 3. Consume until agent_settled; enforce max_tool_calls at the transport (§5.5).
        text_parts: list[str] = []
        tool_ids: set[str] = set()
        aborted = False
        try:
            while True:
                ev = await client.recv()
                if ev.get("type") == EV_EOF:
                    raise PiRpcError("pi ended before agent_settled")
                delta = pe.pi_text_delta(ev)
                if delta:
                    text_parts.append(delta)
                    if on_text is not None:
                        on_text(delta)
                    continue
                tu = pe.pi_tool_update(ev, ref)
                if tu is not None:
                    if on_tool is not None:
                        on_tool(tu)
                    if tu.state.status == "running":
                        tool_ids.add(tu.call_id)
                        if max_tool_calls > 0 and not aborted and len(tool_ids) >= max_tool_calls:
                            aborted = True
                            log.warning("pi: max_tool_calls reached — aborting", limit=max_tool_calls)
                            await client.send({"type": CMD_ABORT})
                    continue
                usage = pe.pi_usage(ev, ref)
                if usage is not None:
                    stats["usage"] = usage
                    continue
                if pe.is_settled(ev):
                    break
        except asyncio.CancelledError:
            # maxInvocationSeconds / on_kill: best-effort abort from the cancel path (§5.5).
            with contextlib.suppress(Exception):
                await asyncio.shield(client.send({"type": CMD_ABORT}))
            raise

        stats["aborted"] = aborted
        return TurnResult(text="".join(text_parts), session_ref=ref, aborted=aborted)

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        # session='none' / rotate: delete the on-disk session file (best-effort — disk residue
        # only, never worth failing the event over).
        try:
            Path(session_ref).unlink(missing_ok=True)
        except OSError:
            log.warning("pi: session file delete failed", session_ref=session_ref, exc_info=True)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        # §12 open item — Pi has no verified RPC compaction (docs describe it as automatic).
        # Decision: no-op; overflow='compact' on Pi degrades to Pi's automatic compaction.
        # (overflow='rotate' still works — engine_runner pops the map + calls discard_session.)
        log.info("pi: compact is a no-op (automatic); rely on Pi's built-in compaction",
                 session_ref=session_ref)

    async def stop(self, server: ManagedServer) -> None:
        await server.stop()  # process-group kill + client.close() (PiRpcClient.close)
```

**Note:** `ManagedServer.stop()` calls `client.close()` if present (`PiRpcClient.close` closes the reader + stdin) and `release_port(0)` (safe no-op). No change to `ManagedServer` is needed. `cfg.pi_mcp_adapter_path` is threaded by main in Step 4.

- [ ] **Step 4: Thread the adapter path from config**

In `main.py`, when building `engine_cfg` (Phase 6 added `binary_path` for pi), also carry the adapter path. Add a field to `EngineConfig` (`base/driver.py`): `pi_mcp_adapter_path: str = ""`. In `main.py`'s `EngineConfig(...)` construction add:

```python
        pi_mcp_adapter_path=(
            cfg.engine.pi.mcp_adapter_path
            if cfg.engine.type == "pi" and cfg.engine.pi is not None
            else ""
        ),
```

- [ ] **Step 5: Run driver units → PASS**

Run: `uv run pytest tests/engine/pi/ -q`
Expected: PASS.

- [ ] **Step 6: mypy + commit**

Run: `uv run mypy --strict src/ach_agent/engine/pi/`

```bash
git add src/ach_agent/engine/pi/driver.py src/ach_agent/engine/base/driver.py \
        src/ach_agent/main.py tests/engine/pi/test_driver.py
git commit -m "feat(pi): PiDriver — launch, durable sessions, run_turn with bounds/abort"
```

---

### Task 8.7: One real-`pi` e2e with `ek_`-hygiene assertions

- [ ] **Step 1: Write the e2e** — `tests/e2e/test_pi_e2e.py` (gated like other e2e; skips if `pi` is absent):

```python
# SPDX-License-Identifier: Apache-2.0
"""Real-pi e2e (SP1 §10): a real `pi --mode rpc` behind the localhost model proxy + a stub MCP
server, asserting one turn completes AND the ek_ never appears in models.json/settings.json/
mcp.json or the Pi subprocess env. Skipped when `pi` is not installed."""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

pi = shutil.which("pi")
if pi is None:
    pytest.skip("pi binary not installed", allow_module_level=True)


async def test_pi_turn_and_ek_never_on_disk(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_e2e_secret_marker")
    # ... stand up: a stub model proxy (echoes an assistant reply), a stub MCP server behind a
    # loopback facade, an EngineConfig(engine_type="pi", model_base_url=<stub proxy>,
    # mcp_servers=[<stub facade>], home=tmp_path, work_dir=tmp_path/"ws"). Then:
    from ach_agent.engine.pi.driver import PiDriver

    driver = PiDriver()
    # server = await driver.launch(cfg, "e2e-key")
    # result = await driver.run_turn(server, conv_key="e2e-key", prompt="say hi", reuse=True,
    #                                sessions={}, stats={})
    # assert result.text  # non-empty reply from the model stub
    # await driver.stop(server)

    agent_dir = tmp_path / "pi" / "e2e-key"  # _key_suffix — adjust to the actual suffix
    for name in ("models.json", "settings.json", "mcp.json"):
        blob = (agent_dir / name).read_text(encoding="utf-8") if (agent_dir / name).exists() else ""
        assert "ek_e2e_secret_marker" not in blob, f"ek leaked into {name}"
    assert json.loads((agent_dir / "settings.json").read_text())["defaultProjectTrust"] == "always"
```

Flesh out the stub proxy/MCP/`EngineConfig` wiring following `tests/e2e/test_a2a_e2e.py` and `tests/engine/test_model_proxy.py` (same loopback-stub pattern). The load-bearing assertions are: (1) a turn returns non-empty text through a real `pi`, and (2) `grep`-equivalent proves `ek_e2e_secret_marker` is absent from all three config files (and, if reachable, `/proc/<pid>/environ`).

- [ ] **Step 2: Run (skips if no `pi`)**

Run: `uv run pytest tests/e2e/test_pi_e2e.py -q`
Expected: PASS (or SKIP where `pi` is unavailable — CI with `pi` runs it; the SP2 Dockerfile pins `pi`).

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_pi_e2e.py
git commit -m "test(pi): real-pi e2e — one turn + ek never on disk/env"
```

---

### Task 8.8: Decision record + final gate

- [ ] **Step 1: Write the decision record**

Create `docs/references/2026-07-DD-pi-engine-driver.md` (use today's date) summarizing: the `EngineDriver` seam, why Pi rides RPC (no HTTP/SSE), the terminal-contract carve, the engine-namespaced sessions map, and MCP egress via vendored pi-mcp-adapter. Add a row to `docs/references/README.md` (per CLAUDE.md's decision-records convention).

- [ ] **Step 2: Full gate**

Run: `make lint && uv run pytest tests/ -q && make conformance`
Expected: all PASS. (`make lint` = ruff check + ruff format --check + mypy --strict.)

- [ ] **Step 3: Commit**

```bash
git add docs/references/
git commit -m "docs(reference): record the EngineDriver seam + Pi engine (SP1)"
```

---

## Self-review (Phase 8)

- **Spec coverage:** §5.1 launch (models/settings/mcp json + `pi --mode rpc --provider --model --session-dir`, `PI_CODING_AGENT_DIR`, `defaultProjectTrust: always`), §5.2 RPC (LF framing, background reader), §5.3 the turn (`assistantMessageEvent` unwrap, `agent_settled`, tool start/end), §5.4 durable sessions via `--session-dir` + pool map (engine-namespaced from Phase 4), §5.5 bounds/abort (`max_tool_calls` at the transport, cancel-path abort, `stop` = process-group kill), §6 MCP egress (facades/proxy remote, passthrough, codemem local, `directTools`/`excludeTools`, vendored adapter), §8 ek-hygiene (asserted in units + e2e), §9 stats (Pi usage → same `OpenCodeUsage` sink), §12 compact open item (decided: no-op/automatic). All covered.
- **Placeholders:** none in the shipped modules. The e2e (8.7) intentionally leaves the stub-proxy/MCP wiring as guided TODO comments — it depends on a real `pi` binary and mirrors existing e2e scaffolding (`test_a2a_e2e.py`, `test_model_proxy.py`); the load-bearing assertions are concrete. This is the one place the plan cannot be fully literal without the external binary.
- **External-schema risk:** all Pi wire literals live in `protocol.py`; units validate logic via fixtures; the e2e is the authority. A field-name mismatch is a one-line `protocol.py` fix, not a rewrite.
- **Type consistency:** `PiDriver.run_turn` signature matches the index contract and the `run_contract_turn` call (Phase 6). `build_models_json -> (dict, provider)`, `build_mcp_json -> dict`, `build_pi_settings`/`build_pi_env` match their `driver.launch` call sites. `pi_tool_update` returns the shared `OpenCodeToolUpdate` the `on_tool` sink expects.
