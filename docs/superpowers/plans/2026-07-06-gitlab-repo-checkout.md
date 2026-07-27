# GitLab Repo Checkout (MCP archive resource → workDir) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the agent an on-disk repo checkout for deep analysis (full-tree `rg`, tests, build) by having the harness read the gitlab-mcp repo-archive *resource* and extract it under `/tmp/gitlab`, exposed to opencode via one harness-hosted MCP tool `checkout_repo(project, ref, subpath?)`.

**Architecture:** opencode cannot consume MCP resources (it discards binary blobs), so the **harness** is the resource consumer. A shared boot-time `FastMCP` facade (mirrors `MemoryFacade`) exposes `checkout_repo`; the handler reads `gitlab://{project}/archive/{ref}[/{subpath}]` from the gitlab-mcp endpoint (authed harness-side with the `ek_` as `x-ach-key`, never seen by the agent), base64-decodes the `application/gzip` blob, and `tarfile`-extracts it into a fresh `mkdtemp` dir under `/tmp/gitlab`. The blob never enters the model context; only the returned path does. Cleanup is a TTL sweep on each call + a full `rmtree` at harness shutdown (Option A — the shared facade cannot link a call to a `session_key`, so exact session-close deletion is out of scope).

**Tech Stack:** Python 3.12 (asyncio, `tarfile` `filter="data"`, `tempfile.mkdtemp`), `mcp>=1.28.0` (`ClientSession.read_resource` → `ReadResourceResult.contents[0].blob`), `mcp.server.fastmcp.FastMCP` + `uvicorn`, Pydantic v2, structlog, pytest(+asyncio).

## Global Constraints

