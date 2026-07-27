# Hindsight Memory Facade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop exposing the raw Hindsight MCP (30 tools, incl. `delete_bank`) to the agent; instead the harness hosts a 4-tool memory facade that injects `bank_id` + admin auth and proxies to Hindsight, and the harness provisions banks + mental models at boot.

**Architecture:** The harness talks to Hindsight directly with an **admin secret** (Bearer), never the `ek_`. Two harness-side responsibilities: (1) **boot provisioning** — `create_bank` + `create_mental_model` + `refresh_mental_model` from rich config specs; (2) an **in-process FastMCP facade** on `127.0.0.1` exposing only `memory_recall`/`memory_reflect`/`memory_retain`/`memory_get_mental_model` to opencode, each mapping to the real `hindsight_*` tool with `bank_id` injected. opencode's `memory-0` MCP server points at the facade URL, not Hindsight. The agent never sees `bank_id`, the admin secret, or any admin/destructive tool.

**Tech Stack:** Python 3.12 + asyncio, Pydantic v2 (config), `mcp` SDK (`FastMCP` server side + `streamable_http_client` client side), uvicorn (host the facade ASGI app), structlog.

## Global Constraints

- Always `uv run ...`; never system pip. Lint gate: `make lint` = ruff + `ruff format --check` + `mypy --strict` over all of `src/`.
- **Fail-open memory (MEM-02 / D-02):** any memory error (unreachable, unset secret, bad response) must degrade to "run without memory" — NEVER raise into the turn or crash boot. Swallow + WARN + increment `MEMORY_DEGRADED`.
- **Secret hygiene:** the admin secret is env-only via `SecretSource` (`{env: NAME}`), resolved at use time with `resolve_secret`, never cached, never logged, never written to `opencode.json` or forwarded to opencode. Same discipline as the `ek_`.
- **Auth is OPTIONAL** (omitted → no auth header; the harness reaches Hindsight over an internal/cluster URL). When present, scheme is assumed Bearer: `Authorization: Bearer <secret>`, isolated to one helper (`hindsight_auth_headers`). Distinguish two cases: `auth` absent → run unauthenticated (fine); `auth` present but its env var unset → misconfig → fail-open degrade (do NOT silently drop the intended auth).
- **Hindsight tool names are `hindsight_*`** on the live deployment (verified). Kept as module constants + a boot `list_tools` probe logs the actual names so a mismatch is caught, not silently 404'd.
- Breaking CONTRACT change (`mentalModels` shape + new required `auth`). Requires `make schema` regen; the operator repo (`ach-runtime`) CRD render is a SEPARATE cross-repo change (Task 6, not built here).
- TDD, DRY, YAGNI, frequent commits. All memory calls route through ONE seam (`call_hindsight`) so tests monkeypatch a single function.

---

## File Structure

- `src/ach_agent/config/schema.py` — MODIFY: add `MentalModelSpec`; change `HindsightParams.mental_models` to `list[MentalModelSpec]`; add `HindsightParams.auth: SecretSource` (required) and `HindsightParams.mission: str`.
- `src/ach_agent/memory/hindsight.py` — MODIFY: add the `call_hindsight` seam + `hindsight_auth_headers` + `HINDSIGHT_*` tool-name constants + `provision_memory`; refactor `fetch_mental_model_summaries` / `prepare_memory` to use the seam, admin auth, and the corrected tool name; update `TOOLS_SPEC` (drop `bank_id`, add `tags`).
- `src/ach_agent/memory/facade.py` — CREATE: `MemoryFacade` (FastMCP host + 4 tools + `bank_id`/auth injection + `hindsight_*` mapping; `start()`/`stop()`).
- `src/ach_agent/main.py` — MODIFY: boot `provision_memory`; start/stop the facade beside the existing proxies; thread `facade_url` into `select_memory_wiring_async`.
- `docs/schemas/agent-config-v1.schema.json` — REGEN via `make schema`.
- `docs/plan/CONTRACT_v3.md` + `docs/references/` — MODIFY/CREATE: doc the new memory contract + an ADR; flag the `ach-runtime` CRD change.
- Tests: `tests/config/test_hindsight_schema.py`, `tests/memory/test_hindsight_client.py`, `tests/memory/test_provision.py`, `tests/memory/test_facade.py`, `tests/memory/test_wiring.py` (all new).

---

### Task 1: Schema — rich `mentalModels`, required `auth`, `mission`

**Files:**
- Modify: `src/ach_agent/config/schema.py` (`HindsightParams` at lines 191-201; `SecretSource` already at 318)
- Test: `tests/config/test_hindsight_schema.py`
- Regen: `docs/schemas/agent-config-v1.schema.json`

**Interfaces:**
- Consumes: `SecretSource` (schema.py:318), `resolve_secret` (schema.py:339).
- Produces:
  - `class MentalModelSpec(BaseModel)` with fields `id: str`, `name: str`, `source_query: str` (alias `sourceQuery`), `auto_refresh: bool = False` (alias `autoRefresh`), `max_tokens: int = 2048` (alias `maxTokens`).
  - `HindsightParams.mental_models: list[MentalModelSpec]` (alias `mentalModels`, default `[]`).
  - `HindsightParams.auth: SecretSource | None = None` (OPTIONAL — omit for an internal/no-auth URL).
  - `HindsightParams.mission: str = ""`.

- [ ] **Step 1: Write the failing test**

