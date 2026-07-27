# `mcpServers` Config Block Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move repo-checkout out of `engine.repoCheckout` into a new top-level `mcpServers` map that also carries operator-declared `local` (stdio) and `remote` MCP servers passed through to opencode.

**Architecture:** One config map keyed by server name, discriminated on `type`. `repoCheckout` is harness-hosted (FastMCP facade + ek_, unchanged behaviour, only its config source moves). `local`/`remote` are passthrough: the harness normalizes them into `opencode.json`'s `mcp` block and opencode connects directly (no ACH proxy). Mirrors `ackbot-process._normalize_mcp_server`.

**Tech Stack:** Python 3.12, Pydantic v2 (discriminated unions), FastMCP (existing facade), pytest/mypy/ruff via `uv`.

## Global Constraints

- **ek_ hygiene:** `ACH_TOKEN`/`ACH_API_KEY`/`ACH_KEY` NEVER logged, NEVER written to `opencode.json`, NEVER forwarded. `repoCheckout` injects it harness-side only.
- **Secrets are env NAMES/refs, never values in config.** `local.env` = env NAMES; `remote.headers` values = `${env:NAME}` refs. The harness resolves at `opencode.json` write time (passthrough auth necessarily lands in `opencode.json` — opencode needs it; this is the honest cost of not going through ACH).
- **Pydantic:** `ConfigDict(extra="forbid", populate_by_name=True)` on every model; camelCase aliases.
- **Frozen schema:** `AgentConfig` is the source of truth; regenerate `docs/schemas/agent-config-v1.schema.json` via `scripts/gen_schema.py` whenever the schema changes (drift-guarded by `tests/config/test_schema_artifact.py`).
- **Contract source of truth:** `docs/plan/CONTRACT_v3-ADDENDUM-mcpservers.md` (already written) — the exact config shape, Pydantic code, and operator seam.
- **Tooling:** `uv run pytest … -q`, `uv run mypy --strict src/ach_agent/<path>`. Commit per task.
- **This plan is harness-only.** The ach-runtime operator change (CRD `spec.mcpServers`) is a separate repo — handed off via the addendum §5. Do NOT touch it here.

---

## File Structure

- `src/ach_agent/config/schema.py` — **modify**: add the `mcpServers` discriminated union (`RepoCheckoutParams`, `RepoCheckoutServer`, `LocalMcpServer`, `RemoteMcpServer`, `McpServerConfig`), add `AgentConfig.mcp_servers`; **delete** `RepoCheckoutBlock` + `EngineBlock.repo_checkout`.
- `src/ach_agent/engine/mcp_passthrough.py` — **create**: `to_opencode_entry(spec)` translating `local`/`remote` config → an `opencode.json` `mcp.<name>` entry (env/header resolution).
- `src/ach_agent/engine/lifecycle.py` — **modify**: `EngineConfig` gains `extra_mcp_servers: dict[str, dict]`; `write_opencode_config` merges it into the `mcp` block.
- `src/ach_agent/main.py` — **modify**: two pure helpers (`collect_passthrough_mcp`, `find_repo_checkout`); replace the `cfg.engine.repo_checkout` boot block with iteration over `cfg.mcp_servers`; thread `extra_mcp_servers` into `EngineConfig`.
- `tests/config/test_mcp_servers_config.py` — **create** (replaces `test_repo_checkout_config.py`, **deleted**).
- `tests/engine/test_mcp_passthrough.py` — **create**.
- `tests/engine/test_lifecycle_mcp.py` — **create** (or extend an existing lifecycle test): `write_opencode_config` writes passthrough entries.
- `tests/test_main_mcp_servers.py` — **create**: the two pure helpers.
- `docs/plan/CONTRACT_v3.md` — **modify**: replace the `engine.repoCheckout` block (§2) with an `mcpServers` section.
- `docs/schemas/agent-config-v1.schema.json` — **regenerated** (not hand-edited).

Existing tests that stay green unchanged: `test_repo_facade.py`, `test_repo_archive.py`, `test_main_repo_checkout.py` (the `resolve_repo_archive_endpoint` signature and `build_engine_prompt` hint are untouched).

---

### Task 1: Schema — `mcpServers` union in, `engine.repoCheckout` out