- **`mcp>=1.28.0,<2`** — `ClientSession.read_resource(uri)` returns `ReadResourceResult`; `.contents[0]` is `BlobResourceContents` with `.blob: str` (base64). Tools use `.content`; resources use `.contents` (note the plural).
- **Python 3.12** — use `tarfile.TarFile.extractall(path, filter="data")` (path-traversal-safe extraction). NEVER call bare `extractall` on the archive.
- **`ek_` hygiene (CONTRACT §6.10):** the `ek_` (`os.environ["ACH_TOKEN"]`) is injected harness-side as the `x-ach-key` header (ACH's scheme — `Authorization: Bearer` 401s). It is closure-only, NEVER logged, and NEVER reaches opencode's config/env. The agent calls the localhost facade; the facade calls gitlab-mcp with the `ek_`.
- **Pydantic `extra="forbid"` everywhere** — every new config field MUST be modeled or `load_config` hard-fails (`sys.exit(1)`). After any `AgentConfig` field change, regenerate the frozen schema: `uv run python scripts/gen_schema.py` (writes `docs/schemas/agent-config-v1.schema.json`).
- **Resource errors RAISE** (resources, not tools): `read_resource` throws on over-cap (>~50 MB compressed, message contains "exceeds cap"), auth failure, and GitLab 403/404. There is NO silent truncation — a successful read is the complete archive. The tool MUST catch and fail-soft (return an error string, never crash the turn).
- **Observability never breaks a turn:** a checkout failure returns a helpful string; the agent falls back to the existing per-file gitlab-mcp read tools.
- **Locked resource contract (from gitlab-mcp, behind `GITLAB_REPO_ARCHIVE=1`):**
  - `gitlab://{project_id}/archive/{ref}` → whole tree at ref
  - `gitlab://{project_id}/archive/{ref}/{subpath*}` → subtree (slashes allowed in subpath)
  - FastMCP URL-decodes captured params, so params are sent **encoded**. Numeric `project_id` + a commit-SHA `ref` need **zero encoding** (cleanest path). A branch name with `/` would need encoding — prefer the SHA.
- **Snapshot limits (design around, do not fix):** no `.git` → no blame/log/history; build steps that shell `git describe`/tags fail. LFS ships pointer files, not content. Submodules are NOT recursed. `rg` + tests + build + multi-file reasoning: fine.
- **Prerequisite:** the gitlab-mcp archive resource is being built behind `GITLAB_REPO_ARCHIVE=1` and is NOT yet live. Tasks 1–7 build + unit-test against a mocked `read_resource` and are fully landable now. Task 8 (E2E) is gated on the flag flipping.

## File Structure

- `src/ach_agent/config/schema.py` — **modify**: add `RepoCheckoutBlock`, add `EngineBlock.repo_checkout`.
- `docs/schemas/agent-config-v1.schema.json` — **modify** (regen only).
- `src/ach_agent/channels/webhook.py` — **modify**: extract MR head SHA into `delivery_context`.
- `tests/channels/test_webhook.py` — **modify**: fixtures gain `last_commit`; assert `head_sha`.
- `src/ach_agent/engine/repo_archive.py` — **create**: pure URI builder, resource-read client, extract, TTL sweep.
- `tests/engine/test_repo_archive.py` — **create**.
- `src/ach_agent/engine/repo_facade.py` — **create**: `RepoCheckoutFacade` (FastMCP `checkout_repo` tool + start/stop).
- `tests/engine/test_repo_facade.py` — **create**.
- `src/ach_agent/main.py` — **modify**: `resolve_repo_archive_endpoint`, construct/start/stop facade, append its URL to `mcp_servers`, add the prompt hint.
- `tests/test_main_repo_checkout.py` — **create**: endpoint resolver + prompt-hint unit tests.

---

### Task 1: Config — `RepoCheckoutBlock` on `EngineBlock`

**Files:**
- Modify: `src/ach_agent/config/schema.py` (add class before `EngineBlock` at line 61; add field inside `EngineBlock`)
- Modify: `docs/schemas/agent-config-v1.schema.json` (regen)
- Test: `tests/config/test_repo_checkout_config.py` (create)

**Interfaces:**
- Produces: `RepoCheckoutBlock(enabled: bool, mcp_server_id: str, tmp_base: str, ttl_seconds: float)`; `EngineBlock.repo_checkout: RepoCheckoutBlock`.

- [ ] **Step 1: Write the failing test**

Create `tests/config/test_repo_checkout_config.py`:

```python
from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_agent.config.schema import EngineBlock, RepoCheckoutBlock


def test_defaults_disabled() -> None:
    rc = RepoCheckoutBlock()
    assert rc.enabled is False
    assert rc.tmp_base == "/tmp/gitlab"
    assert rc.ttl_seconds == 3600.0


def test_enabled_requires_server_id() -> None:
    with pytest.raises(ValidationError, match="mcpServerId is required"):
        RepoCheckoutBlock(enabled=True)


def test_enabled_with_server_id_ok() -> None:
    rc = RepoCheckoutBlock.model_validate({"enabled": True, "mcpServerId": "gitlab"})
    assert rc.mcp_server_id == "gitlab"


def test_engineblock_default_has_repo_checkout() -> None:
    eb = EngineBlock()
    assert eb.repo_checkout.enabled is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_repo_checkout_config.py -q`
Expected: FAIL with `ImportError: cannot import name 'RepoCheckoutBlock'`.

- [ ] **Step 3: Add the model + field**

In `src/ach_agent/config/schema.py`, immediately BEFORE `class EngineBlock(BaseModel):` (line 61), add:

```python
class RepoCheckoutBlock(BaseModel):
    """engine.repoCheckout — expose the harness `checkout_repo` tool (gitlab archive → workDir).

    enabled=False → tool not wired. When enabled, mcpServerId names the hydrated McpServer.id
    whose endpoint serves the `gitlab://{project}/archive/{ref}` resource. tmpBase is the parent
    dir for per-checkout mkdtemp dirs; ttlSeconds bounds how long a stale checkout lingers before
    the next call sweeps it (Option A: no exact session-close deletion — /tmp is ephemeral).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    enabled: bool = False
    mcp_server_id: str = Field(default="", alias="mcpServerId")
    tmp_base: str = Field(default="/tmp/gitlab", alias="tmpBase")
    ttl_seconds: float = Field(default=3600.0, ge=0, alias="ttlSeconds")

    @model_validator(mode="after")
    def _check(self) -> RepoCheckoutBlock:
        if self.enabled and not self.mcp_server_id:
            raise ValueError("engine.repoCheckout.mcpServerId is required when enabled")
        return self
```

Then inside `class EngineBlock`, after the `max_tool_calls` field (line 88), add:

```python
    repo_checkout: RepoCheckoutBlock = Field(
        default_factory=RepoCheckoutBlock, alias="repoCheckout"
    )
```

(`ConfigDict`, `Field`, `model_validator`, `BaseModel` are already imported at the top of `schema.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_repo_checkout_config.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Regenerate the frozen schema**

Run: `uv run python scripts/gen_schema.py`
Expected: `wrote .../docs/schemas/agent-config-v1.schema.json`. Confirm `repoCheckout` appears: `grep -c repoCheckout docs/schemas/agent-config-v1.schema.json` → non-zero.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/config/schema.py docs/schemas/agent-config-v1.schema.json tests/config/test_repo_checkout_config.py
git commit -m "feat(config): add engine.repoCheckout block for checkout_repo tool"
```

---

### Task 2: Webhook — extract MR head SHA into `delivery_context`

**Files:**
- Modify: `src/ach_agent/channels/webhook.py` (`_parse_gitlab`, lines 118–170)
- Test: `tests/channels/test_webhook.py`

**Interfaces:**
- Produces: `delivery_context["head_sha"]` (str) present on MR-hook and note-on-MR events when the payload carries it; key absent when it does not (no crash). `build_engine_prompt` (Task 7) reads `dc.get("head_sha")`.

The head SHA lives at `object_attributes.last_commit.id` on a `merge_request` hook and at `merge_request.last_commit.id` on a note-on-MR hook. (From GitLab's webhook schema — no in-repo fixture has it yet; this task adds them.)

- [ ] **Step 1: Write the failing test**

Add to `tests/channels/test_webhook.py` (near the other routing tests, after `test_mr_hook_default_routes_with_kind`):

```python
@pytest.mark.asyncio
async def test_mr_hook_extracts_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 42, "name": "my-repo"},
        "object_attributes": {"iid": 7, "title": "x", "last_commit": {"id": "9af2c1e0deadbeef"}},
    }
    result, handler = await _post(payload, cfg, "s")
    assert result.status_code == 202
    assert handler.events[0].delivery_context["head_sha"] == "9af2c1e0deadbeef"