```python
# tests/config/test_hindsight_schema.py
import pytest
from pydantic import ValidationError
from ach_agent.config.schema import HindsightParams


def _base(**over):
    d = {
        "endpoint": "https://hs.example/mcp",
        "bank": "gitlab-pr-review",
        "auth": {"env": "HINDSIGHT_ADMIN_TOKEN"},
        "mentalModels": [
            {"id": "architecture", "name": "Arch", "sourceQuery": "What is the architecture?"}
        ],
    }
    d.update(over)
    return d


def test_rich_mental_models_parse_with_aliases():
    p = HindsightParams.model_validate(_base())
    assert p.auth is not None and p.auth.env == "HINDSIGHT_ADMIN_TOKEN"
    assert p.mission == ""
    mm = p.mental_models[0]
    assert (mm.id, mm.name, mm.source_query) == ("architecture", "Arch", "What is the architecture?")
    assert mm.auto_refresh is False and mm.max_tokens == 2048


def test_auth_optional_defaults_none():
    d = _base()
    del d["auth"]
    p = HindsightParams.model_validate(d)  # internal URL — no auth needed
    assert p.auth is None


def test_legacy_string_mental_models_rejected():
    with pytest.raises(ValidationError):
        HindsightParams.model_validate(_base(mentalModels=["architecture", "conventions"]))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_hindsight_schema.py -q`
Expected: FAIL — today `auth`/`mission` are not fields (`extra='forbid'` rejects the keys) and `mentalModels` is `list[str]`, so all three tests fail.

- [ ] **Step 3: Write minimal implementation**

Replace `HindsightParams` (schema.py:191-201) and add `MentalModelSpec` immediately above it:

```python
class MentalModelSpec(BaseModel):
    """A pinned reflection the harness provisions into Hindsight at boot (CONTRACT §2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    name: str
    source_query: str = Field(alias="sourceQuery")
    auto_refresh: bool = Field(default=False, alias="autoRefresh")
    max_tokens: int = Field(default=2048, alias="maxTokens")


class HindsightParams(BaseModel):
    """Hindsight backend params — the ``memory.hindsight`` sub-block (CONTRACT §2)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str
    # Static memory bank_id (harness-owned; the agent never sees or sets it). Per-repo
    # partitioning is via tags, NEVER by templating bank from inbound payload (T-04-03).
    bank: str = ""
    # Admin secret for the harness→Hindsight path (Bearer). NOT the ek_. env-only; resolved
    # at use time; never logged / forwarded to opencode. OPTIONAL — omit when Hindsight is on
    # an internal/no-auth URL. If set but the env var is unset at runtime → fail-open degrade.
    auth: SecretSource | None = None
    # Optional mission string passed to create_bank at provisioning.
    mission: str = ""
    # Rich specs the harness provisions (create_mental_model) + reads (get_mental_model).
    mental_models: list[MentalModelSpec] = Field(default_factory=list, alias="mentalModels")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/config/test_hindsight_schema.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Regenerate the JSON schema + verify no drift**

Run: `uv run python scripts/gen_schema.py && uv run pytest tests/config/test_schema_artifact.py -q`
Expected: `agent-config-v1.schema.json` rewritten; artifact test PASS.

- [ ] **Step 6: Typecheck**

Run: `uv run mypy --strict src/ach_agent/config/schema.py`
Expected: no errors.

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/config/schema.py tests/config/test_hindsight_schema.py docs/schemas/agent-config-v1.schema.json
git commit -m "feat(memory): rich mentalModels specs + required admin auth on hindsight config"
```

---

### Task 2: Hindsight client seam — auth headers, correct tool names, refactor reads

**Files:**
- Modify: `src/ach_agent/memory/hindsight.py` (imports at 69-70; `fetch_mental_model_summaries` at 58-94; `prepare_memory` at 97-141; `TOOLS_SPEC` at 29-34)
- Test: `tests/memory/test_hindsight_client.py`

**Interfaces:**
- Consumes: `resolve_secret` (schema.py:339), `HindsightMemory`/`HindsightParams` (schema.py), `streamable_http_client` (mcp SDK, accepts `headers=`).
- Produces:
  - `hindsight_auth_headers(secret: str | None) -> dict[str, str]` → `{"Authorization": f"Bearer {secret}"}` when `secret` truthy, else `{}` (internal/no-auth URL).
  - `async def call_hindsight(endpoint: str, secret: str | None, tool: str, args: dict[str, object]) -> str` — opens one streamable-http MCP session (auth header only when `secret` set), calls `tool` with `args`, returns the first text content (`""` if none). The ONE seam every harness→Hindsight call routes through.
  - `resolve_memory_secret(params: HindsightParams) -> tuple[bool, str | None]` — DRY gate reused by prepare/provision/main. Returns `(True, None)` when `auth` is absent (no-auth URL), `(False, None)` when `auth` is set but its env is unset (misconfig → degrade), `(True, secret)` when resolved.
  - Constants: `HINDSIGHT_RECALL="hindsight_recall"`, `HINDSIGHT_REFLECT="hindsight_reflect"`, `HINDSIGHT_RETAIN="hindsight_retain"`, `HINDSIGHT_GET_MENTAL_MODEL="hindsight_get_mental_model"`, `HINDSIGHT_CREATE_BANK="hindsight_create_bank"`, `HINDSIGHT_CREATE_MENTAL_MODEL="hindsight_create_mental_model"`, `HINDSIGHT_REFRESH_MENTAL_MODEL="hindsight_refresh_mental_model"`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_hindsight_client.py
import pytest
from ach_agent.memory import hindsight as hs


def test_auth_headers_bearer():
    assert hs.hindsight_auth_headers("sekret") == {"Authorization": "Bearer sekret"}


def test_auth_headers_empty_when_no_secret():
    assert hs.hindsight_auth_headers(None) == {}  # internal URL — no auth