**Files:**
- Modify: `src/ach_agent/config/schema.py` (delete lines 61-81 `RepoCheckoutBlock`; delete `EngineBlock.repo_checkout` field lines 112-114; add the union near the other blocks; add `AgentConfig.mcp_servers`)
- Create: `tests/config/test_mcp_servers_config.py`
- Delete: `tests/config/test_repo_checkout_config.py`
- Regenerate: `docs/schemas/agent-config-v1.schema.json`

**Interfaces:**
- Produces: `RepoCheckoutParams` (`.source_mcp_server_id: str`, `.tmp_base: str`, `.ttl_seconds: float`); `RepoCheckoutServer` (`.type Literal["repoCheckout"]`, `.repo_checkout: RepoCheckoutParams`); `LocalMcpServer` (`.type Literal["local"]`, `.command: str`, `.args: list[str]`, `.env: list[str]`); `RemoteMcpServer` (`.type Literal["remote"]`, `.url: str`, `.headers: dict[str,str]`); `McpServerConfig` union; `AgentConfig.mcp_servers: dict[str, McpServerConfig]`.

- [ ] **Step 1: Write the failing test**

Create `tests/config/test_mcp_servers_config.py`:

```python
from __future__ import annotations

import pytest
from pydantic import TypeAdapter, ValidationError

from ach_agent.config.schema import (
    AgentConfig,
    EngineBlock,
    LocalMcpServer,
    McpServerConfig,
    RemoteMcpServer,
    RepoCheckoutServer,
)

_ADAPTER = TypeAdapter(dict[str, McpServerConfig])


def test_repo_checkout_parses() -> None:
    m = _ADAPTER.validate_python(
        {"repo-checkout": {"type": "repoCheckout", "repoCheckout": {"sourceMcpServerId": "mcp-gitlab-ro"}}}
    )
    e = m["repo-checkout"]
    assert isinstance(e, RepoCheckoutServer)
    assert e.repo_checkout.source_mcp_server_id == "mcp-gitlab-ro"
    assert e.repo_checkout.tmp_base == "/tmp/gitlab"
    assert e.repo_checkout.ttl_seconds == 3600.0


def test_local_parses() -> None:
    m = _ADAPTER.validate_python(
        {"fs": {"type": "local", "command": "docker", "args": ["run", "--rm", "mcp/filesystem"]}}
    )
    e = m["fs"]
    assert isinstance(e, LocalMcpServer)
    assert e.command == "docker"
    assert e.args == ["run", "--rm", "mcp/filesystem"]
    assert e.env == []


def test_remote_parses() -> None:
    m = _ADAPTER.validate_python(
        {"other": {"type": "remote", "url": "https://x/mcp", "headers": {"Authorization": "Bearer ${env:T}"}}}
    )
    e = m["other"]
    assert isinstance(e, RemoteMcpServer)
    assert e.url == "https://x/mcp"
    assert e.headers == {"Authorization": "Bearer ${env:T}"}


def test_repo_checkout_requires_source_id() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"x": {"type": "repoCheckout", "repoCheckout": {}}})


def test_local_requires_command() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"x": {"type": "local"}})


def test_unknown_type_rejected() -> None:
    with pytest.raises(ValidationError):
        _ADAPTER.validate_python({"x": {"type": "bogus"}})


def test_engineblock_has_no_repo_checkout() -> None:
    # The block moved out of engine to top-level mcpServers.
    assert not hasattr(EngineBlock(), "repo_checkout")


def test_agentconfig_mcp_servers_default_empty() -> None:
    cfg = AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "a"},
            "model": {"name": "m", "type": "openai"},
            "capability": {"type": "ach", "ach": {"baseUrl": "https://ach"}},
        }
    )
    assert cfg.mcp_servers == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_mcp_servers_config.py -q`
Expected: FAIL — `ImportError: cannot import name 'McpServerConfig'`.

- [ ] **Step 3: Delete the obsolete config test + `RepoCheckoutBlock`**

Delete the old file:
```bash
git rm tests/config/test_repo_checkout_config.py
```

In `src/ach_agent/config/schema.py`, DELETE the `RepoCheckoutBlock` class (currently lines 61-81) and the `repo_checkout` field in `EngineBlock` (currently lines 112-114):

```python
    repo_checkout: RepoCheckoutBlock = Field(
        default_factory=RepoCheckoutBlock, alias="repoCheckout"
    )
```