@pytest.mark.asyncio
async def test_note_on_mr_extracts_head_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    payload = {
        "object_kind": "note",
        "project": {"id": 42},
        "merge_request": {"iid": 7, "last_commit": {"id": "abc123"}},
        "object_attributes": {"noteable_type": "MergeRequest", "note": "please rebase"},
    }
    result, handler = await _post(payload, cfg, "s")
    assert result.status_code == 202
    assert handler.events[0].delivery_context["head_sha"] == "abc123"


@pytest.mark.asyncio
async def test_mr_hook_without_head_sha_omits_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_ENV, "s")
    cfg = _make_cfg_events()
    result, handler = await _post(MR_PAYLOAD, cfg, "s")  # MR_PAYLOAD has no last_commit
    assert result.status_code == 202
    assert "head_sha" not in handler.events[0].delivery_context
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/channels/test_webhook.py -k head_sha -q`
Expected: FAIL — `KeyError: 'head_sha'` on the first two.

- [ ] **Step 3: Add a helper + populate the two MR branches**

In `src/ach_agent/channels/webhook.py`, add a helper next to `_gitlab_actor`:

```python
def _mr_head_sha(container: dict[str, Any]) -> str:
    """The MR source-branch head commit SHA from a merge_request/note payload block.

    `container` is object_attributes (MR hook) or the merge_request block (note hook).
    Empty string if absent — never raises.
    """
    last = container.get("last_commit")
    return str(last.get("id", "")) if isinstance(last, dict) else ""
```

In `_parse_gitlab`, the `merge_request` branch (line 118) — after computing `mr_iid`, add `head_sha` to the returned dict ONLY when present:

```python
    if kind == "merge_request" and "merge_request" in allowed:
        project_id = int(body["project"]["id"])
        mr_iid = int(body["object_attributes"]["iid"])
        dc: dict[str, Any] = {
            "project_id": project_id,
            "kind": "merge_request",
            "target_type": "mr",
            "mr_iid": mr_iid,
        }
        head_sha = _mr_head_sha(body["object_attributes"])
        if head_sha:
            dc["head_sha"] = head_sha
        return (dc, f"{project_id}:{mr_iid}")