@pytest.mark.asyncio
async def test_call_hindsight_passes_headers_and_returns_text(monkeypatch):
    seen = {}

    class _Content:
        text = "OK-BODY"

    class _Result:
        content = [_Content()]

    class _Session:
        def __init__(self, *a):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def initialize(self):
            pass
        async def call_tool(self, tool, args):
            seen["tool"] = tool
            seen["args"] = args
            return _Result()

    class _Client:
        def __init__(self, url, headers=None, **k):
            seen["url"] = url
            seen["headers"] = headers
        async def __aenter__(self):
            return (object(), object(), object())
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(hs, "streamable_http_client", _Client)
    monkeypatch.setattr(hs, "ClientSession", lambda read, write: _Session())

    out = await hs.call_hindsight("https://hs/mcp", "sekret", hs.HINDSIGHT_RECALL, {"query": "q"})
    assert out == "OK-BODY"
    assert seen["url"] == "https://hs/mcp"
    assert seen["headers"] == {"Authorization": "Bearer sekret"}
    assert seen["tool"] == "hindsight_recall"
    assert seen["args"] == {"query": "q"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/memory/test_hindsight_client.py -q`
Expected: FAIL — `hindsight_auth_headers` / `call_hindsight` / constants don't exist; `ClientSession` not a module attribute.

- [ ] **Step 3: Write minimal implementation**

At the top of `hindsight.py`, hoist the mcp imports to module level (so tests can monkeypatch them) and add the constants + seam. Replace the local imports inside `fetch_mental_model_summaries` (lines 69-70) with module-level ones:

```python
# near the top of the module, after `import structlog`
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

# tool names on the live Hindsight deployment (verified 2026-07-05). A boot list_tools
# probe (facade) logs the real names so a rename is caught, not silently 404'd.
HINDSIGHT_RECALL = "hindsight_recall"
HINDSIGHT_REFLECT = "hindsight_reflect"
HINDSIGHT_RETAIN = "hindsight_retain"
HINDSIGHT_GET_MENTAL_MODEL = "hindsight_get_mental_model"
HINDSIGHT_CREATE_BANK = "hindsight_create_bank"
HINDSIGHT_CREATE_MENTAL_MODEL = "hindsight_create_mental_model"
HINDSIGHT_REFRESH_MENTAL_MODEL = "hindsight_refresh_mental_model"


def hindsight_auth_headers(secret: str | None) -> dict[str, str]:
    """Admin auth header (assumed Bearer). Empty when no secret — internal/no-auth URL."""
    return {"Authorization": f"Bearer {secret}"} if secret else {}


async def call_hindsight(
    endpoint: str, secret: str | None, tool: str, args: dict[str, object]
) -> str:
    """Call one Hindsight MCP tool; return first text content ('' if none).

    The single harness→Hindsight seam (probe/fetch/provision/facade all route here so tests
    monkeypatch one function). ``secret`` (if any) is used only to build headers — never logged.
    """
    headers = hindsight_auth_headers(secret)
    async with streamable_http_client(endpoint, headers=headers or None) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool, args)
            return getattr(result.content[0], "text", "") if result.content else ""


def resolve_memory_secret(params: "HindsightParams") -> tuple[bool, str | None]:
    """(ok, secret) gate reused by prepare/provision/main.

    (True, None)  → no auth configured (internal URL) — proceed unauthenticated.
    (False, None) → auth configured but env unset (misconfig) — caller degrades.
    (True, secret)→ auth resolved — proceed with Bearer.
    """
    from ach_agent.config.schema import resolve_secret

    if params.auth is None:
        return True, None
    secret = resolve_secret(params.auth)
    return (False, None) if secret is None else (True, secret)
```

`HindsightParams` is only needed for the annotation — import it under `TYPE_CHECKING` (already present at the top of the module).

Then rewrite `fetch_mental_model_summaries` to take the secret + route through the seam + use the corrected tool name, and delete its local mcp imports:

```python
async def fetch_mental_model_summaries(
    endpoint: str,
    secret: str,
    bank_id: str,
    mental_model_ids: list[str],
) -> str:
    """Fetch mental model summaries and return a '## Memory\\n...' section string.

    Partial failures (single model unreachable): log warning + skip, never raise.
    Returns '## Memory\\n\\nUnavailable' if all fetches fail or the id list is empty.
    """
    sections: list[str] = []
    for mid in mental_model_ids:
        try:
            text = await call_hindsight(
                endpoint,
                secret,
                HINDSIGHT_GET_MENTAL_MODEL,
                {"bank_id": bank_id, "mental_model_id": mid},
            )
            if text:
                sections.append(f"### {mid}\n{text}")
        except Exception as exc:
            log.warning("memory: mental model fetch failed — skipping", model=mid, error=str(exc))
    if sections:
        return "## Memory\n\n" + "\n\n".join(sections)
    return "## Memory\n\nUnavailable"
```

Update `prepare_memory` (lines 112-132) to resolve the secret and pass ids + secret. Degrade if the secret is unset:

```python
    try:
        params = memory_cfg.hindsight
        bank_id = params.bank
        ok, secret = resolve_memory_secret(params)
        if not ok:
            log.warning("memory: auth configured but env unset — running degraded", bank_id=bank_id)
            _inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (auth unset)."

        available = await probe_memory_endpoint(params.endpoint)
        if not available:
            log.warning(
                "memory backend unreachable — running degraded (MEM-02, D-02)",
                endpoint=params.endpoint,
                bank_id=bank_id,
            )
            _inc_memory_degraded()
            return False, "## Memory\n\nUnavailable (backend unreachable)."

        log.info("memory: hindsight backend active", endpoint=params.endpoint, bank_id=bank_id)
        prompt_section = await fetch_mental_model_summaries(
            endpoint=params.endpoint,
            secret=secret,
            bank_id=bank_id,
            mental_model_ids=[m.id for m in params.mental_models],
        )
        return True, prompt_section