- [ ] **Step 4: Add the `mcpServers` union**

In `src/ach_agent/config/schema.py`, add these classes AFTER the `Memory` union definition (search for `Memory = Annotated[...]`; add just below it, so the union lives beside the other discriminated unions). Ensure `Annotated` and `Literal` are imported (they already are — used by `Memory`/prompt):

```python
# ---------------------------------------------------------------------------
# mcpServers — harness-managed MCP servers (CONTRACT_v3 §2a, ADDENDUM-mcpservers)
# ---------------------------------------------------------------------------


class RepoCheckoutParams(BaseModel):
    """Params for the harness-hosted repoCheckout facade (the `checkout_repo` tool)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # The hydrated runtime.mcpServers[].id whose endpoint serves the
    # gitlab://{project}/archive/{ref} resource the harness reads (harness-side, with the ek_).
    source_mcp_server_id: str = Field(alias="sourceMcpServerId")
    tmp_base: str = Field(default="/tmp/gitlab", alias="tmpBase")
    ttl_seconds: float = Field(default=3600.0, ge=0, alias="ttlSeconds")


class RepoCheckoutServer(BaseModel):
    """INTERNAL: the harness HOSTS this MCP (FastMCP facade), injecting the ek_."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["repoCheckout"]
    repo_checkout: RepoCheckoutParams = Field(alias="repoCheckout")


class LocalMcpServer(BaseModel):
    """PASSTHROUGH: opencode LAUNCHES this as a stdio subprocess."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["local"]
    command: str
    args: list[str] = Field(default_factory=list)
    env: list[str] = Field(default_factory=list)  # env NAMES only; never the ek_


class RemoteMcpServer(BaseModel):
    """PASSTHROUGH: opencode CONNECTS directly to a remote MCP endpoint."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["remote"]
    url: str
    headers: dict[str, str] = Field(default_factory=dict)  # values are ${env:NAME} refs


# Strict discriminated union on `type` (mirror of the Memory union). Named *Config to avoid
# clashing with engine.hydrate.McpServer (the hydrated {id,endpoint} external server).
McpServerConfig = Annotated[
    RepoCheckoutServer | LocalMcpServer | RemoteMcpServer, Field(discriminator="type")
]
```

In the `AgentConfig` class, add the field (after `engine`):

```python
    mcp_servers: dict[str, McpServerConfig] = Field(default_factory=dict, alias="mcpServers")
```

- [ ] **Step 5: Run the config test to verify it passes**

Run: `uv run pytest tests/config/test_mcp_servers_config.py -q`
Expected: PASS (8 passed).

- [ ] **Step 6: Regenerate the frozen schema + verify drift guard**

Run:
```bash
uv run python scripts/gen_schema.py
uv run pytest tests/config/test_schema_artifact.py -q
uv run python scripts/gen_schema.py --check
```
Expected: writer prints `wrote …`; drift test PASS; `--check` prints `OK`.

- [ ] **Step 7: mypy + commit**

Run: `uv run mypy --strict src/ach_agent/config/schema.py`
Expected: `Success: no issues found`.

```bash
git add src/ach_agent/config/schema.py tests/config/test_mcp_servers_config.py docs/schemas/agent-config-v1.schema.json
git commit -m "feat(config): mcpServers block (repoCheckout|local|remote); drop engine.repoCheckout"
```

---

### Task 2: Passthrough writer — `to_opencode_entry` + `EngineConfig` + `write_opencode_config`

**Files:**
- Create: `src/ach_agent/engine/mcp_passthrough.py`
- Modify: `src/ach_agent/engine/lifecycle.py` (`EngineConfig` add `extra_mcp_servers` after `codemem_project` ~line 110; `write_opencode_config` merge into `mcp_block` ~line 324)
- Create: `tests/engine/test_mcp_passthrough.py`
- Create: `tests/engine/test_lifecycle_mcp.py`

**Interfaces:**
- Consumes: `LocalMcpServer`, `RemoteMcpServer` (Task 1).
- Produces: `to_opencode_entry(spec: LocalMcpServer | RemoteMcpServer) -> dict[str, object]`; `EngineConfig.extra_mcp_servers: dict[str, dict[str, object]]`.

- [ ] **Step 1: Write the failing test for the normalizer**

