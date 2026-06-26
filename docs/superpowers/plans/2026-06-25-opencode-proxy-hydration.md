# Plan 2 — Localhost Proxy + Hydration + Context

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
>
> **Design-forward plan (new code).** Prereq: Plan 1 merged (tree green on opencode + v3 config).
>
> **Execution (see `README.md`).** Owns: `engine/hydrate.py`, `engine/context.py`, `engine/mcp_proxy.py` (new) + edits `engine/lifecycle.py` (`write_opencode_config`/`EngineConfig`) and **`main.py boot()`**. ⚠ Plan 3 also edits `main.py boot()` — **do NOT run Plan 2 and Plan 3 concurrently against the same base** (merge order 2→3→4). Parallel-safe *within* this plan: Tasks 1 (hydrate) + 2 (context) are independent standalone modules; Tasks 3-4 (proxy) then Task 5 (boot wiring) serialize after.

**Goal:** The harness self-hydrates from ACH and fronts the model + MCP traffic via a **localhost reverse-proxy** that injects the `ek_`, so opencode never sees the `ek_` or real ACH URLs.

**Architecture:** At boot the harness calls `POST {ACH_BASE_URL}/platform/hydrate` (`x-ach-key: ek_`) → manifest (`runtime.models/mcpServers/a2aAgents`, `context.skills/prompts/artifacts`). It resolves `model.name` against `runtime.models` (hard-fail if absent), downloads context tars into directories, starts a localhost proxy (model `/v1|/gemini|/anthropic` + one route per MCP server), and writes `opencode.json` pointing **only at localhost** (no `ek_`). `capability.filter.exclude` is applied before servers/tools are offered.

**Tech Stack:** Python 3.12, asyncio, httpx (async client + streaming), aiohttp (already a dep), Pydantic v2.

## Global Constraints

- `uv run` for everything; `make lint` green; router untouched.
- **The `ek_` MUST NOT appear in `opencode.json`, opencode's env, or logs** (this is the headline invariant — tested in Plan 4 §6.10, but honored here).
- Secrets read from mounted paths at use time; `ACH_TOKEN` (the `ek_`) is read by the proxy only.
- New dep allowed: `httpx` (already in the dev group — promote to `dependencies` if used at runtime).

---

### Task 1: Hydration client (`engine/hydrate.py`)

**Files:**
- Create: `src/ach_agent/engine/hydrate.py`
- Create: `tests/engine/test_hydrate.py`

**Interfaces:**
- Produces: `HydrationManifest` (Pydantic: `models: list[str]`, `mcp_servers: list[McpServer]`, `a2a_agents: list[A2AAgent]`, `context: Context`), `McpServer{id: str, endpoint: str}`, `async hydrate(base_url: str, ek: str) -> HydrationManifest`, `resolve_model(manifest, name: str) -> None` (hard-fail `sys.exit(1)` if `name not in manifest.models` **and** `manifest.models` is non-empty).

- [ ] **Step 1: Write the failing test**

```python
import json, pytest
from ach_agent.engine.hydrate import hydrate, resolve_model, HydrationManifest

SAMPLE = {
    "schemaVersion": "v1alpha1", "environment": "frontend-dev",
    "runtime": {"models": ["openai.gpt-5"],
                "mcpServers": [{"id": "mcp-gofetch", "endpoint": "https://ach/mcp/mcp-gofetch"}],
                "a2aAgents": []},
    "context": {"prompts": [], "plugins": [], "artifacts": [],
                "skills": [{"name": "frontend-design", "id": "fd", "downloadUrl": "https://ach/content/skill/fd"}]},
}

async def test_hydrate_parses_manifest(monkeypatch):
    async def fake_post(url, headers, manifest=SAMPLE):
        assert headers["x-ach-key"] == "ek-abc"
        return SAMPLE
    monkeypatch.setattr("ach_agent.engine.hydrate._post_hydrate", fake_post)
    m = await hydrate("https://ach.ackstorm.ai", "ek-abc")
    assert m.models == ["openai.gpt-5"]
    assert m.mcp_servers[0].id == "mcp-gofetch"
    assert m.context.skills[0].download_url.endswith("/skill/fd")

def test_resolve_model_hard_fails_when_absent():
    m = HydrationManifest.model_validate(SAMPLE)
    with pytest.raises(SystemExit):
        resolve_model(m, "gemini.not-there")

def test_resolve_model_ok_when_present():
    m = HydrationManifest.model_validate(SAMPLE)
    resolve_model(m, "openai.gpt-5")  # no raise
```

- [ ] **Step 2: Run to verify it fails** — `uv run pytest tests/engine/test_hydrate.py -q` → FAIL (module missing).

- [ ] **Step 3: Implement `hydrate.py`**