```

Update `TOOLS_SPEC` (lines 29-34) to drop `bank_id` (harness-injected now) and mention tags:

```python
TOOLS_SPEC = """\
Memory tools (the harness fills the memory bank for you — do NOT pass a bank id):
- `memory_recall(query, tags?)`: search past memories by topic or filename.
- `memory_reflect(query, tags?)`: synthesize across memories — patterns, not single facts.
- `memory_get_mental_model(mental_model_id)`: read a pre-built summary (ids are in the ## Memory section).
- `memory_retain(content, tags?)`: store an insight for future sessions. Tag it (e.g. tags=["repo:<name>"])."""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/memory/test_hindsight_client.py -q`
Expected: PASS (2 tests).

- [ ] **Step 5: Run the existing memory tests + typecheck**

Run: `uv run pytest tests/memory -q && uv run mypy --strict src/ach_agent/memory/hindsight.py`
Expected: PASS. If any existing test calls `fetch_mental_model_summaries` with the old 3-arg signature, update that call to pass `secret` (it is now 4-arg).

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/memory/hindsight.py tests/memory/test_hindsight_client.py
git commit -m "feat(memory): admin-authed hindsight seam + correct tool names, drop bank_id from tools spec"
```

---

### Task 3: Boot provisioning — `provision_memory`

**Files:**
- Modify: `src/ach_agent/memory/hindsight.py` (append `provision_memory`)
- Test: `tests/memory/test_provision.py`

**Interfaces:**
- Consumes: `call_hindsight`, `resolve_memory_secret`, the `HINDSIGHT_CREATE_*`/`HINDSIGHT_REFRESH_*` constants (Task 2), `HindsightMemory`.
- Produces: `async def provision_memory(memory_cfg: "Memory | None") -> None` — idempotent, fail-open, called ONCE at boot. No-op unless `memory_cfg` is `HindsightMemory`; skips (WARN) only if `auth` is configured but its env is unset. Ensures the bank, then creates each mental model, then refreshes the `auto_refresh` ones.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_provision.py
import pytest
from ach_agent.config.schema import HindsightMemory
from ach_agent.memory import hindsight as hs


def _cfg():
    return HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "https://hs/mcp",
                "bank": "bank-1",
                "mission": "reviewer",
                "auth": {"env": "HS_TOK"},
                "mentalModels": [
                    {"id": "arch", "name": "Arch", "sourceQuery": "arch?", "autoRefresh": True},
                    {"id": "conv", "name": "Conv", "sourceQuery": "conv?"},
                ],
            },
        }
    )


@pytest.mark.asyncio
async def test_provision_creates_bank_models_and_refreshes(monkeypatch):
    monkeypatch.setenv("HS_TOK", "sekret")
    calls = []

    async def fake_call(endpoint, secret, tool, args):
        calls.append((tool, args))
        return "{}"

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(_cfg())

    tools = [t for t, _ in calls]
    assert tools.count(hs.HINDSIGHT_CREATE_BANK) == 1
    assert tools.count(hs.HINDSIGHT_CREATE_MENTAL_MODEL) == 2
    # only the auto_refresh model (arch) is refreshed
    refresh = [a for t, a in calls if t == hs.HINDSIGHT_REFRESH_MENTAL_MODEL]
    assert len(refresh) == 1 and refresh[0]["mental_model_id"] == "arch"
    # bank_id injected everywhere
    assert all("bank_id" in a for _, a in calls)


@pytest.mark.asyncio
async def test_provision_skips_when_auth_configured_but_unset(monkeypatch):
    monkeypatch.delenv("HS_TOK", raising=False)  # cfg has auth={env:HS_TOK} but it's unset
    called = False

    async def fake_call(*a, **k):
        nonlocal called
        called = True
        return ""

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(_cfg())  # must not raise
    assert called is False


@pytest.mark.asyncio
async def test_provision_proceeds_with_no_auth(monkeypatch):
    """No auth field (internal URL) → provisions with secret=None."""
    cfg = HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "http://hindsight.svc/mcp",
                "bank": "b",
                "mentalModels": [{"id": "arch", "name": "Arch", "sourceQuery": "arch?"}],
            },
        }
    )
    secrets_seen = []

    async def fake_call(endpoint, secret, tool, args):
        secrets_seen.append(secret)
        return "{}"

    monkeypatch.setattr(hs, "call_hindsight", fake_call)
    await hs.provision_memory(cfg)
    assert secrets_seen and all(s is None for s in secrets_seen)  # unauthenticated


@pytest.mark.asyncio
async def test_provision_swallows_errors(monkeypatch):
    monkeypatch.setenv("HS_TOK", "sekret")

    async def boom(*a, **k):
        raise RuntimeError("hindsight down")

    monkeypatch.setattr(hs, "call_hindsight", boom)
    await hs.provision_memory(_cfg())  # fail-open: no raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/memory/test_provision.py -q`
Expected: FAIL — `provision_memory` does not exist.

- [ ] **Step 3: Write minimal implementation**

Append to `hindsight.py`:

```python
async def provision_memory(memory_cfg: object) -> None:
    """Provision the bank + mental models in Hindsight (boot-once, idempotent, fail-open).

    No-op unless ``memory_cfg`` is a HindsightMemory with a resolvable admin secret.
    Never raises — a provisioning failure degrades memory, it does not stop boot.
    """
    from ach_agent.config.schema import HindsightMemory

    if not isinstance(memory_cfg, HindsightMemory):
        return
    params = memory_cfg.hindsight
    ok, secret = resolve_memory_secret(params)  # secret may be None (internal URL)
    if not ok:
        log.warning("memory: auth configured but env unset — skipping provisioning", bank_id=params.bank)
        return

    try:
        await call_hindsight(
            params.endpoint,
            secret,
            HINDSIGHT_CREATE_BANK,
            {"bank_id": params.bank, "name": params.bank, "mission": params.mission or None},
        )
        for spec in params.mental_models:
            try:
                await call_hindsight(
                    params.endpoint,
                    secret,
                    HINDSIGHT_CREATE_MENTAL_MODEL,
                    {
                        "bank_id": params.bank,
                        "name": spec.name,
                        "source_query": spec.source_query,
                        "mental_model_id": spec.id,
                        "max_tokens": spec.max_tokens,
                        "trigger_refresh_after_consolidation": spec.auto_refresh,
                    },
                )
            except Exception as exc:  # one bad model must not abort the rest
                log.warning("memory: create_mental_model failed", model=spec.id, error=str(exc))
        for spec in params.mental_models:
            if spec.auto_refresh:
                try:
                    await call_hindsight(
                        params.endpoint,
                        secret,
                        HINDSIGHT_REFRESH_MENTAL_MODEL,
                        {"bank_id": params.bank, "mental_model_id": spec.id},
                    )
                except Exception as exc:
                    log.warning("memory: refresh_mental_model failed", model=spec.id, error=str(exc))
        log.info("memory: provisioning complete", bank_id=params.bank, models=len(params.mental_models))
    except Exception as exc:  # ensure_bank failed → degrade, never raise
        log.warning("memory: provisioning failed — running degraded", bank_id=params.bank, error=str(exc))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/memory/test_provision.py -q`
Expected: PASS (3 tests).

- [ ] **Step 5: Typecheck**

Run: `uv run mypy --strict src/ach_agent/memory/hindsight.py`
Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/memory/hindsight.py tests/memory/test_provision.py
git commit -m "feat(memory): boot-once provision_memory (ensure_bank + create/refresh mental models, fail-open)"
```

---

### Task 4: The facade MCP server — `memory/facade.py`

**Files:**
- Create: `src/ach_agent/memory/facade.py`
- Test: `tests/memory/test_facade.py`

**Interfaces:**
- Consumes: `call_hindsight` + `HINDSIGHT_RECALL/REFLECT/RETAIN/GET_MENTAL_MODEL` (Task 2), `FastMCP` (mcp SDK), `uvicorn`.
- Produces:
  - `class MemoryFacade` with `__init__(self, endpoint: str, secret: str | None, bank_id: str)` (secret `None` → unauthenticated internal URL), `async def start(self) -> str` (returns `http://127.0.0.1:<port>/mcp`), `async def stop(self) -> None`, and an internal `async def _invoke(self, tool: str, args: dict[str, object]) -> str` that injects `bank_id` and calls `call_hindsight`. Registers exactly 4 FastMCP tools: `memory_recall`, `memory_reflect`, `memory_get_mental_model`, `memory_retain`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_facade.py
import asyncio
import socket
import pytest
from ach_agent.memory import hindsight as hs
from ach_agent.memory.facade import MemoryFacade


@pytest.mark.asyncio
async def test_invoke_injects_bank_id_and_maps_tool(monkeypatch):
    seen = {}

    async def fake_call(endpoint, secret, tool, args):
        seen["endpoint"] = endpoint
        seen["secret"] = secret
        seen["tool"] = tool
        seen["args"] = args
        return "RESULT"

    monkeypatch.setattr("ach_agent.memory.facade.call_hindsight", fake_call)
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    out = await f._invoke(hs.HINDSIGHT_RECALL, {"query": "q", "tags": ["repo:x"]})
    assert out == "RESULT"
    assert seen["tool"] == "hindsight_recall"
    assert seen["args"] == {"bank_id": "bank-1", "query": "q", "tags": ["repo:x"]}
    assert seen["secret"] == "sekret"


@pytest.mark.asyncio
async def test_registers_exactly_four_tools():
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    tools = await f._mcp.list_tools()
    names = sorted(t.name for t in tools)
    assert names == [
        "memory_get_mental_model",
        "memory_recall",
        "memory_reflect",
        "memory_retain",
    ]


@pytest.mark.asyncio
async def test_start_returns_reachable_url_and_stop_tears_down():
    f = MemoryFacade("https://hs/mcp", "sekret", "bank-1")
    url = await f.start()
    assert url.startswith("http://127.0.0.1:") and url.endswith("/mcp")
    port = int(url.split(":")[2].split("/")[0])
    with socket.create_connection(("127.0.0.1", port), timeout=2):
        pass  # connect succeeds → listening
    await f.stop()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/memory/test_facade.py -q`
Expected: FAIL — `ach_agent.memory.facade` does not exist.

- [ ] **Step 3: Write minimal implementation**

```python
# src/ach_agent/memory/facade.py
# SPDX-License-Identifier: Apache-2.0
"""Harness-hosted memory MCP facade.

Fronts Hindsight for opencode on 127.0.0.1, exposing ONLY four agent-facing tools
(recall/reflect/get_mental_model/retain). Each call injects the harness-owned ``bank_id``
and the admin auth secret, then maps to the real ``hindsight_*`` tool. The agent never sees
``bank_id``, the admin secret, or any admin/destructive Hindsight tool.

opencode's ``memory-0`` MCP server points at this facade's URL, not at Hindsight.
"""

from __future__ import annotations

import asyncio

import structlog
import uvicorn
from mcp.server.fastmcp import FastMCP