Create `tests/engine/test_mcp_passthrough.py`:

```python
from __future__ import annotations

import pytest

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer
from ach_agent.engine.mcp_passthrough import to_opencode_entry


def test_local_to_entry() -> None:
    spec = LocalMcpServer(type="local", command="docker", args=["run", "--rm", "mcp/fs"])
    assert to_opencode_entry(spec) == {
        "type": "local",
        "command": ["docker", "run", "--rm", "mcp/fs"],
        "enabled": True,
    }


def test_local_env_resolved_from_names(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_VAR", "val1")
    monkeypatch.delenv("MISSING_VAR", raising=False)
    spec = LocalMcpServer(type="local", command="x", env=["MY_VAR", "MISSING_VAR"])
    entry = to_opencode_entry(spec)
    assert entry["environment"] == {"MY_VAR": "val1"}  # missing name omitted


def test_remote_to_entry() -> None:
    spec = RemoteMcpServer(type="remote", url="https://x/mcp")
    assert to_opencode_entry(spec) == {"type": "remote", "url": "https://x/mcp", "enabled": True}


def test_remote_headers_expand_env_refs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOKEN", "sekret")
    spec = RemoteMcpServer(
        type="remote", url="https://x/mcp", headers={"Authorization": "Bearer ${env:TOKEN}"}
    )
    entry = to_opencode_entry(spec)
    assert entry["headers"] == {"Authorization": "Bearer sekret"}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/engine/test_mcp_passthrough.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.mcp_passthrough'`.

- [ ] **Step 3: Implement the normalizer**

Create `src/ach_agent/engine/mcp_passthrough.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Translate passthrough MCP config (local/remote) into opencode.json mcp entries.

opencode is the MCP client for these — it connects DIRECTLY (not through the ACH localhost
proxy). Mirrors ackbot-process._normalize_mcp_server: stdio → type "local" (command array),
http → type "remote" (url + headers). Env NAMES / ${env:NAME} refs are resolved harness-side
at write time — passthrough auth necessarily lands in opencode.json (opencode needs it).
"""

from __future__ import annotations

import os
import re

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer

# ponytail: only our contract's ${env:NAME} form; opencode's own interpolation is not relied on.
_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(value: str) -> str:
    """Expand ${env:NAME} → os.environ[NAME] (empty string if unset)."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def to_opencode_entry(spec: LocalMcpServer | RemoteMcpServer) -> dict[str, object]:
    """A single opencode.json `mcp.<name>` value for a passthrough server."""
    if isinstance(spec, LocalMcpServer):
        entry: dict[str, object] = {
            "type": "local",
            "command": [spec.command, *spec.args],
            "enabled": True,
        }
        env = {name: os.environ[name] for name in spec.env if name in os.environ}
        if env:
            entry["environment"] = env
        return entry
    # RemoteMcpServer
    remote: dict[str, object] = {"type": "remote", "url": spec.url, "enabled": True}
    if spec.headers:
        remote["headers"] = {k: _expand_env_refs(v) for k, v in spec.headers.items()}
    return remote
```

- [ ] **Step 4: Run to verify the normalizer passes**

Run: `uv run pytest tests/engine/test_mcp_passthrough.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Write the failing test for `write_opencode_config`**

Create `tests/engine/test_lifecycle_mcp.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

from ach_agent.engine.lifecycle import EngineConfig, write_opencode_config


def test_write_config_includes_passthrough_mcp(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".config" / "opencode").mkdir(parents=True)
    cfg = EngineConfig(
        home=str(home),
        work_dir=str(home / "workspace"),
        model="m",
        model_type="openai",
        model_base_url="http://127.0.0.1:1/v1",
        extra_mcp_servers={
            "fs": {"type": "local", "command": ["docker", "run"], "enabled": True},
            "other": {"type": "remote", "url": "https://x/mcp", "enabled": True},
        },
    )
    path = write_opencode_config(home, cfg, "sess-key")
    written = json.loads(Path(path).read_text())
    assert written["mcp"]["fs"] == {"type": "local", "command": ["docker", "run"], "enabled": True}
    assert written["mcp"]["other"] == {"type": "remote", "url": "https://x/mcp", "enabled": True}