```python
from __future__ import annotations
import sys
import httpx
import structlog
from pydantic import BaseModel, Field

log = structlog.get_logger(__name__)

class McpServer(BaseModel):
    id: str
    endpoint: str

class A2AAgent(BaseModel):
    id: str = ""
    endpoint: str = ""

class ContextItem(BaseModel):
    name: str = ""
    id: str = ""
    download_url: str = Field(default="", alias="downloadUrl")

class Context(BaseModel):
    skills: list[ContextItem] = []
    prompts: list[ContextItem] = []
    artifacts: list[ContextItem] = []

class _Runtime(BaseModel):
    models: list[str] = []
    mcpServers: list[McpServer] = []
    a2aAgents: list[A2AAgent] = []

class HydrationManifest(BaseModel):
    environment: str = ""
    runtime: _Runtime = _Runtime()
    context: Context = Context()

    @property
    def models(self) -> list[str]: return self.runtime.models
    @property
    def mcp_servers(self) -> list[McpServer]: return self.runtime.mcpServers
    @property
    def a2a_agents(self) -> list[A2AAgent]: return self.runtime.a2aAgents

async def _post_hydrate(url: str, headers: dict[str, str]) -> dict:
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, headers=headers)
        r.raise_for_status()
        return r.json()

async def hydrate(base_url: str, ek: str) -> HydrationManifest:
    data = await _post_hydrate(f"{base_url.rstrip('/')}/platform/hydrate", {"x-ach-key": ek})
    return HydrationManifest.model_validate(data)

def resolve_model(manifest: HydrationManifest, name: str) -> None:
    if manifest.models and name not in manifest.models:
        log.error("model not in hydrated models — exiting", name=name, available=manifest.models)
        sys.exit(1)
```

- [ ] **Step 4: Run to verify pass** — `uv run pytest tests/engine/test_hydrate.py -q` → PASS.
- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(hydrate): POST /platform/hydrate client + model resolution"`

---

### Task 2: Context fetch (tar → directory)

**Files:** Create `src/ach_agent/engine/context.py`, `tests/engine/test_context.py`.

**Interfaces:** Produces `async fetch_context(ctx: Context, ek: str, root: Path) -> None` — for each item, GET `download_url` (`Authorization: Bearer ek`), stream the `tar.gz`, `tarfile.extractall` into `root/{skills|prompts|artifacts}/{name}/` (path-traversal guarded).

- [ ] **Step 1: Failing test** — build a tiny in-memory `.tar.gz`, monkeypatch the GET to return its bytes, assert files land under `root/skills/<name>/` and that a member with `../` is rejected.

```python
import io, tarfile, pytest
from pathlib import Path
from ach_agent.engine.context import fetch_context, _safe_extract
from ach_agent.engine.hydrate import Context, ContextItem

def _make_targz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name); info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()

async def test_fetch_context_extracts(monkeypatch, tmp_path):
    blob = _make_targz({"SKILL.md": "x"})
    async def fake_get(url, ek): return blob
    monkeypatch.setattr("ach_agent.engine.context._get_bytes", fake_get)
    ctx = Context(skills=[ContextItem(name="fd", id="fd", downloadUrl="https://ach/skill/fd")])
    await fetch_context(ctx, "ek", tmp_path)
    assert (tmp_path / "skills" / "fd" / "SKILL.md").read_text() == "x"

def test_safe_extract_rejects_traversal(tmp_path):
    info = tarfile.TarInfo("../evil")
    with pytest.raises(ValueError):
        _safe_extract([info], tmp_path)
```

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** `context.py` with `_get_bytes` (httpx GET + Bearer), `_safe_extract` (reject members whose resolved path escapes the dest), and `fetch_context` looping skills/prompts/artifacts into `root/<kind>/<name>/`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(context): fetch + safe-extract skills/prompts/artifacts tars"`

---

### Task 3: Localhost MCP proxy (front ACH MCP servers, inject ek)

**Files:** Create `src/ach_agent/engine/mcp_proxy.py`, `tests/engine/test_mcp_proxy.py`.

**Interfaces:** Produces `class McpProxy` with `async start(servers: list[McpServer], ek: str, exclude: set[str]) -> dict[str, str]` returning `{server_id: "http://127.0.0.1:<port>/mcp/<id>"}` (the localhost URLs to write into `opencode.json`), and `async stop()`. Each request to a localhost route is forwarded to the server's real `endpoint` with `Authorization: Bearer ek` added. Servers whose `id` is in `exclude` are not started.

- [ ] **Step 1: Failing test** — start the proxy with a fake upstream (a local aiohttp app that echoes the `Authorization` header), POST to the localhost route, assert the upstream saw `Bearer ek` and the localhost URL contains no `ek`. Assert an excluded server id yields no route.

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** a minimal aiohttp reverse-proxy app bound to `127.0.0.1` on an ephemeral port: one catch-all route per server id that streams method/headers/body to `endpoint`, **adds** `Authorization: Bearer {ek}`, and streams the response back (including SSE/`text/event-stream`). Keep the `ek` only in the proxy closure. Apply `exclude`.
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(proxy): localhost MCP reverse-proxy injecting ek"`

---

### Task 4: Localhost model proxy (/v1,/gemini,/anthropic → ACH, inject ek)