from ach_agent.memory.hindsight import (
    HINDSIGHT_GET_MENTAL_MODEL,
    HINDSIGHT_RECALL,
    HINDSIGHT_REFLECT,
    HINDSIGHT_RETAIN,
    call_hindsight,
)

log = structlog.get_logger(__name__)


class MemoryFacade:
    """FastMCP server exposing 4 memory tools; proxies to Hindsight with bank_id + auth."""

    def __init__(self, endpoint: str, secret: str | None, bank_id: str) -> None:
        self._endpoint = endpoint
        self._secret = secret  # closure-only, never logged; None → internal/no-auth URL
        self._bank_id = bank_id
        self._mcp = FastMCP("ach-memory")
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._register_tools()

    async def _invoke(self, tool: str, args: dict[str, object]) -> str:
        """Inject bank_id + call the mapped Hindsight tool. Fail-soft: return a short note."""
        try:
            return await call_hindsight(
                self._endpoint, self._secret, tool, {"bank_id": self._bank_id, **args}
            )
        except Exception as exc:
            log.warning("memory facade: hindsight call failed", tool=tool, error=str(exc))
            return "Memory temporarily unavailable."

    def _register_tools(self) -> None:
        @self._mcp.tool(name="memory_recall", description="Search past memories by topic or filename.")
        async def memory_recall(query: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_RECALL, {"query": query, "tags": tags})

        @self._mcp.tool(name="memory_reflect", description="Synthesize across memories — patterns, not single facts.")
        async def memory_reflect(query: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_REFLECT, {"query": query, "tags": tags})

        @self._mcp.tool(name="memory_get_mental_model", description="Read a pre-built mental-model summary by id.")
        async def memory_get_mental_model(mental_model_id: str) -> str:
            return await self._invoke(HINDSIGHT_GET_MENTAL_MODEL, {"mental_model_id": mental_model_id})

        @self._mcp.tool(name="memory_retain", description="Store an insight for future sessions. Tag it, e.g. tags=['repo:<name>'].")
        async def memory_retain(content: str, tags: list[str] | None = None) -> str:
            return await self._invoke(HINDSIGHT_RETAIN, {"content": content, "tags": tags})

    async def start(self) -> str:
        """Bind the facade on an ephemeral localhost port; return its MCP URL."""
        config = uvicorn.Config(
            self._mcp.streamable_http_app(), host="127.0.0.1", port=0, log_level="warning"
        )
        self._server = uvicorn.Server(config)
        self._task = asyncio.create_task(self._server.serve())
        while not self._server.started:  # bounded: uvicorn flips this within ~ms of bind
            await asyncio.sleep(0.02)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        log.info("memory facade started", port=port, bank_id=self._bank_id)
        return f"http://127.0.0.1:{port}/mcp"

    async def stop(self) -> None:
        """Signal uvicorn to exit and await the serve task."""
        if self._server is not None:
            self._server.should_exit = True
        if self._task is not None:
            await self._task
        self._server = None
        self._task = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/memory/test_facade.py -q`
Expected: PASS (3 tests). If `list_tools()` is not awaitable in the installed SDK, replace the Step-1 assertion with `sorted(f._mcp._tool_manager.list_tools()...)`; verify the actual API first with `uv run python -c "from mcp.server.fastmcp import FastMCP; import inspect; print(inspect.iscoroutinefunction(FastMCP('x').list_tools))"`.

- [ ] **Step 5: Typecheck**

Run: `uv run mypy --strict src/ach_agent/memory/facade.py`
Expected: no errors. (If `self._server.servers[0].sockets[0]` trips mypy, annotate the port line with a local `# type: ignore[union-attr]` — uvicorn's `servers` is loosely typed.)

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/memory/facade.py tests/memory/test_facade.py
git commit -m "feat(memory): harness-hosted MCP facade (4 tools, bank_id + admin auth injection)"
```

---

### Task 5: Wire facade + provisioning into `main.py`

**Files:**
- Modify: `src/ach_agent/main.py` — `provision_memory` at boot (after 1091); start/stop `MemoryFacade` beside the proxies (~1149 start, ~1416 + ~1576 stop); `select_memory_wiring_async` (542-558) takes a `facade_url` and returns it in place of the raw endpoint; extend `collect_secret_env_names` (985-995) to include `memory.hindsight.auth.env`.
- Test: `tests/memory/test_wiring.py`

**Interfaces:**
- Consumes: `provision_memory` (Task 3), `MemoryFacade` (Task 4), `prepare_memory` (Task 2), `resolve_secret`, `HindsightMemory`.
- Produces: updated `select_memory_wiring_async(memory_cfg, facade_url)` — returns `([facade_url], memory_prompt)` when memory is available and `facade_url` is set, else `([], memory_prompt)`.

- [ ] **Step 1: Write the failing test**

```python
# tests/memory/test_wiring.py
import pytest
import ach_agent.main as m
from ach_agent.config.schema import HindsightMemory


def _cfg():
    return HindsightMemory.model_validate(
        {
            "type": "hindsight",
            "hindsight": {
                "endpoint": "https://hs/mcp",
                "bank": "b",
                "auth": {"env": "HS_TOK"},
                "mentalModels": [],
            },
        }
    )


@pytest.mark.asyncio
async def test_wiring_returns_facade_url_not_endpoint(monkeypatch):
    async def fake_prepare(cfg):
        return True, "## Memory\n\nok"

    monkeypatch.setattr(m, "prepare_memory", fake_prepare)
    servers, prompt = await m.select_memory_wiring_async(_cfg(), "http://127.0.0.1:9/mcp")
    assert servers == ["http://127.0.0.1:9/mcp"]  # facade URL, NOT the hindsight endpoint
    assert prompt == "## Memory\n\nok"