```

- [ ] **Step 6: Run to verify it fails**

Run: `uv run pytest tests/engine/test_lifecycle_mcp.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'extra_mcp_servers'`.

- [ ] **Step 7: Add `extra_mcp_servers` to `EngineConfig`**

In `src/ach_agent/engine/lifecycle.py`, in the `EngineConfig` dataclass, add after the `codemem_project` field (~line 110):

```python
    # Passthrough MCP servers (mcpServers type local|remote), pre-normalized to opencode.json
    # mcp.<name> entries by engine.mcp_passthrough.to_opencode_entry. opencode connects to these
    # DIRECTLY (not via the localhost proxy). Static per-agent (boot-computed from cfg.mcp_servers).
    extra_mcp_servers: dict[str, dict[str, object]] = field(default_factory=dict)
```

- [ ] **Step 8: Merge passthrough entries into the mcp block**

In `write_opencode_config`, immediately AFTER the `codemem` block appends to `mcp_block` and BEFORE `if mcp_block:` (currently ~line 324), add:

```python
    # Passthrough MCP servers (mcpServers local|remote) — opencode connects directly.
    # Keyed by operator-chosen name; last-writer-wins is impossible (config map keys unique).
    for name, entry in config.extra_mcp_servers.items():
        mcp_block[name] = entry
```

- [ ] **Step 9: Run the lifecycle test + mypy**

Run: `uv run pytest tests/engine/test_lifecycle_mcp.py tests/engine/test_mcp_passthrough.py -q`
Expected: PASS (5 passed).

Run: `uv run mypy --strict src/ach_agent/engine/mcp_passthrough.py src/ach_agent/engine/lifecycle.py`
Expected: `Success: no issues found`.

- [ ] **Step 10: Commit**

```bash
git add src/ach_agent/engine/mcp_passthrough.py src/ach_agent/engine/lifecycle.py tests/engine/test_mcp_passthrough.py tests/engine/test_lifecycle_mcp.py
git commit -m "feat(engine): passthrough local/remote MCP servers into opencode.json"
```

---

### Task 3: Boot wiring — iterate `cfg.mcp_servers` in `main.py`

**Files:**
- Modify: `src/ach_agent/main.py` (add two helpers near `resolve_repo_archive_endpoint` ~line 560; replace the `rc = cfg.engine.repo_checkout` block ~lines 1217-1232; add `extra_mcp_servers=` to `EngineConfig(...)` ~line 1334)
- Create: `tests/test_main_mcp_servers.py`

**Interfaces:**
- Consumes: `McpServerConfig`, `RepoCheckoutParams`, `RepoCheckoutServer`, `LocalMcpServer`, `RemoteMcpServer` (Task 1); `to_opencode_entry` (Task 2); `resolve_repo_archive_endpoint(mcp_servers, server_id)` (existing); `RepoCheckoutFacade` (existing).
- Produces: `collect_passthrough_mcp(mcp_servers: dict[str, McpServerConfig]) -> dict[str, dict[str, object]]`; `find_repo_checkout(mcp_servers: dict[str, McpServerConfig]) -> tuple[str, RepoCheckoutParams] | None`.

- [ ] **Step 1: Write the failing test for the pure helpers**

Create `tests/test_main_mcp_servers.py`:

```python
from __future__ import annotations

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer, RepoCheckoutParams, RepoCheckoutServer
from ach_agent.main import collect_passthrough_mcp, find_repo_checkout


def _repo() -> RepoCheckoutServer:
    return RepoCheckoutServer(
        type="repoCheckout", repo_checkout=RepoCheckoutParams(source_mcp_server_id="mcp-gitlab-ro")
    )


def test_collect_passthrough_skips_repocheckout() -> None:
    servers = {
        "repo-checkout": _repo(),
        "fs": LocalMcpServer(type="local", command="docker", args=["run"]),
        "other": RemoteMcpServer(type="remote", url="https://x/mcp"),
    }
    out = collect_passthrough_mcp(servers)
    assert set(out) == {"fs", "other"}  # repoCheckout is NOT passthrough
    assert out["fs"]["type"] == "local"
    assert out["other"]["type"] == "remote"


def test_find_repo_checkout_returns_name_and_params() -> None:
    servers = {"fs": LocalMcpServer(type="local", command="x"), "repo-checkout": _repo()}
    found = find_repo_checkout(servers)
    assert found is not None
    name, params = found
    assert name == "repo-checkout"
    assert params.source_mcp_server_id == "mcp-gitlab-ro"