**Files:** Extend `src/ach_agent/engine/mcp_proxy.py` (or new `model_proxy.py`), `tests/engine/test_model_proxy.py`.

**Interfaces:** Produces `async start_model_proxy(ach_base_url: str, ek: str) -> str` returning `"http://127.0.0.1:<port>"`; routes `/v1/*`, `/gemini/*`, `/anthropic/*` forward to `{ach_base_url}/<same path>` with `Authorization: Bearer ek`, **streaming** responses (SSE for `/v1/responses`).

- [ ] **Step 1: Failing test** — fake ACH upstream that streams 3 SSE chunks and echoes the auth header; assert the proxy streams all 3 chunks through and the upstream saw `Bearer ek`, and the returned base URL has no `ek`.
- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the streaming reverse-proxy (reuse the aiohttp app from Task 3; add the three path prefixes). Critical: do **not** buffer — stream the upstream response body chunk-by-chunk (`StreamResponse`).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "feat(proxy): localhost model reverse-proxy (v1/gemini/anthropic) injecting ek"`

---

### Task 5: Wire hydration + proxies into boot; point opencode.json at localhost; assert ek-hygiene

**Files:** Modify `src/ach_agent/main.py` (boot), `src/ach_agent/engine/lifecycle.py` (`write_opencode_config`), `tests/test_main_wiring.py`.

**Interfaces:** Consumes Tasks 1-4. `write_opencode_config` now takes localhost model `baseURL` + localhost MCP URLs and writes **no** `ek` and **no** ACH URLs.

- [ ] **Step 1: Write the ek-hygiene test (the headline gate)**

```python
def test_opencode_json_never_contains_ek(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_TOKEN", "ek-secret-xyz")
    from ach_agent.engine.lifecycle import write_opencode_config, EngineConfig
    cfg = EngineConfig(model="openai.gpt-5", provider="openai",
                       model_base_url="http://127.0.0.1:9001/v1",
                       mcp_local_urls={"mcp-gofetch": "http://127.0.0.1:9002/mcp/mcp-gofetch"})
    write_opencode_config(tmp_path, cfg)
    blob = (tmp_path / ".config" / "opencode" / "opencode.json").read_text()
    assert "ek-secret-xyz" not in blob
    assert "ach.ackstorm.ai" not in blob
    assert "127.0.0.1" in blob
```

- [ ] **Step 2: Run → FAIL** (EngineConfig has no `model_base_url`/`mcp_local_urls`; opencode.json still uses `{env:ACH_API_KEY}`/`{env:ACH_BASE_URL}`).

- [ ] **Step 3: Implement** — add `model_base_url: str` and `mcp_local_urls: dict[str,str]` to `EngineConfig`; in `write_opencode_config` set `provider[...]["options"]["baseURL"] = config.model_base_url` and **drop** the `apiKey` `{env:...}` line (the local proxy needs no key, or a dummy); write `mcp.servers.<id> = {type: "streamable-http", url: <local url>}` from `mcp_local_urls`. In `main.py boot()`: read `ek = read_secret(ACH_TOKEN path)`; `manifest = await hydrate(cfg.capability.ach.base_url, ek)`; `resolve_model(manifest, cfg.model.name)`; `await fetch_context(manifest.context, ek, Path(cfg.persistence.mount_path))`; `mcp_urls = await McpProxy().start(manifest.mcp_servers, ek, exclude=set(cfg.capability.filter.exclude.mcp_servers))`; `model_url = await start_model_proxy(cfg.capability.ach.base_url, ek)`; build `EngineConfig(..., model_base_url=f"{model_url}/{endpoint_for(cfg.model.type)}", mcp_local_urls=mcp_urls)`. Apply `exclude.tools` later in opencode (or document as Plan 3/4 follow-up).

- [ ] **Step 4: Run → PASS** — `uv run pytest tests/test_main_wiring.py::test_opencode_json_never_contains_ek tests/engine -q`.
- [ ] **Step 5: Run `make lint` + full non-e2e suite → PASS.**
- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(boot): hydrate + localhost proxies; opencode.json points at localhost (ek-hygiene)"`

---

## Self-Review

**Coverage:** hydration via `/platform/hydrate` ✓ T1, model resolution hard-fail ✓ T1, context tar→dir ✓ T2, MCP proxy ✓ T3, model proxy ✓ T4, ek-hygiene + boot wiring ✓ T5. **Verify against live ACH:** the model-proxy path prefix per `type` (`endpoint_for`), the opencode model-string under litellm, and `/platform/hydrate` field names (sampled from the user's curl) — confirm on first real run. **Deferred:** `exclude.tools` enforcement (opencode-side) lands with Plan 3/4; `exclude.skills` applied when context is wired into opencode skills dir (Plan 3).

**Type consistency:** `HydrationManifest.mcp_servers: list[McpServer]` → `McpProxy.start(...) -> dict[id,url]` → `EngineConfig.mcp_local_urls` → `write_opencode_config`. `Context` → `fetch_context`.