@pytest.mark.asyncio
async def test_wiring_empty_when_unavailable(monkeypatch):
    async def fake_prepare(cfg):
        return False, "## Memory\n\nUnavailable"

    monkeypatch.setattr(m, "prepare_memory", fake_prepare)
    servers, _ = await m.select_memory_wiring_async(_cfg(), "http://127.0.0.1:9/mcp")
    assert servers == []


def test_memory_auth_env_collected_for_forward_env_strip():
    """SECURITY: the memory admin secret env NAME must be collected so it's stripped from
    engine.forwardEnv + redacted from logs — same as webhook/a2a secrets."""
    import types

    cfg = types.SimpleNamespace(channels=[], memory=_cfg())  # _cfg() has auth={env:HS_TOK}
    assert "HS_TOK" in m.collect_secret_env_names(cfg)


def test_memory_no_auth_collects_nothing():
    import types

    mem = HindsightMemory.model_validate(
        {"type": "hindsight", "hindsight": {"endpoint": "http://hs/mcp", "bank": "b", "mentalModels": []}}
    )
    cfg = types.SimpleNamespace(channels=[], memory=mem)
    assert m.collect_secret_env_names(cfg) == []
```

Note: `prepare_memory` must be importable as `m.prepare_memory`. If it is currently imported lazily inside the function, hoist it to a module-level import in `main.py` so the monkeypatch target exists.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/memory/test_wiring.py -q`
Expected: FAIL — `select_memory_wiring_async` takes one arg today and returns the raw endpoint; `m.prepare_memory` may not exist at module scope.

- [ ] **Step 3: Update `select_memory_wiring_async`**

At the top of `main.py`, add a module-level import so tests can patch it and the function stops importing lazily:

```python
from ach_agent.memory.hindsight import prepare_memory, provision_memory
from ach_agent.memory.facade import MemoryFacade
```

Rewrite `select_memory_wiring_async` (main.py:542-558):

```python
async def select_memory_wiring_async(
    memory_cfg: Memory | None,
    facade_url: str | None,
) -> tuple[list[str], str]:
    """Probe memory + build the prompt section; return the FACADE url (not the raw endpoint).

    The agent only ever reaches Hindsight through the harness facade, so the mcp_servers list
    carries the facade URL. Gated by prepare_memory's probe (D-02 fail-open) AND by the facade
    actually being up.
    """
    if not isinstance(memory_cfg, HindsightMemory):
        return [], ""
    mem_available, memory_prompt = await prepare_memory(memory_cfg)
    mcp_servers = [facade_url] if (mem_available and facade_url) else []
    return mcp_servers, memory_prompt
```

Update its two call sites to pass the facade URL:
- `engine_runner` (main.py:642): `mcp_servers, memory_prompt = await select_memory_wiring_async(effective_memory_cfg, memory_facade_url)` where `memory_facade_url` is captured from `main()` (Step 4). Thread it into `engine_runner`'s closure the same way `engine_cfg`/`pool` are.
- `--tui` pre-warm (main.py:1364-1370): pass `memory_facade_url` too (or the local warm variable holding it).

- [ ] **Step 4: Start/stop the facade + provision at boot in `main()`**

After `cfg = load_config(config_path)` (main.py:1091), add the fail-open provisioning:

```python
    # Boot-once memory provisioning (ensure bank + mental models). Fail-open (never raises).
    await provision_memory(cfg.memory)
```

Beside the existing proxy starts (inside the `if ek:` block near main.py:1149), start the facade when memory is a reachable Hindsight config with a resolvable secret. Declare `memory_facade: MemoryFacade | None = None` and `memory_facade_url: str | None = None` near the other proxy locals (~1128), then:

```python
    if isinstance(cfg.memory, HindsightMemory):
        from ach_agent.memory.hindsight import resolve_memory_secret

        _ok, _mem_secret = resolve_memory_secret(cfg.memory.hindsight)  # secret may be None
        if _ok:
            memory_facade = MemoryFacade(
                cfg.memory.hindsight.endpoint, _mem_secret, cfg.memory.hindsight.bank
            )
            memory_facade_url = await memory_facade.start()
        else:
            log.warning("memory: auth configured but env unset — facade not started; running without memory")
```

Ensure `memory_facade_url` is in scope for `engine_runner`/pre-warm (define it before the runner closure is built at main.py:1314). Add teardown at BOTH shutdown paths:
- Graceful drain, next to `mcp_proxy.stop()` (main.py:1578):

```python
    if memory_facade is not None:
        await memory_facade.stop()
```

- Console-mode `finally`, next to `stop_model_proxies()` (main.py:1416): the same `if memory_facade is not None: await memory_facade.stop()`.

**SECURITY — extend `collect_secret_env_names` (main.py:985-995)** so the memory admin secret joins the same forwardEnv-strip + log-redaction path as webhook/a2a secrets. Add before its `return names`:

```python
    from ach_agent.config.schema import HindsightMemory

    mem = getattr(cfg, "memory", None)
    if isinstance(mem, HindsightMemory) and mem.hindsight.auth is not None:
        names.append(mem.hindsight.auth.env)
    return names
```

This is what wires the new secret into `strip_forwarded_secrets` (main.py:998) and `add_secret_redaction` (main.py:1098) — both already called at boot over `collect_secret_env_names(cfg)`, so no other change is needed there. (No-auth config → nothing appended.)

- [ ] **Step 5: Run the wiring test + full memory suite + typecheck**