def test_find_repo_checkout_none_when_absent() -> None:
    assert find_repo_checkout({"fs": LocalMcpServer(type="local", command="x")}) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_main_mcp_servers.py -q`
Expected: FAIL — `ImportError: cannot import name 'collect_passthrough_mcp'`.

- [ ] **Step 3: Add the helpers + imports**

In `src/ach_agent/main.py`, ensure the imports are present near the top (module level):

```python
from ach_agent.config.schema import (
    LocalMcpServer,
    McpServerConfig,
    RemoteMcpServer,
    RepoCheckoutParams,
    RepoCheckoutServer,
)
from ach_agent.engine.mcp_passthrough import to_opencode_entry
```

(If `ach_agent.config.schema` is already imported for other names, extend that import list instead of adding a new line.)

Add the two helpers immediately BEFORE `resolve_repo_archive_endpoint` (~line 560):

```python
def collect_passthrough_mcp(
    mcp_servers: dict[str, McpServerConfig],
) -> dict[str, dict[str, object]]:
    """Normalize every local/remote entry to an opencode.json mcp.<name> value.

    repoCheckout entries are skipped — the harness hosts those itself (facade), they are not
    passed through to opencode.
    """
    out: dict[str, dict[str, object]] = {}
    for name, spec in mcp_servers.items():
        if isinstance(spec, (LocalMcpServer, RemoteMcpServer)):
            out[name] = to_opencode_entry(spec)
    return out


def find_repo_checkout(
    mcp_servers: dict[str, McpServerConfig],
) -> tuple[str, RepoCheckoutParams] | None:
    """The (name, params) of the repoCheckout entry, or None.

    ponytail: one repoCheckout facade per agent (the only real case). If several are declared,
    take the first and WARN — supporting N facades is unneeded plumbing until asked.
    """
    found: tuple[str, RepoCheckoutParams] | None = None
    for name, spec in mcp_servers.items():
        if isinstance(spec, RepoCheckoutServer):
            if found is not None:
                log.warning("multiple repoCheckout mcpServers — using first", ignored=name)
                continue
            found = (name, spec.repo_checkout)
    return found
```

- [ ] **Step 4: Run to verify the helpers pass**

Run: `uv run pytest tests/test_main_mcp_servers.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Replace the boot block that reads `cfg.engine.repo_checkout`**

In `src/ach_agent/main.py`, REPLACE the current block (~lines 1217-1232):

```python
        # Start the repo-checkout facade beside the proxies (engine.repoCheckout.enabled).
        # It fronts gitlab-mcp's archive resource with the ek_ (x-ach-key), so the agent gets a
        # local checkout without ever seeing the ek_ or the raw endpoint.
        rc = cfg.engine.repo_checkout
        if rc.enabled:
            gl_endpoint = resolve_repo_archive_endpoint(manifest.mcp_servers, rc.mcp_server_id)
            if gl_endpoint:
                from ach_agent.engine.repo_facade import RepoCheckoutFacade

                repo_facade = RepoCheckoutFacade(gl_endpoint, ek, rc.tmp_base, rc.ttl_seconds)
                repo_facade_url = await repo_facade.start()
            else:
                log.warning(
                    "repo checkout enabled but gitlab endpoint not in manifest — tool not wired",
                    mcp_server_id=rc.mcp_server_id,
                )
```

WITH:

```python
        # Start the repo-checkout facade beside the proxies (mcpServers type=repoCheckout).
        # It fronts gitlab-mcp's archive resource with the ek_ (x-ach-key), so the agent gets a
        # local checkout without ever seeing the ek_ or the raw endpoint.
        _rc = find_repo_checkout(cfg.mcp_servers)
        if _rc is not None:
            _rc_name, _rc_params = _rc
            gl_endpoint = resolve_repo_archive_endpoint(
                manifest.mcp_servers, _rc_params.source_mcp_server_id
            )
            if gl_endpoint:
                from ach_agent.engine.repo_facade import RepoCheckoutFacade

                repo_facade = RepoCheckoutFacade(
                    gl_endpoint, ek, _rc_params.tmp_base, _rc_params.ttl_seconds
                )
                repo_facade_url = await repo_facade.start()
            else:
                log.warning(
                    "repoCheckout: source mcp server not in manifest — tool not wired",
                    source_mcp_server_id=_rc_params.source_mcp_server_id,
                )
```