```

In the note-on-MR branch (line 149, `if noteable == "mergerequest"`), mirror it:

```python
        if noteable == "mergerequest" and "merge_request" in allowed:
            project_id = int(body["project"]["id"])
            mr_iid = int(body["merge_request"]["iid"])
            dc = {"project_id": project_id, "kind": "note", "target_type": "mr", "mr_iid": mr_iid}
            head_sha = _mr_head_sha(body["merge_request"])
            if head_sha:
                dc["head_sha"] = head_sha
            return (dc, f"{project_id}:{mr_iid}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/channels/test_webhook.py -q`
Expected: PASS (all webhook tests, including the 3 new).

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/channels/webhook.py tests/channels/test_webhook.py
git commit -m "feat(webhook): extract MR head SHA into delivery_context"
```

---

### Task 3: `repo_archive.py` — URI builder + resource-read client

**Files:**
- Create: `src/ach_agent/engine/repo_archive.py`
- Test: `tests/engine/test_repo_archive.py`

**Interfaces:**
- Produces: `build_archive_uri(project: str, ref: str, subpath: str | None) -> str`; `async read_repo_archive(endpoint: str, ek: str, project: str, ref: str, subpath: str | None = None) -> bytes` (returns the gzip tar bytes; RAISES on read error).
- Consumes: `mcp.ClientSession`, `create_mcp_http_client`, `streamable_http_client` (same imports as `memory/hindsight.py:22-24`).

- [ ] **Step 1: Write the failing test**

Create `tests/engine/test_repo_archive.py`:

```python
from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ach_agent.engine import repo_archive


def test_build_uri_whole_repo() -> None:
    assert repo_archive.build_archive_uri("1234", "9af2c1e0", None) == "gitlab://1234/archive/9af2c1e0"


def test_build_uri_subpath_keeps_slashes() -> None:
    assert (
        repo_archive.build_archive_uri("1234", "9af2c1e0", "src/app")
        == "gitlab://1234/archive/9af2c1e0/src/app"
    )


def test_build_uri_encodes_specials_in_subpath() -> None:
    # a space must be encoded; slashes must survive
    assert (
        repo_archive.build_archive_uri("1234", "9af2c1e0", "my dir/x")
        == "gitlab://1234/archive/9af2c1e0/my%20dir/x"
    )


@pytest.mark.asyncio
async def test_read_repo_archive_decodes_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"\x1f\x8b\x08fake-gzip-bytes"

    class _Blob:
        blob = base64.b64encode(raw).decode()

    class _Result:
        contents = [_Blob()]

    class _Session:
        async def read_resource(self, uri: Any) -> _Result:
            assert str(uri) == "gitlab://1234/archive/abc"
            return _Result()

    @asynccontextmanager
    async def _fake_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
        assert endpoint == "https://mcp.example/gitlab"
        assert ek == "ek_test"
        yield _Session()

    monkeypatch.setattr(repo_archive, "_archive_session", _fake_session)
    out = await repo_archive.read_repo_archive("https://mcp.example/gitlab", "ek_test", "1234", "abc")
    assert out == raw


@pytest.mark.asyncio
async def test_read_repo_archive_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Session:
        async def read_resource(self, uri: Any) -> Any:
            raise RuntimeError("archive exceeds cap")

    @asynccontextmanager
    async def _fake_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
        yield _Session()

    monkeypatch.setattr(repo_archive, "_archive_session", _fake_session)
    with pytest.raises(RuntimeError, match="exceeds cap"):
        await repo_archive.read_repo_archive("e", "k", "1234", "abc")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_repo_archive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.repo_archive'`.

- [ ] **Step 3: Write the module (URI + client parts)**

Create `src/ach_agent/engine/repo_archive.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""GitLab repo-archive MCP resource client + local extraction (Task 3/4).

Reads the gitlab-mcp `gitlab://{project}/archive/{ref}[/{subpath}]` resource, authenticating
harness-side with the ek_ as `x-ach-key` (never seen by the agent), and returns the raw gzip
tar bytes. Extraction (Task 4) writes those bytes into a per-checkout dir under a tmp base.

SDK note: the installed `streamable_http_client` takes NO `headers=` kwarg — auth is injected
by pre-building an httpx client via `create_mcp_http_client(headers=...)` (mirrors
memory/hindsight.py:_hindsight_session).
"""

from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from urllib.parse import quote

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client


def build_archive_uri(project: str, ref: str, subpath: str | None) -> str:
    """Build the gitlab-mcp archive resource URI.

    Numeric project + SHA ref need no encoding. subpath keeps its slashes (path separators)
    but other specials are percent-encoded (FastMCP URL-decodes captured params).
    """
    uri = f"gitlab://{project}/archive/{ref}"
    if subpath:
        uri = f"{uri}/{quote(subpath, safe='/')}"
    return uri


@asynccontextmanager
async def _archive_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
    """Open a ClientSession to the gitlab-mcp endpoint with the ek as x-ach-key (harness-side)."""
    async with create_mcp_http_client(headers={"x-ach-key": ek}) as http_client:
        async with streamable_http_client(endpoint, http_client=http_client) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def read_repo_archive(
    endpoint: str, ek: str, project: str, ref: str, subpath: str | None = None
) -> bytes:
    """Read the archive resource → decoded gzip tar bytes. RAISES on read error (over-cap/auth/404)."""
    uri = build_archive_uri(project, ref, subpath)
    async with _archive_session(endpoint, ek) as session:
        result = await session.read_resource(uri)
    blob = result.contents[0].blob  # BlobResourceContents.blob is base64 (application/gzip)
    return base64.b64decode(blob)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_repo_archive.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/repo_archive.py tests/engine/test_repo_archive.py
git commit -m "feat(engine): gitlab archive resource client (uri + read)"
```

---

### Task 4: `repo_archive.py` — safe extraction + TTL sweep

**Files:**
- Modify: `src/ach_agent/engine/repo_archive.py`
- Test: `tests/engine/test_repo_archive.py`

**Interfaces:**
- Produces: `extract_archive(data: bytes, tmp_base: str, project: str, ref: str) -> str` (returns the repo-root path); `sweep_stale(tmp_base: str, ttl_seconds: float, now: float) -> int` (returns count removed).

- [ ] **Step 1: Write the failing test**

Add to `tests/engine/test_repo_archive.py`:

```python
import io
import os
import tarfile
import time
from pathlib import Path


def _make_targz(top: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_extract_returns_repo_root(tmp_path: Path) -> None:
    data = _make_targz("myrepo-9af2-9af2", {"README.md": b"hi", "src/app.py": b"x=1"})
    root = repo_archive.extract_archive(data, str(tmp_path), "1234", "9af2c1e0")
    assert Path(root).name == "myrepo-9af2-9af2"
    assert (Path(root) / "README.md").read_bytes() == b"hi"
    assert (Path(root) / "src" / "app.py").exists()


def test_extract_blocks_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"evil"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    # filter="data" must prevent the file escaping tmp_base's parent
    repo_archive.extract_archive(buf.getvalue(), str(tmp_path), "1234", "sha")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_sweep_removes_stale_keeps_fresh(tmp_path: Path) -> None:
    base = tmp_path / "gitlab"
    base.mkdir()
    old = base / "old"
    old.mkdir()
    fresh = base / "fresh"
    fresh.mkdir()
    now = 1_000_000.0
    os.utime(old, (now - 7200, now - 7200))  # 2h old
    os.utime(fresh, (now - 60, now - 60))  # 1min old
    removed = repo_archive.sweep_stale(str(base), 3600.0, now)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_missing_base_is_noop() -> None:
    assert repo_archive.sweep_stale("/tmp/does-not-exist-xyz", 3600.0, time.time()) == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_repo_archive.py -k "extract or sweep" -q`
Expected: FAIL — `AttributeError: module ... has no attribute 'extract_archive'`.

- [ ] **Step 3: Add extraction + sweep**

Append to `src/ach_agent/engine/repo_archive.py` (and add the needed imports to the top import block):

```python
import io
import shutil
import tarfile
import tempfile
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


def extract_archive(data: bytes, tmp_base: str, project: str, ref: str) -> str:
    """Extract gzip tar `data` into a fresh mkdtemp dir under tmp_base; return the repo root.

    GitLab archives nest everything under one top dir; when that holds, the repo root is that
    single child (so callers land inside the tree). Uses filter="data" to block path traversal.
    """
    Path(tmp_base).mkdir(parents=True, exist_ok=True)
    dest = tempfile.mkdtemp(prefix=f"{project}-{ref[:12]}-", dir=tmp_base)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        tf.extractall(dest, filter="data")
    children = [c for c in Path(dest).iterdir()]
    if len(children) == 1 and children[0].is_dir():
        return str(children[0])
    return dest


def sweep_stale(tmp_base: str, ttl_seconds: float, now: float) -> int:
    """rmtree every direct child of tmp_base older than ttl_seconds. Returns count removed."""
    base = Path(tmp_base)
    if not base.is_dir():
        return 0
    removed = 0
    for child in base.iterdir():
        try:
            if now - child.stat().st_mtime > ttl_seconds:
                shutil.rmtree(child, ignore_errors=True)
                removed += 1
        except OSError:  # noqa: PERF203 — child vanished mid-sweep; ignore
            continue
    if removed:
        log.info("repo checkout sweep", base=tmp_base, removed=removed)
    return removed
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_repo_archive.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/repo_archive.py tests/engine/test_repo_archive.py
git commit -m "feat(engine): safe archive extraction + TTL sweep"
```

---

### Task 5: `repo_facade.py` — `RepoCheckoutFacade` (FastMCP `checkout_repo`)

**Files:**
- Create: `src/ach_agent/engine/repo_facade.py`
- Test: `tests/engine/test_repo_facade.py`

**Interfaces:**
- Consumes: `read_repo_archive`, `extract_archive`, `sweep_stale` (Tasks 3–4); the `MemoryFacade` start/stop pattern (`memory/facade.py:142-172`).
- Produces: `RepoCheckoutFacade(endpoint: str, ek: str, tmp_base: str = "/tmp/gitlab", ttl_seconds: float = 3600.0)` with `async start() -> str` (returns MCP URL) and `async stop() -> None` (rmtrees `tmp_base`). Exposes tool `checkout_repo(project: str, ref: str, subpath: str | None = None) -> str`.

- [ ] **Step 1: Write the failing test**

Create `tests/engine/test_repo_facade.py`:

```python
from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.engine import repo_facade


def _targz(top: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"{top}/README.md")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_checkout_returns_path_on_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_read(endpoint, ek, project, ref, subpath=None):  # type: ignore[no-untyped-def]
        return _targz("repo-abc-abc")

    monkeypatch.setattr(repo_facade, "read_repo_archive", _fake_read)
    facade = repo_facade.RepoCheckoutFacade("e", "ek", tmp_base=str(tmp_path))
    out = await facade._checkout("1234", "abc", None)
    assert "repo-abc-abc" in out
    assert (Path(str(tmp_path)) / "repo-abc-abc" / "README.md").exists() or True  # nested mkdtemp
    # the returned path really exists and holds the file
    root = out.split("Checked out to ", 1)[1].split(" ", 1)[0]
    assert (Path(root) / "README.md").read_bytes() == b"hi"


@pytest.mark.asyncio
async def test_checkout_fail_soft_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("archive exceeds cap")

    monkeypatch.setattr(repo_facade, "read_repo_archive", _boom)
    facade = repo_facade.RepoCheckoutFacade("e", "ek", tmp_base=str(tmp_path))
    out = await facade._checkout("1234", "abc", None)
    assert out.startswith("Checkout failed:")
    assert "exceeds cap" in out  # never raises
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_repo_facade.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.engine.repo_facade'`.

- [ ] **Step 3: Write the facade**

Create `src/ach_agent/engine/repo_facade.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Harness-hosted repo-checkout MCP facade.

Exposes ONE agent-facing tool, `checkout_repo`, on 127.0.0.1. The handler reads the gitlab-mcp
archive resource (ek injected harness-side, never seen by the agent), extracts it under a tmp
base, and returns the local path. The archive blob never enters the model context — only the
path string does. Mirrors memory/facade.py's FastMCP + uvicorn lifecycle.

Cleanup (Option A): a TTL sweep runs before each checkout; stop() rmtrees the whole tmp base at
harness shutdown. The shared facade cannot attribute a call to a session_key, so there is no
exact session-close deletion — /tmp is ephemeral, wiped on pod restart.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from typing import Annotated

import structlog
import uvicorn
from mcp.server.fastmcp import FastMCP
from pydantic import Field

from ach_agent.engine.repo_archive import extract_archive, read_repo_archive, sweep_stale

log = structlog.get_logger(__name__)


class RepoCheckoutFacade:
    """FastMCP server exposing `checkout_repo`; reads the gitlab archive resource → local path."""

    def __init__(
        self, endpoint: str, ek: str, tmp_base: str = "/tmp/gitlab", ttl_seconds: float = 3600.0
    ) -> None:
        self._endpoint = endpoint
        self._ek = ek  # closure-only, never logged; injected as x-ach-key upstream
        self._tmp_base = tmp_base
        self._ttl = ttl_seconds
        self._mcp = FastMCP("ach-repo")
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._register_tools()

    async def _checkout(self, project: str, ref: str, subpath: str | None) -> str:
        """Read + extract the archive; fail-soft to a short note (never raises)."""
        try:
            sweep_stale(self._tmp_base, self._ttl, time.time())
            data = await read_repo_archive(self._endpoint, self._ek, project, ref, subpath)
            path = extract_archive(data, self._tmp_base, project, ref)
            return (
                f"Checked out to {path} (read-only snapshot, no .git — no blame/log/history). "
                "Use rg/tests/build there."
            )
        except Exception as exc:  # noqa: BLE001 — observability never breaks a turn
            log.warning("checkout_repo failed", project=project, ref=ref, error=str(exc))
            return (
                f"Checkout failed: {exc}. Narrow with a subpath if the repo is large, "
                "or use the per-file gitlab read tools instead."
            )

    def _register_tools(self) -> None:
        @self._mcp.tool(
            name="checkout_repo",
            description=(
                "Copy a GitLab repo (or subtree) to a local directory so you can run "
                "ripgrep/tests/build over the whole tree instead of reading one file at a time. "
                "Returns the local path. Read-only SNAPSHOT: no .git, so no blame/log/history and "
                "no `git describe`. For big repos pass `subpath` to fetch only what you need."
            ),
        )
        async def checkout_repo(
            project: Annotated[
                str, Field(description="Numeric GitLab project id (e.g. '1234').")
            ],
            ref: Annotated[
                str, Field(description="Commit SHA to check out (the MR head SHA).")
            ],
            subpath: Annotated[
                str | None,
                Field(description="Optional subtree, e.g. 'src/app', to stay small."),
            ] = None,
        ) -> str:
            return await self._checkout(project, ref, subpath)

    async def start(self) -> str:
        """Bind the facade on an ephemeral localhost port; return its MCP URL."""
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
            raise RuntimeError("repo facade failed to start within 5s")
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info("repo checkout facade started", port=port, tmp_base=self._tmp_base)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Signal uvicorn to exit, await the task, and rmtree the tmp base (shutdown sweep)."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._server = None
        self._task = None
        shutil.rmtree(self._tmp_base, ignore_errors=True)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/engine/test_repo_facade.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/ach_agent/engine/repo_facade.py tests/engine/test_repo_facade.py
git commit -m "feat(engine): RepoCheckoutFacade exposing checkout_repo tool"
```

---

### Task 6: Boot wiring — resolve endpoint, start/stop facade, append URL

**Files:**
- Modify: `src/ach_agent/main.py` (add `resolve_repo_archive_endpoint`; construct/start/stop the facade in `run()`; append its URL where `select_memory_wiring_async` result is used in the engine runner)
- Test: `tests/test_main_repo_checkout.py` (create)

**Interfaces:**
- Consumes: `RepoCheckoutFacade` (Task 5); `manifest.mcp_servers` (`list[McpServer]` with `.id`, `.endpoint`); `ek = os.environ["ACH_TOKEN"]` (`main.py:1128`); the memory-facade construction/shutdown pattern (`main.py:1169`, and its `.stop()` on shutdown).
- Produces: `resolve_repo_archive_endpoint(mcp_servers: list, server_id: str) -> str | None`; the repo facade URL appended to each event's `mcp_servers` list in the engine runner.

**Read before editing:** `main.py:1120-1175` (boot: `ek`, `manifest`, `memory_facade` construct/start), the shutdown block that calls `memory_facade.stop()`, and `main.py:630-660` (`engine_runner`, where `select_memory_wiring_async` is awaited and `dataclasses.replace(engine_cfg, mcp_servers=...)` is applied). Mirror the memory facade exactly.

- [ ] **Step 1: Write the failing test (pure resolver)**

Create `tests/test_main_repo_checkout.py`:

```python
from __future__ import annotations

from ach_agent.engine.hydrate import McpServer
from ach_agent.main import resolve_repo_archive_endpoint


def test_resolve_finds_by_id() -> None:
    servers = [McpServer(id="gitlab", endpoint="https://mcp/gl"), McpServer(id="jira", endpoint="x")]
    assert resolve_repo_archive_endpoint(servers, "gitlab") == "https://mcp/gl"


def test_resolve_missing_returns_none() -> None:
    servers = [McpServer(id="jira", endpoint="x")]
    assert resolve_repo_archive_endpoint(servers, "gitlab") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_repo_checkout.py -q`
Expected: FAIL — `ImportError: cannot import name 'resolve_repo_archive_endpoint'`.

- [ ] **Step 3: Add the resolver + boot glue**

In `src/ach_agent/main.py`, add the pure helper near the other resolve helpers:

```python
def resolve_repo_archive_endpoint(mcp_servers: list, server_id: str) -> str | None:
    """The endpoint of the hydrated McpServer whose id == server_id, or None."""
    for s in mcp_servers:
        if s.id == server_id:
            return s.endpoint
    return None
```

In `run()`, AFTER `memory_facade` is started (around `main.py:1172`) and where `ek` + `manifest` are in scope, add:

```python
    repo_facade = None
    repo_facade_url: str | None = None
    rc = cfg.engine.repo_checkout
    if rc.enabled:
        gl_endpoint = resolve_repo_archive_endpoint(manifest.mcp_servers, rc.mcp_server_id)
        if gl_endpoint and ek:
            from ach_agent.engine.repo_facade import RepoCheckoutFacade

            repo_facade = RepoCheckoutFacade(gl_endpoint, ek, rc.tmp_base, rc.ttl_seconds)
            repo_facade_url = await repo_facade.start()
        else:
            log.warning(
                "repo checkout enabled but no gitlab endpoint/ek — tool not wired",
                mcp_server_id=rc.mcp_server_id,
            )
```

Thread `repo_facade_url` into the engine runner factory (`_make_engine_runner`) alongside `memory_facade_url`, and in `engine_runner`, after the line `mcp_servers, memory_prompt = await select_memory_wiring_async(...)` (`main.py:637`), append it:

```python
        if repo_facade_url:
            mcp_servers = [*mcp_servers, repo_facade_url]
```

Add `repo_facade_url` to the boot pre-warm list (`warm_mcp_servers = [memory_facade_url]`, `main.py:1392`):

```python
    warm_mcp_servers = [u for u in (memory_facade_url, repo_facade_url) if u]
```

In the shutdown block where `memory_facade.stop()` is awaited, add:

```python
        if repo_facade is not None:
            await repo_facade.stop()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_repo_checkout.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Typecheck the touched modules**

Run: `uv run mypy --strict src/ach_agent/main.py src/ach_agent/engine/repo_facade.py src/ach_agent/engine/repo_archive.py`
Expected: `Success`. (If `manifest.mcp_servers` typing needs it, annotate the resolver param as `list[McpServer]` and import `McpServer` under `TYPE_CHECKING`.)

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_repo_checkout.py
git commit -m "feat(main): wire RepoCheckoutFacade into boot + engine mcp servers"
```

---

### Task 7: Prompt hint — tell the agent it can check out the repo

**Files:**
- Modify: `src/ach_agent/main.py` (`build_engine_prompt`, lines 311–394; and its call site to pass the flag)
- Test: `tests/test_main_repo_checkout.py`

**Interfaces:**
- Consumes: `delivery_context["head_sha"]` (Task 2), `cfg.engine.repo_checkout.enabled` (Task 1).
- Produces: `build_engine_prompt(..., repo_checkout_enabled: bool = False)` — appends a `checkout_repo(project=..., ref=<head_sha>)` hint on the fallback MR/note paths when enabled AND `head_sha` present.

Note: the hint is only added on the LEGACY fallback path (no `channel.prompt`). When an operator supplies a custom `channel.prompt`, that template wins (returns early), and the operator is responsible for their own checkout guidance.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_main_repo_checkout.py`:

```python
from ach_agent.channels.message_event import MessageEvent
from ach_agent.main import build_engine_prompt


def _mr_event(head_sha: str | None) -> MessageEvent:
    dc = {"project_id": 42, "kind": "merge_request", "mr_iid": 7}
    if head_sha:
        dc["head_sha"] = head_sha
    return MessageEvent(
        idempotency_key="i",
        session_key="42:7",
        channel_name="gl",
        payload={"object_attributes": {"iid": 7, "title": "x"}},
        delivery_context=dc,
        source_trait="sync",
    )


def test_prompt_hint_added_when_enabled_and_sha() -> None:
    out = build_engine_prompt(_mr_event("9af2c1e0"), repo_checkout_enabled=True)
    assert "checkout_repo(project=42, ref=9af2c1e0)" in out


def test_prompt_hint_absent_when_disabled() -> None:
    out = build_engine_prompt(_mr_event("9af2c1e0"), repo_checkout_enabled=False)
    assert "checkout_repo" not in out


def test_prompt_hint_absent_without_sha() -> None:
    out = build_engine_prompt(_mr_event(None), repo_checkout_enabled=True)
    assert "checkout_repo" not in out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_repo_checkout.py -k hint -q`
Expected: FAIL — `TypeError: build_engine_prompt() got an unexpected keyword argument 'repo_checkout_enabled'`.

- [ ] **Step 3: Add the flag + hint helper**

In `src/ach_agent/main.py`, add a helper above `build_engine_prompt`:

```python
def _checkout_hint(project_id: Any, head_sha: str) -> str:
    return (
        f" You can copy the repo locally for deep analysis: "
        f"checkout_repo(project={project_id}, ref={head_sha}) — returns a path with the full "
        f"tree for rg/tests/build (read-only snapshot, no .git)."
    )
```

Change the signature (line 311):

```python
def build_engine_prompt(
    event: MessageEvent,
    channel_cfg: Any = None,
    agent_name: str = "",
    memory_bank: str = "",
    repo_checkout_enabled: bool = False,
) -> str:
```

At the END of the note branch, before `return " ".join(parts)` (line 378), append the hint:

```python
        head_sha = dc.get("head_sha", "")
        if repo_checkout_enabled and head_sha and target_type != "issue":
            parts.append(_checkout_hint(project_id, str(head_sha)))
        return " ".join(parts)
```

At the END of the MR/issue branch, before the final `return " ".join(parts)` (line 394):

```python
    head_sha = dc.get("head_sha", "")
    if repo_checkout_enabled and head_sha and kind != "issue":
        parts.append(_checkout_hint(project_id, str(head_sha)))
    return " ".join(parts)
```

At the `build_engine_prompt(` call site (find with `grep -n "build_engine_prompt(" src/ach_agent/main.py` — the non-definition hit), pass the flag:

```python
        repo_checkout_enabled=cfg.engine.repo_checkout.enabled,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_main_repo_checkout.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Full lint + targeted tests**

Run: `uv run ruff check src/ach_agent tests && uv run ruff format --check src/ach_agent tests && uv run pytest tests/engine tests/channels/test_webhook.py tests/config tests/test_main_repo_checkout.py -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_repo_checkout.py
git commit -m "feat(main): prompt hint advertising checkout_repo on gitlab MR events"
```

---

### Task 8: E2E verification (GATED on `GITLAB_REPO_ARCHIVE=1`)

**Files:** none (manual verification against a live gitlab-mcp with the flag on).

This task cannot be automated until gitlab-mcp ships the resource. Do NOT block Tasks 1–7 on it.

- [ ] **Step 1: Confirm the flag is live**

Confirm with the gitlab-mcp owner that `GITLAB_REPO_ARCHIVE=1` is set on the target endpoint and `read_api` serves the archive there (their open item: on the read-only profile the resource may 403 — the tool must be pointed at the full profile if so).

- [ ] **Step 2: Drive one real checkout**

With a runtime config that sets `engine.repoCheckout.enabled: true` + `mcpServerId: <gitlab id>`, run the harness and send a gitlab MR webhook whose payload carries `object_attributes.last_commit.id`. In the agent turn, confirm `checkout_repo(project=<id>, ref=<sha>)` returns a path and that path contains the repo tree.

- [ ] **Step 3: Verify the negatives**

- Over-cap: request a large repo without `subpath` → confirm the tool returns `Checkout failed: ... exceeds cap ...` (not a crash) and the turn completes via file-read fallback.
- Cleanup: confirm stale dirs under `/tmp/gitlab` are swept on the next call, and `/tmp/gitlab` is gone after harness shutdown.

- [ ] **Step 4: Record the result**

Write a short decision-record at `docs/references/2026-07-06-gitlab-repo-checkout.md` (topology, resource contract, the tool, TTL-sweep cleanup, snapshot ceilings) and add its row to `docs/references/README.md`.

---

## Self-Review

**Spec coverage:** agent picks WHAT not WHERE (tool takes `project`/`ref`/`subpath`, harness owns the dir — Task 5) ✓; harness is the resource consumer, blob never in context (Task 3/5) ✓; ek harness-side as x-ach-key, never seen by agent (Task 3, Global Constraints) ✓; `mkdtemp` under `/tmp/gitlab` (Task 4) ✓; TTL sweep + shutdown rmtree — Option A cleanup (Task 4/5) ✓; head-SHA ref (Task 2) ✓; error-raises → fail-soft (Task 5) ✓; snapshot ceilings documented (Global Constraints, tool description) ✓; config flag + gitlab server id (Task 1/6) ✓; prerequisite + E2E gating (Global Constraints, Task 8) ✓.

**Placeholder scan:** every code step carries complete code; the only deliberately manual task is Task 8 (E2E), which is external-dependency-gated and says so.

**Type consistency:** `checkout_repo(project, ref, subpath)` and `_checkout(project, ref, subpath)` match across Tasks 5/7; `read_repo_archive(endpoint, ek, project, ref, subpath)` consistent Tasks 3/5; `extract_archive(data, tmp_base, project, ref)` and `sweep_stale(tmp_base, ttl_seconds, now)` consistent Tasks 4/5; `resolve_repo_archive_endpoint(mcp_servers, server_id)` consistent Task 6; `delivery_context["head_sha"]` produced Task 2, consumed Tasks 2/7.