Run: `uv run pytest tests/memory -q && uv run mypy --strict src/ach_agent/main.py`
Expected: PASS. Fix any other `select_memory_wiring_async(...)` call site the grep `grep -n select_memory_wiring_async src/ach_agent/main.py` reveals — all must pass two args now.

- [ ] **Step 6: Lint everything**

Run: `uv run ruff check src/ach_agent && uv run ruff format --check src/ach_agent`
Expected: clean (run `uv run ruff format src/ach_agent` if the format check fails).

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/main.py tests/memory/test_wiring.py
git commit -m "feat(memory): wire facade + boot provisioning into main; agent reaches hindsight only via facade"
```

---

### Task 6: Docs + cross-repo flag

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (the `memory` section) — the `mentalModels` shape change, the new required `auth`, the facade topology, and that the agent-facing tools carry no `bank_id`.
- Create: `docs/references/2026-07-05-hindsight-memory-facade.md` (ADR) + one row in `docs/references/README.md`.
- No code.

**Interfaces:** none (documentation).

- [ ] **Step 1: Update the CONTRACT memory section**

In `docs/plan/CONTRACT_v3.md`, find the `memory:` / `hindsight` block and update it to the new schema. Use the repo's JSON+`//` comment style. Document verbatim:

```jsonc
"memory": {
  "type": "hindsight",
  "hindsight": {
    "endpoint": "https://hindsight.../mcp",
    "bank": "gitlab-pr-review",                 // static, harness-owned; agent NEVER sees/sets it
    "auth": { "env": "ACH_SECRET_MEMORY_HINDSIGHT" }, // OPTIONAL (omit for internal/no-auth URL). env NAME is
                                                       // operator-generated (ACH_SECRET_<...>); harness reads it
                                                       // from os.environ transparently. Bearer, NOT the ek_.
    "mission": "AI code reviewer",              // optional; passed to create_bank
    "mentalModels": [                            // rich specs the harness provisions at boot
      { "id": "architecture", "name": "Architecture", "sourceQuery": "What is the architecture?",
        "autoRefresh": true, "maxTokens": 2048 }
    ]
  }
}
```

Add a short paragraph: the agent reaches Hindsight ONLY through a harness-hosted localhost MCP facade exposing `memory_recall(query,tags?)`, `memory_reflect(query,tags?)`, `memory_get_mental_model(id)`, `memory_retain(content,tags?)` — no `bank_id` parameter, no admin/destructive tools. Per-repo partitioning is via tags, never by templating `bank` (T-04-03).

- [ ] **Step 2: Write the ADR**

Create `docs/references/2026-07-05-hindsight-memory-facade.md` capturing: the problem (raw Hindsight = 30 tools incl. destructive; current code never provisions and calls the wrong tool names), the decision (harness facade + admin secret + boot provisioning), the 4-tool agent surface, the assumed-Bearer note, and the tags-not-bank partitioning rule. Add one row to `docs/references/README.md`.

- [ ] **Step 3: Flag the cross-repo change (do NOT implement here)**

In the ADR's "Follow-ups" section, state: `ach-runtime` (the Go operator) must render the CRD → contract `memory.hindsight` with the rich `mentalModels` objects + the `auth` env NAME + `mission`. Until that lands, this schema is only usable via hand-authored local configs. This is a separate PR in the sibling repo.

- [ ] **Step 4: Commit**

```bash
git add docs/plan/CONTRACT_v3.md docs/references/2026-07-05-hindsight-memory-facade.md docs/references/README.md
git commit -m "docs(memory): CONTRACT + ADR for hindsight facade; flag ach-runtime CRD follow-up"
```

---

## Self-Review

**Spec coverage:**
- No raw Hindsight to the agent → Task 4 (facade, 4 tools) + Task 5 (facade URL replaces endpoint in `mcp_servers`). ✅
- `bank_id` hidden from agent → Task 4 (`_invoke` injects it) + Task 2 (TOOLS_SPEC drops it). ✅
- Optional admin secret (Bearer), not ek → Task 1 (`auth: SecretSource | None`) + Task 2 (`hindsight_auth_headers` None-safe, `resolve_memory_secret` gate) + Task 5 (never forwarded to opencode). Absent auth → internal/no-auth URL; configured-but-unset → fail-open degrade. ✅
- `mentalModels` regression (harness provisions, not agent) → Task 1 (rich specs) + Task 3 (`provision_memory`). ✅
- Correct tool names (`hindsight_*`) → Task 2 constants + fetch fix. ✅
- Per-repo via tags → Task 2 TOOLS_SPEC guidance + Task 6 doc. ✅
- Fail-open throughout → Tasks 2/3/4/5 all degrade, never raise. ✅
- Secret hygiene (forwardEnv strip + log redaction) → Task 5 extends `collect_secret_env_names` so the memory admin secret joins the existing webhook/a2a secret path; never written to opencode.json/env. ✅

**Placeholder scan:** every code step carries full code; no TODO/TBD; tests are concrete. ✅

**Type consistency:** `call_hindsight(endpoint, secret, tool, args) -> str` used identically in Tasks 2/3/4. `MentalModelSpec` fields (`id`, `name`, `source_query`, `auto_refresh`, `max_tokens`) match across Tasks 1/3. `select_memory_wiring_async(memory_cfg, facade_url) -> (list[str], str)` consistent Task 5. Facade tool names (`memory_recall` etc.) match TOOLS_SPEC (Task 2) and the facade registration (Task 4). ✅

**Known verify-if-fails points (flagged inline):** FastMCP `list_tools()` await-ness (Task 4 Step 4); uvicorn `servers[0].sockets[0]` mypy (Task 4 Step 5); any extra `select_memory_wiring_async` call site (Task 5 Step 5).