- [ ] **Step 6: Compute passthrough at boot + thread into EngineConfig**

In `src/ach_agent/main.py`, immediately BEFORE the `engine_cfg = EngineConfig(` construction (~line 1334), add:

```python
    passthrough_mcp = collect_passthrough_mcp(cfg.mcp_servers)
```

Then add this argument inside the `EngineConfig(...)` call (e.g. after `exclude_tools=...`, before the closing `)`):

```python
        extra_mcp_servers=passthrough_mcp,
```

(The `warm` and `engine_runner` paths use `dataclasses.replace(engine_cfg, …)`, which preserves `extra_mcp_servers` automatically — no other construction site to touch.)

- [ ] **Step 7: Run the full affected suites + mypy**

Run:
```bash
uv run pytest tests/test_main_mcp_servers.py tests/test_main_repo_checkout.py tests/engine/ tests/config/ -q
uv run mypy --strict src/ach_agent/main.py
```
Expected: all PASS; mypy `Success`.

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_mcp_servers.py
git commit -m "feat(main): wire mcpServers block — repoCheckout facade + local/remote passthrough"
```

---

### Task 4: CONTRACT doc + full gates + finish

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (§2 engine block: remove the `repoCheckout` sub-block; add a new `mcpServers` section)

- [ ] **Step 1: Update CONTRACT_v3 §2**

In `docs/plan/CONTRACT_v3.md`, in the `engine` block (the jsonc sample), DELETE the `repoCheckout` sub-block that was added for the old design (the `"repoCheckout": { … }` object and its comment lines), and restore `maxToolCalls` as the last key (no trailing comma).

Then add a new top-level `mcpServers` sample block after the `memory` block, using the content from `docs/plan/CONTRACT_v3-ADDENDUM-mcpservers.md` §2 (the three-entry jsonc). §9 already names the three harness-hosted MCPs — leave it.

- [ ] **Step 2: Run the full gate**

Run:
```bash
uv run pytest tests/ -q --ignore=tests/e2e
uv run mypy --strict src/ach_agent
uv run ruff check src/ach_agent tests
uv run ruff format --check src/ach_agent tests
uv run python scripts/gen_schema.py --check
```
Expected: all green (tests pass, mypy `Success`, ruff clean, schema `OK`). If ruff format flags files YOU touched, run `uv run ruff format <those files>` and re-check.

- [ ] **Step 3: Run the conformance suite (router invariants must still hold)**

Run: `uv run pytest tests/ -q -k conformance` (or `make conformance` if the devtools container is available).
Expected: PASS — this change does not touch the router; confirm no regression.

- [ ] **Step 4: Commit the doc**

```bash
git add docs/plan/CONTRACT_v3.md
git commit -m "docs(contract): replace engine.repoCheckout with top-level mcpServers block"
```

- [ ] **Step 5: Finish the branch**

**REQUIRED SUB-SKILL:** Use superpowers:finishing-a-development-branch — verify tests, present merge/PR/keep/discard options, execute the choice.

---

## Self-Review Notes

- **Spec coverage:** repoCheckout relocation (Task 1 schema, Task 3 wiring), local passthrough (Task 2 normalizer + Task 3 collect), remote passthrough (same), opencode.json translation (Task 2 write_opencode_config), env/header secret handling (Task 2 `to_opencode_entry`), CONTRACT + frozen schema (Task 1 + Task 4). Operator side is explicitly out of scope (separate repo; addendum §5).
- **Type consistency:** `source_mcp_server_id` (not `mcp_server_id`) used everywhere post-move; `to_opencode_entry` signature matches its call in `collect_passthrough_mcp`; `extra_mcp_servers` field name identical in `EngineConfig`, `write_opencode_config`, and the `EngineConfig(...)` call.
- **Unchanged-by-design:** `resolve_repo_archive_endpoint`, `RepoCheckoutFacade`, `build_engine_prompt(..., repo_checkout_enabled=...)`, and the shutdown/warm `repo_facade`/`repo_facade_url` plumbing — the prompt hint still fires iff `repo_facade_url is not None`.
```
