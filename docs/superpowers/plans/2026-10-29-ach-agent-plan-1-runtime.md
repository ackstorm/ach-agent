# ach-agent Plan 1: repo bootstrap and runtime `workspace-v3`

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Create the `ach-agent` repo with the imported runtime, and move the runtime to config `workspace-v3`: standalone or ACH capability server, per-upstream headers, model enforcement, standalone memory and metrics, and gateway-outage handling.

**Architecture:** The runtime keeps its package (`ach_runtime`) and its two roles (control, execution). `RuntimeConfig` replaces the `ach` block and root `model` with a `capability` block. Properties `config.model`, `config.capability.filter` and `config.identity_environment` give the rest of the code one read path in both modes. The ACH `Hydrator` takes a normalized `HydrationInput`; a `standalone_catalog()` builds the same `HydratedCatalog` from config. Facades receive resolved upstream headers instead of an ek. The host API gains `PUT /host/v1/gateway`, which the operator (Plan 2) calls to report gateway availability.

**Tech Stack:** Python 3.13, pydantic 2, Starlette/FastAPI, httpx, pytest (`asyncio_mode=auto`), Docker.

**Spec:** `docs/superpowers/specs/2026-10-29-ach-agent-design.md` (copied into the new repo in Task 1). Plan 2 (operator) and Plan 3 (chart, e2e, release) follow.

**Assumed answers (confirm before Task 1):** the existing public repo `ackstorm/ach-agent` is reused; its history is kept and a commit retires the legacy tree; the first release is `v0.19.0`; the old local checkout moves to `../ach-agent-legacy`.

## Global Constraints

- Repo `github.com/ackstorm/ach-agent`, local path `/home/coder/workspace/local/ach-agent`, MIT licence. Runtime lives in `runtime/`; Python package stays `ach_runtime`; distribution name `ach-agent-runtime`; version `0.19.0`.
- Config schema literal `workspace-v3` for `RuntimeConfig.schemaVersion` and `ExecutionBootstrap.bootstrapVersion`. One version; no compatibility with `workspace-v2`.
- The runtime never calls the Kubernetes API or the OpenShell gateway API.
- No credential value in the config document, the public projection, the catalog sent to execution, or logs. Credentials are env names (`{"env": NAME}`), resolved only in the control role.
- Config `model.type` keeps the runtime's name for the wire format (`openai | anthropic | gemini`); the CRD's `api` field (Plan 2) renders into it.
- Reserved upstream header names (rejected in config): `host`, `content-length`, `transfer-encoding`, `connection`, `traceparent`, `tracestate`, `x-litellm-session-id`, `x-litellm-trace-id`, any `x-ach-*`, any `langfuse_*`. Header names are unique case-insensitively per endpoint.
- Model facade allow-list (relative to the model endpoint), POST only:
  - `openai`: `chat/completions`, `responses`; model from body `model`.
  - `anthropic`: `messages`, `messages/count_tokens`; model from body `model`.
  - `gemini`: `models/{m}:generateContent`, `models/{m}:streamGenerateContent`, `models/{m}:countTokens`; model from path `{m}`; only query allowed is `alt=sse`.
  - Rejected with zero upstream egress: other paths 404, other methods 405, `%` anywhere in the raw path 400, non-JSON body 400 (openai/anthropic), `Content-Encoding` other than `identity` 400, missing or different model 400, unexpected query 400.
- Every new `src/**/*.py` file starts with `# SPDX-License-Identifier: MIT`.
- Git: use `/usr/bin/git`. Never push. Commit trailers:
  ```
  Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA
  ```
- Inner loop: per-file pytest in `runtime/.venv`. Full suite once at the end (`make -C runtime test-unit`). Bounded waits only; never pipe a gate into `tail`/`grep`.

Test commands (from `runtime/`):

```bash
# one-time
uv venv .venv --python 3.13 && uv pip install --python .venv/bin/python -e ".[dev]"
# per file
PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness \
  .venv/bin/pytest --import-mode=importlib tests/<path>.py -q -p no:cacheprovider
```

## File map

| File (under `runtime/`) | Change |
|---|---|
| `src/ach_runtime/config/schema.py` | `CapabilityBlock`, `StandaloneServer`, `AchServer`, `ModelLimits`; `RuntimeConfig.capability`; properties `model`, `identity_environment`; memory and header validators |
| `src/ach_runtime/config/public.py` | literals to v3; projection reads `config.model`, `config.capability.filter` |
| `src/ach_runtime/harness/hydrate.py` | `HydrationInput`; `Hydrator.load(source)`; `stage_context(..., excluded_skills=...)`; `standalone_catalog(config)` |
| `src/ach_runtime/harness/control.py` | assembly per server; `resolve_headers`; facades get headers; identity environment; unpriced cost |
| `src/ach_runtime/facades/_proxy.py` | case-insensitive injected-header replacement; hop-by-hop and `x-api-key` dropped |
| `src/ach_runtime/facades/model.py` | `headers`, `api`, `model` ctor args; allow-list |
| `src/ach_runtime/facades/mcp.py` | `server_headers` instead of `ek` |
| `src/ach_runtime/harness/app.py` | `/metrics` always mounted |
| `src/ach_runtime/observability/runs.py` | `register_secrets()`; `redact` uses it; `gateway_unavailable` anomaly |
| `src/ach_runtime/harness/hosted.py` | `PUT /gateway`, `gateway_available()`, `wait_gateway()` |
| `src/ach_runtime/harness/workspaces.py` | `gateway_available()` on the source protocol and manager; deferred adoption |
| `src/ach_runtime/harness/local.py`, `tests/harness/fake_source.py` | `gateway_available()` returns True / settable |
| `src/ach_runtime/harness/lifecycle.py` | no recovery while the gateway is unavailable |
| `src/ach_runtime/harness/delivery.py` | connect failures retried with backoff until the deadline |
| `scripts/gen_schema.py`, `docs/schemas/*` | v3 schema and contract doc |
| `tests/config/fixtures/*.json`, `examples/hosts/compose/config.json` | v3 shape; new `standalone-runtime.json` |

---

### Task 1: Bootstrap the repo with the imported runtime

**Files:**
- Move: `/home/coder/workspace/local/ach-agent` -> `/home/coder/workspace/local/ach-agent-legacy`
- Create in the new checkout: `LICENSE`, `README.md`, `.gitignore`, `CLAUDE.md`, `runtime/**` (import), `.github/workflows/runtime.yml`, `docs/superpowers/specs/2026-10-29-ach-agent-design.md`, `docs/superpowers/plans/2026-10-29-ach-agent-plan-1-runtime.md`
- Modify: `runtime/pyproject.toml`, `runtime/Makefile`

**Interfaces:**
- Produces: branch `feat/bootstrap` in `/home/coder/workspace/local/ach-agent` with `runtime/` importable and its suite green; `make -C runtime test-unit`.

- [ ] **Step 1: Move the legacy checkout (needs the user's go-ahead given at plan approval)**

```bash
cd /home/coder/workspace/local
test ! -e ach-agent-legacy || { echo "FAIL: ach-agent-legacy exists" >&2; exit 1; }
/usr/bin/git -C ach-agent worktree list
mv ach-agent ach-agent-legacy
/usr/bin/git -C ach-agent-legacy worktree repair
/usr/bin/git clone https://github.com/ackstorm/ach-agent.git ach-agent
cd ach-agent && /usr/bin/git checkout -b feat/bootstrap origin/main
```

Expected: clone on `feat/bootstrap` at the same commit as `origin/main` (`bc6c74b` or later).

- [ ] **Step 2: Retire the legacy tree in one commit**

```bash
cd /home/coder/workspace/local/ach-agent
/usr/bin/git rm -r -q .
/usr/bin/git commit -q -m "chore: retire the legacy runtime tree

The legacy runtime stays reachable through tag v0.17.0 and this commit's parent.
ach-agent now holds the unified operator and runtime.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

- [ ] **Step 3: Import ach-runtime main into `runtime/`**

```bash
cd /home/coder/workspace/local/ach-agent
SRC_SHA=$(/usr/bin/git -C ../ach-runtime rev-parse main); echo "$SRC_SHA"
mkdir runtime
/usr/bin/git -C ../ach-runtime archive "$SRC_SHA" \
  src tests plugins docs/schemas docs/references examples scripts \
  Dockerfile Dockerfile.devtools Dockerfile.devtools.dockerignore .dockerignore \
  docker-compose.yml pyproject.toml Makefile CHANGELOG.md .gitleaks.toml \
  | tar -x -C runtime
ls runtime/docs/references >/dev/null 2>&1 || echo "note: no docs/references in source"
/usr/bin/git add runtime
/usr/bin/git commit -q -m "chore: import ach-runtime ${SRC_SHA} into runtime/

Plain copy of ach-runtime main at ${SRC_SHA}; history stays in ackstorm/ach-runtime.

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

If `git archive` fails because a listed path does not exist at `$SRC_SHA`, drop that path from the list and rerun. Never copy `spikes/` (it holds PoC keys) or `.mcp.json`.

- [ ] **Step 4: Rename the distribution, add repo files and the full-suite target**

`runtime/pyproject.toml`: set `name = "ach-agent-runtime"`, `version = "0.19.0"`, description `"ach-agent runtime: control and execution roles (workspace-v3)"`. Update `tests/test_version.py` if it asserts the old name or version.

Append to `runtime/Makefile` (and add `test-unit` to `.PHONY`):

```make
test-unit: ## Full unit suite in the devtools execution image, as CI runs it
	docker build --target python-execution -f Dockerfile.devtools -t ach-agent-runtime-execution-devtools .
	docker run --rm --network host -e PYTHONDONTWRITEBYTECODE=1 \
	  -e PYTHONPATH=/work/src:/work/tests/storage:/work/tests/engine:/work/tests/facades:/work/tests/harness \
	  -v "$(CURDIR)/src:/work/src:ro" -v "$(CURDIR)/tests:/work/tests:ro" \
	  -v "$(CURDIR)/plugins:/work/plugins:ro" -v "$(CURDIR)/docs:/work/docs:ro" \
	  -v "$(CURDIR)/examples:/work/examples:ro" -v "$(CURDIR)/scripts:/work/scripts:ro" \
	  -v "$(CURDIR)/Dockerfile:/work/Dockerfile:ro" -w /work \
	  ach-agent-runtime-execution-devtools \
	  pytest --import-mode=importlib tests -q -p no:cacheprovider
```

Root files:
- `LICENSE`: copy `../ach-runtime/LICENSE` (MIT).
- `.gitignore`: `.venv/`, `__pycache__/`, `.pytest_cache/`, `.superpowers/`, `*.log`.
- `README.md`: title `ach-agent`, two paragraphs from spec §1 (goal) and §2 (layout), and "Status: under construction; see docs/superpowers/specs".
- `CLAUDE.md`: layout (`operator/` Go, `runtime/` Python), the test commands from this plan's header, the invariant "the runtime never calls the Kubernetes or OpenShell gateway API", "use /usr/bin/git".
- Copy the spec and this plan into `docs/superpowers/`.
- `.github/workflows/runtime.yml`: copy `../ach-runtime/.github/workflows/ci.yml`; set `defaults.run.working-directory: runtime`; replace every `$GITHUB_WORKSPACE/` with `$GITHUB_WORKSPACE/runtime/`; set the docker build `context: runtime` and `file: runtime/Dockerfile.devtools`; add `paths: ["runtime/**", ".github/workflows/runtime.yml"]` to its triggers; drop mounts of directories that were not imported (`spikes`, `schemas`).

- [ ] **Step 5: Baseline the imported suite**

```bash
cd /home/coder/workspace/local/ach-agent/runtime
make test-unit > /tmp/ach-agent-baseline.log 2>&1; echo "EXIT=$?"
grep -E "passed|failed|error" /tmp/ach-agent-baseline.log | tail -3
```

Expected: `EXIT=0`. A test that reads a non-imported path (e.g. `spikes/`) is deleted with a note in the commit message; any other failure stops the task (report it).

- [ ] **Step 6: Commit**

```bash
cd /home/coder/workspace/local/ach-agent
/usr/bin/git add LICENSE README.md .gitignore CLAUDE.md docs .github runtime/pyproject.toml runtime/Makefile runtime/tests/test_version.py
/usr/bin/git commit -q -m "chore: set up the ach-agent repo around the imported runtime

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 2: Config `workspace-v3` with the `capability` block

**Files:**
- Modify: `runtime/src/ach_runtime/config/schema.py` (ach block L89-120, `ModelBlock` L140, `RuntimeConfig` L760-813, `EngineBlock._check_deny_tools` message L216), `runtime/src/ach_runtime/config/public.py` (L73, L109-111, L137), `runtime/src/ach_runtime/config/__init__.py` docstring, `runtime/scripts/gen_schema.py` (title/$id/output name), `runtime/src/ach_runtime/harness/control.py:878` literal
- Create: `runtime/tests/config/fixtures/standalone-runtime.json`, `runtime/tests/config/test_capability.py`, `runtime/docs/schemas/ach-workspace-config-v3.schema.json` (generated; delete the v2 file)
- Modify fixtures: `runtime/tests/config/fixtures/{pr-review-runtime.json,bootstrap.json,config-canonicalization.json}`, `runtime/examples/hosts/compose/config.json`, and every test that builds or mutates `wire["ach"]`, `wire["model"]` or the `workspace-v2` literal
- Rename + edit: `runtime/docs/schemas/ach-runtime-contract-v2.md` -> `ach-runtime-contract-v3.md` (§3.1 and §7: config shape)

**Interfaces:**
- Produces (in `ach_runtime.config.schema`):
  - `class ModelLimits(BaseModel)`: `context: int`, `output: int` (both > 0)
  - `class StandaloneModel(BaseModel)`: `name`, `type`, `base_url` (alias `baseUrl`), `headers: dict[str, SecretAlias]`, `params`, `thinking: ThinkingBlock`, `limits: ModelLimits | None`
  - `class StandaloneMcpServer(BaseModel)`: `name`, `url`, `headers: dict[str, SecretAlias]`
  - `class StandaloneServer(BaseModel)`: `model: StandaloneModel`, `mcp_servers: list[StandaloneMcpServer]` (alias `mcpServers`)
  - `class AchServer(BaseModel)`: `base_url` (alias `baseUrl`), `environment: str`, `key: SecretAlias`, `model: ModelBlock`
  - `class CapabilityBlock(BaseModel)`: `server: Literal["standalone", "ach"]`, `standalone: StandaloneServer | None`, `ach: AchServer | None`, `filter: AchCapabilityFilter`
  - `RuntimeConfig.capability: CapabilityBlock`; `RuntimeConfig.model -> ModelBlock` (cached property); `RuntimeConfig.identity_environment -> str` (cached property)
  - `AchBlock` and `AchCapabilityBlock` are deleted except `AchCapabilityBlock`, which `PublicConfig.capability` keeps using.

- [ ] **Step 1: Write the failing tests**

`runtime/tests/config/test_capability.py`:

```python
# SPDX-License-Identifier: MIT
"""workspace-v3 capability block: server selection, headers, memory rules."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from ach_runtime.config import ConfigError, parse_config, public_config
from ach_runtime.config.canonical import compute_config_version

FIXTURES = Path(__file__).parent / "fixtures"


def _wire(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _parse(wire: dict):
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def test_standalone_fixture_parses_and_projects_model():
    cfg = _parse(_wire("standalone-runtime.json"))
    assert cfg.schema_version == "workspace-v3"
    assert cfg.capability.server == "standalone"
    assert cfg.model.name == "anthropic/claude-sonnet-5.5"
    assert cfg.model.type == "openai"
    assert cfg.identity_environment == cfg.agent.namespace


def test_ach_fixture_reads_environment_from_ach_server():
    cfg = _parse(_wire("pr-review-runtime.json"))
    assert cfg.capability.server == "ach"
    assert cfg.identity_environment == cfg.capability.ach.environment
    assert cfg.model == cfg.capability.ach.model


@pytest.mark.parametrize("server,present", [("standalone", "ach"), ("ach", "standalone")])
def test_server_requires_its_block_and_forbids_the_other(server, present):
    wire = _wire("standalone-runtime.json" if server == "standalone" else "pr-review-runtime.json")
    other = _wire("pr-review-runtime.json" if server == "standalone" else "standalone-runtime.json")
    wire["capability"][present] = other["capability"][present]
    with pytest.raises(ConfigError, match="capability"):
        _parse(wire)


@pytest.mark.parametrize("name", ["Host", "x-ach-key", "X-ACH-Agent", "traceparent", "langfuse_session", "Connection"])
def test_reserved_header_names_are_rejected(name):
    wire = _wire("standalone-runtime.json")
    wire["capability"]["standalone"]["model"]["headers"] = {name: {"env": "ACH_AGENT_MODEL_H0"}}
    with pytest.raises(ConfigError, match="reserved"):
        _parse(wire)


def test_duplicate_header_names_differing_in_case_are_rejected():
    wire = _wire("standalone-runtime.json")
    wire["capability"]["standalone"]["mcpServers"][0]["headers"] = {
        "X-API-KEY": {"env": "ACH_AGENT_MCP_SERPER_H0"}, "x-api-key": {"env": "ACH_AGENT_MCP_SERPER_H1"}}
    with pytest.raises(ConfigError, match="duplicate"):
        _parse(wire)


def test_standalone_rejects_ach_memory_auth():
    wire = _wire("standalone-runtime.json")
    wire["memory"] = {"type": "ach-memory", "achMemory": {"mcpServerId": "serper", "auth": {"type": "ach"}}}
    with pytest.raises(ConfigError, match="auth.type"):
        _parse(wire)


def test_standalone_memory_server_must_be_a_standalone_mcp_server():
    wire = _wire("standalone-runtime.json")
    wire["memory"] = {"type": "ach-memory", "achMemory": {"mcpServerId": "nope"}}
    with pytest.raises(ConfigError, match="mcpServerId"):
        _parse(wire)


def test_standalone_rejects_skill_exclusions():
    wire = _wire("standalone-runtime.json")
    wire["capability"]["filter"] = {"exclude": {"skills": ["x"], "tools": [], "mcpServers": []}}
    with pytest.raises(ConfigError, match="skills"):
        _parse(wire)


def test_public_projection_never_carries_upstreams_or_headers():
    cfg = _parse(_wire("standalone-runtime.json"))
    dumped = json.dumps(public_config(cfg, {}).model_dump(by_alias=True, mode="json"))
    assert "litellm.example.com" not in dumped
    assert "mcp.serper.dev" not in dumped
    assert "ACH_AGENT_" not in dumped
    assert "baseUrl" not in dumped
```

`public_config(cfg, {})` assumes the fixture's `engine.env` is empty; set it so in the new fixture.

- [ ] **Step 2: Create the fixtures**

Convert `pr-review-runtime.json` (and the other two fixtures plus `examples/hosts/compose/config.json`) with this one-off script, run from `runtime/` (not committed):

```python
import json, pathlib, sys
from ach_runtime.config.canonical import compute_config_version
for path in map(pathlib.Path, sys.argv[1:]):
    wire = json.loads(path.read_text())
    for key in ("schemaVersion", "bootstrapVersion"):
        if wire.get(key) == "workspace-v2":
            wire[key] = "workspace-v3"
    if "ach" in wire:
        ach = wire.pop("ach")
        wire["capability"] = {
            "server": "ach",
            "ach": {"baseUrl": ach["baseUrl"], "environment": ach["environment"],
                    "key": ach["identity"], "model": wire.pop("model")},
            "filter": ach.get("capability", {}).get("filter", {"exclude": {"tools": [], "mcpServers": [], "skills": []}}),
        }
    if "configVersion" in wire:
        wire["configVersion"] = compute_config_version(wire)
    path.write_text(json.dumps(wire, indent=2) + "\n")
```

Create `tests/config/fixtures/standalone-runtime.json` by copying the converted `pr-review-runtime.json`, then: set `capability` to the block below, set `engine.env` to `{}`, drop `memory`, recompute `configVersion` with the same script.

```json
"capability": {
  "server": "standalone",
  "standalone": {
    "model": {"name": "anthropic/claude-sonnet-5.5", "type": "openai",
              "baseUrl": "https://litellm.example.com/v1",
              "headers": {"x-litellm-api-key": {"env": "ACH_AGENT_MODEL_H0"}},
              "params": {}, "thinking": {"enabled": false},
              "limits": {"context": 200000, "output": 32000}},
    "mcpServers": [{"name": "serper", "url": "https://mcp.serper.dev/mcp",
                    "headers": {"X-API-KEY": {"env": "ACH_AGENT_MCP_SERPER_H0"}}}]
  },
  "filter": {"exclude": {"tools": [], "mcpServers": [], "skills": []}}
}
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `PYTHONPATH=src .venv/bin/pytest --import-mode=importlib tests/config/test_capability.py -q -p no:cacheprovider`
Expected: FAIL (`workspace-v2` literal / extra field `capability`).

- [ ] **Step 4: Implement the schema**

In `schema.py`, delete `AchBlock` (keep `AchCapabilityFilterExclude`, `AchCapabilityFilter`, `AchCapabilityBlock`, `SecretAlias`). Add after `ModelBlock`:

```python
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_RESERVED_HEADER_NAMES = frozenset({
    "host", "content-length", "transfer-encoding", "connection",
    "traceparent", "tracestate", "x-litellm-session-id", "x-litellm-trace-id",
})
_RESERVED_HEADER_PREFIXES = ("x-ach-", "langfuse_")


def _check_headers(headers: dict[str, SecretAlias], where: str) -> None:
    seen: set[str] = set()
    for name in headers:
        lowered = name.lower()
        if not _HEADER_NAME_RE.fullmatch(name):
            raise ValueError(f"{where}: invalid header name {name!r}")
        if lowered in _RESERVED_HEADER_NAMES or lowered.startswith(_RESERVED_HEADER_PREFIXES):
            raise ValueError(f"{where}: header {name!r} is reserved")
        if lowered in seen:
            raise ValueError(f"{where}: duplicate header {name!r}")
        seen.add(lowered)


class ModelLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    context: int = Field(gt=0)
    output: int = Field(gt=0)


class StandaloneModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    name: str
    type: Literal["openai", "gemini", "anthropic"]
    base_url: str = Field(alias="baseUrl")
    headers: dict[str, SecretAlias] = Field(default_factory=dict)
    params: dict[str, Any] = Field(default_factory=dict)
    thinking: ThinkingBlock = Field(default_factory=ThinkingBlock)
    limits: ModelLimits | None = None

    @model_validator(mode="after")
    def _headers_are_valid(self) -> "StandaloneModel":
        _check_headers(self.headers, "capability.standalone.model.headers")
        return self


class StandaloneMcpServer(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    name: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,62}$")
    url: str
    headers: dict[str, SecretAlias] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _headers_are_valid(self) -> "StandaloneMcpServer":
        _check_headers(self.headers, f"capability.standalone.mcpServers[{self.name}].headers")
        return self


class StandaloneServer(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    model: StandaloneModel
    mcp_servers: list[StandaloneMcpServer] = Field(default_factory=list, alias="mcpServers")

    @model_validator(mode="after")
    def _names_are_unique(self) -> "StandaloneServer":
        names = [server.name for server in self.mcp_servers]
        if len(names) != len(set(names)):
            raise ValueError("capability.standalone.mcpServers: duplicate name")
        return self


class AchServer(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    base_url: str = Field(alias="baseUrl")
    environment: str
    key: SecretAlias
    model: ModelBlock


class CapabilityBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_by_alias=True, validate_by_name=False)

    server: Literal["standalone", "ach"]
    standalone: StandaloneServer | None = None
    ach: AchServer | None = None
    filter: AchCapabilityFilter = Field(default_factory=AchCapabilityFilter)

    @model_validator(mode="after")
    def _server_selects_its_block(self) -> "CapabilityBlock":
        chosen, other = (self.standalone, self.ach) if self.server == "standalone" else (self.ach, self.standalone)
        if chosen is None or other is not None:
            raise ValueError(f"capability: server {self.server!r} requires capability.{self.server} and no other server block")
        if self.server == "standalone" and self.filter.exclude.skills:
            raise ValueError("capability.filter.exclude.skills requires server 'ach'")
        return self
```

Use the model_config flags the neighbouring classes use (copy `ModelBlock`'s `model_config` exactly, including `strict` if present); the snippet shows the minimum.

In `RuntimeConfig`: replace the `ach` and `model` fields with `capability: CapabilityBlock`; change the literal to `Literal["workspace-v3"]`; add:

```python
    @cached_property
    def model(self) -> ModelBlock:
        """The selected server's model as the runtime's provider-neutral block."""
        if self.capability.ach is not None:
            return self.capability.ach.model
        m = self.capability.standalone.model
        return ModelBlock.model_validate(
            {"name": m.name, "type": m.type, "params": m.params, "thinking": m.thinking.model_dump(by_alias=True)})

    @cached_property
    def identity_environment(self) -> str:
        """Metrics/run-record environment label: the ACH environment, else the namespace."""
        return self.capability.ach.environment if self.capability.ach is not None else self.agent.namespace

    @model_validator(mode="after")
    def _memory_fits_the_capability_server(self) -> "RuntimeConfig":
        if self.memory is None or self.capability.standalone is None:
            return self
        params = self.memory.ach_memory
        if params.auth is not None and params.auth.type == "ach":
            raise ValueError("memory.achMemory.auth.type 'ach' requires capability.server 'ach'")
        names = {server.name for server in self.capability.standalone.mcp_servers}
        if params.mcp_server_id and params.mcp_server_id not in names:
            raise ValueError(f"memory.achMemory.mcpServerId {params.mcp_server_id!r} is not a capability.standalone.mcpServers name")
        return self
```

Import `cached_property` from `functools`. If pydantic rejects `cached_property` on the model, use `@property` (the objects are cheap). `parse_config`: also reject `"workspace-v2"` with message `"workspace-v2 is not supported; see docs/schemas/ach-runtime-contract-v3.md"`. Update the `_check_deny_tools` message to `capability.filter.exclude.tools`.

`public.py`: literals `workspace-v3`; L109-111 become `model=config.model,` and `capability=AchCapabilityBlock(filter=config.capability.filter),`. `control.py:878`: `bootstrapVersion="workspace-v3"`.

- [ ] **Step 5: Move every reader of `config.ach` to the new block**

```bash
cd runtime && grep -rn "config\.ach\b\|\.ach\.\(capability\|environment\|identity\|base_url\)\|\[\"ach\"\]\|\[\"model\"\]\|workspace-v2" src tests examples scripts docs/schemas | grep -v "\.json:"
```

Rules: `config.ach.capability.filter` -> `config.capability.filter`; `config.ach.environment` -> `config.identity_environment`; `config.ach.identity.env` -> `config.capability.ach.key.env`; `config.ach.base_url` -> `config.capability.ach.base_url`; tests mutating `wire["ach"][...]` -> `wire["capability"]["ach"][...]`; `wire["model"]` -> `wire["capability"]["ach"]["model"]`; `"workspace-v2"` -> `"workspace-v3"` (keep the v1/v2 rejection tests, updated to expect rejection of v2). Hydrator and assembly reads of `config.ach` are rewritten in Task 3; for this task keep them compiling by applying the same mechanical rules.

- [ ] **Step 6: Regenerate the JSON schema and update the contract doc**

In `scripts/gen_schema.py` change the title to `ach-runtime workspace-v3 RuntimeConfig`, `$id`/output to `ach-workspace-config-v3.schema.json`; `git rm docs/schemas/ach-workspace-config-v2.schema.json`; run `make gen-schema`. `git mv docs/schemas/ach-runtime-contract-v2.md docs/schemas/ach-runtime-contract-v3.md`; in §3.1 and §7 replace the `ach`/`model` description with the `capability` block (copy the JSON from Step 2 and one sentence per rule from the validators); fix links (`grep -rn contract-v2 runtime`).

- [ ] **Step 7: Run the config and public tests**

Run: `PYTHONPATH=src .venv/bin/pytest --import-mode=importlib tests/config -q -p no:cacheprovider && make schema-check`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests runtime/scripts runtime/docs runtime/examples
/usr/bin/git commit -q -m "feat(config): workspace-v3 capability block (standalone or ach)" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 3: Catalog sources and assembly per capability server

**Files:**
- Modify: `runtime/src/ach_runtime/harness/hydrate.py` (`Hydrator.load` L347-365, `stage_context` L368-), `runtime/src/ach_runtime/harness/control.py` (`assemble` L828-1005, `_transfer_context` L515-523)
- Test: `runtime/tests/harness/test_hydrate.py` (replace `_StubConfig` L391 and the `SimpleNamespace` configs L649), create `runtime/tests/harness/test_standalone_assembly.py`

**Interfaces:**
- Consumes: Task 2 config types.
- Produces:
  - `hydrate.HydrationInput(agent: str, base_url: str, environment: str, key_env: str, model: str)` with `HydrationInput.from_config(config) -> HydrationInput`
  - `Hydrator.load(self, source: HydrationInput) -> HydratedCatalog`
  - `stage_context(catalog, root, *, excluded_skills: Iterable[str], client, ek) -> ...` (other params unchanged)
  - `hydrate.standalone_catalog(config: RuntimeConfig) -> HydratedCatalog`
  - `control.resolve_headers(headers: Mapping[str, SecretAlias], env: Mapping[str, str]) -> dict[str, str]` (raises `WorkspaceAssemblyUnavailable`)
  - After assembly: `control.upstream_secrets: tuple[str, ...]` (every resolved header value and the ek), consumed by Task 6.
  - Facade constructor changes are Task 4; in this task pass the resolved headers to the existing ctors only where they already accept them, and keep `ek=` for ACH.

- [ ] **Step 1: Write the failing tests**

`runtime/tests/harness/test_standalone_assembly.py`:

```python
# SPDX-License-Identifier: MIT
"""Standalone assembly: no ACH round-trip, catalog from config, headers from env."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.harness.control import WorkspaceAssemblyUnavailable, assemble
from ach_runtime.harness.hydrate import standalone_catalog

FIXTURE = Path(__file__).parents[1] / "config" / "fixtures" / "standalone-runtime.json"
ENV = {"ACH_SANDBOX_KEY": "harness-key", "ACH_AGENT_MODEL_H0": "model-secret-1",
       "ACH_AGENT_MCP_SERPER_H0": "serper-secret-1"}


def _config():
    wire = json.loads(FIXTURE.read_text(encoding="utf-8"))
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def test_standalone_catalog_comes_from_config():
    catalog = standalone_catalog(_config())
    assert catalog.model.id == "anthropic/claude-sonnet-5.5"
    assert catalog.model.endpoint == "https://litellm.example.com/v1"
    assert catalog.model.metadata.context_limit == 200000
    assert catalog.model.metadata.output_limit == 32000
    assert [(s.id, s.endpoint) for s in catalog.mcp_servers] == [("serper", "https://mcp.serper.dev/mcp")]
    assert catalog.skills == () and catalog.a2a_agents == ()


async def test_standalone_assembly_makes_no_ach_call():
    def ach(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected ACH call {request.url}")

    control = await assemble(_config(), environ=ENV, ach_transport=httpx.MockTransport(ach))
    try:
        assert control.catalog.model.id == "anthropic/claude-sonnet-5.5"
        assert set(control.upstream_secrets) == {"model-secret-1", "serper-secret-1"}
    finally:
        await control.close()


async def test_standalone_assembly_fails_closed_on_missing_header_env():
    env = {k: v for k, v in ENV.items() if k != "ACH_AGENT_MCP_SERPER_H0"}
    with pytest.raises(WorkspaceAssemblyUnavailable, match="ACH_AGENT_MCP_SERPER_H0"):
        await assemble(_config(), environ=env)
```

In `test_hydrate.py`, replace `_StubConfig` with `HydrationInput(agent="a", base_url=BASE_URL, environment="engineering-prod", key_env="ACH_SECRET_IDENTITY", model=<the model id the test used>)` and call `hydrator.load(source)`; replace the `stage_context` `SimpleNamespace` config with `excluded_skills=[]` (or the list the test used).

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:tests/harness .venv/bin/pytest --import-mode=importlib tests/harness/test_standalone_assembly.py tests/harness/test_hydrate.py -q -p no:cacheprovider`
Expected: FAIL (`standalone_catalog` / `HydrationInput` missing).

- [ ] **Step 3: Implement `HydrationInput`, `standalone_catalog`, `stage_context`**

In `hydrate.py`:

```python
@dataclass(frozen=True, slots=True)
class HydrationInput:
    """What the ACH Hydrator needs, independent of the config document's layout."""

    agent: str
    base_url: str
    environment: str
    key_env: str
    model: str

    @classmethod
    def from_config(cls, config: RuntimeConfig) -> "HydrationInput":
        ach = config.capability.ach
        if ach is None:
            raise HydrationError("capability.server is not 'ach'")
        return cls(agent=config.agent.name, base_url=ach.base_url, environment=ach.environment,
                   key_env=ach.key.env, model=ach.model.name)


def standalone_catalog(config: RuntimeConfig) -> HydratedCatalog:
    """Catalog for capability.server 'standalone': the configured model and MCP servers only."""
    server = config.capability.standalone
    limits = server.model.limits
    metadata = None if limits is None else ModelMetadata(context_limit=limits.context, output_limit=limits.output)
    return HydratedCatalog(
        environment=config.identity_environment,
        model=HydratedModel(id=server.model.name, endpoint=server.model.base_url, metadata=metadata),
        mcp_servers=tuple(McpServerEntry(id=item.name, endpoint=item.url) for item in server.mcp_servers),
        skills=(), prompts=(), artifacts=(),
    )
```

`Hydrator.load(self, source: HydrationInput)`: replace `config.agent.name` -> `source.agent`, `config.ach.environment` -> `source.environment`, `config.ach.identity.env` -> `source.key_env`, `config.ach.base_url` -> `source.base_url`, `config.model.name` -> `source.model`. `stage_context`: replace the `config` parameter with keyword `excluded_skills: Iterable[str]`; `excluded = set(excluded_skills)`. Match `McpServerEntry`/`ModelMetadata` constructor style to their definitions (pydantic models take keywords).

- [ ] **Step 4: Branch the assembly**

In `control.py` add:

```python
def resolve_headers(headers: Mapping[str, SecretAlias], env: Mapping[str, str]) -> dict[str, str]:
    """Resolve configured upstream headers from env; a missing value fails assembly."""
    resolved: dict[str, str] = {}
    for name, alias in headers.items():
        value = env.get(alias.env, "")
        if not value:
            raise WorkspaceAssemblyUnavailable(f"upstream header {name} needs env {alias.env}, which is unset")
        resolved[name] = value
    return resolved
```

In `assemble`, replace L839-848 and the per-server reads with:

```python
    if config.capability.ach is not None:
        ach = config.capability.ach
        ek = env.get(ach.key.env, "")
        if not ek:
            raise WorkspaceAssemblyUnavailable("ACH identity credential is not configured")
        ach_client = httpx.AsyncClient(transport=ach_transport, timeout=30)
        hydrator = Hydrator(resolve_ek=lambda name: env.get(name, ""), client=ach_client)
        try:
            catalog = await hydrator.load(HydrationInput.from_config(config))
        finally:
            await ach_client.aclose()
        prefix = {"openai": "v1", "gemini": "gemini/v1beta", "anthropic": "anthropic"}[config.model.type]
        model_endpoint = f"{ach.base_url.rstrip('/')}/{prefix}"
        model_headers = {"x-ach-key": ek}
        mcp_headers = {item.id: {"x-ach-key": ek} for item in catalog.mcp_servers}
        upstream_secrets = (ek,)
    else:
        server = config.capability.standalone
        ek = ""
        catalog = standalone_catalog(config)
        model_endpoint = server.model.base_url
        model_headers = resolve_headers(server.model.headers, env)
        mcp_headers = {item.name: resolve_headers(item.headers, env) for item in server.mcp_servers}
        upstream_secrets = tuple(model_headers.values()) + tuple(v for h in mcp_headers.values() for v in h.values())
    identity = ProcessIdentity(agent=config.agent.name, environment=config.identity_environment)
```

Keep `mcp_servers = {item.id: item.endpoint for item in catalog.mcp_servers}`. Memory: `resolve_ach_memory_auth(params.auth, ek or None, ...)` unchanged otherwise. Remove the old `model_endpoint_prefix` block (L929-935) and point `ModelFacade(endpoint=model_endpoint, ...)`. Store `control.upstream_secrets = upstream_secrets`, `control._model_headers = model_headers`, `control._mcp_headers = mcp_headers` (Task 4 hands them to the facades). `A2AEgressFacade` stays as is (only built when `catalog.a2a_agents`, which standalone leaves empty). `_transfer_context`: `ek = getattr(self, "_ek", "")`; call `stage_context(self.catalog, root, excluded_skills=self.config.capability.filter.exclude.skills, client=client, ek=ek)`.

- [ ] **Step 5: Run the harness tests**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/harness tests/config -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests
/usr/bin/git commit -q -m "feat(harness): standalone catalog and per-server assembly" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 4: Per-upstream headers in the facades

**Files:**
- Modify: `runtime/src/ach_runtime/facades/_proxy.py` (`DROP_REQUEST_HEADERS` L111-124, header merge L237-245), `runtime/src/ach_runtime/facades/model.py` (ctor L46-56, L77), `runtime/src/ach_runtime/facades/mcp.py` (ctor L108-120, L154), `runtime/src/ach_runtime/harness/control.py` (facade construction L936-944)
- Test: `runtime/tests/facades/test_secrets.py`, create `runtime/tests/facades/test_upstream_headers.py`

**Interfaces:**
- Consumes: `control._model_headers: dict[str, str]`, `control._mcp_headers: dict[str, dict[str, str]]` (Task 3).
- Produces:
  - `ModelFacade.__init__(self, *, endpoint: str, headers: Mapping[str, str], key, owned_workspace, identity, correlation_headers=None, client=None)` (Task 5 adds `api`, `model`)
  - `McpFacade.__init__(self, *, servers: dict[str, str], server_headers: Mapping[str, Mapping[str, str]], key, owned_workspace, exclude, exclude_tools, identity, correlation_headers=None, client=None)`
  - `_proxy.HOP_BY_HOP_HEADERS: frozenset[str]`

- [ ] **Step 1: Write the failing tests**

`runtime/tests/facades/test_upstream_headers.py`:

```python
# SPDX-License-Identifier: MIT
"""Injected upstream headers replace inbound ones case-insensitively; hop-by-hop never forwarded."""

from __future__ import annotations

import httpx

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"k" * 32
IDENTITY = ProcessIdentity(agent="a", environment="e")


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def test_mcp_injects_exactly_one_header_per_configured_name():
    seen: list[list[tuple[str, str]]] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append([(k.lower(), v) for k, v in request.headers.multi_items()])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    facade = McpFacade(
        servers={"serper": "https://mcp.serper.dev/mcp"},
        server_headers={"serper": {"X-API-KEY": "real-secret"}},
        key=KEY, owned_workspace=_owned, exclude=frozenset(), exclude_tools=frozenset(),
        identity=IDENTITY, client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    token = facade_token(KEY, "ws")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        response = await client.post(
            f"/t/{token}/mcp/serper",
            json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
            headers={"x-api-key": "sandbox-guess", "Connection": "keep-alive", "Upgrade": "h2c", "TE": "trailers"},
        )
    assert response.status_code == 200
    names = [name for name, _ in seen[0]]
    assert names.count("x-api-key") == 1
    assert dict(seen[0])["x-api-key"] == "real-secret"
    assert "upgrade" not in names and "te" not in names
```

In `test_secrets.py`, change `_model_facade` to pass `headers={"x-ach-key": "ek-xyz"}` instead of `ek="ek-xyz"`, and add one test that sends `x-api-key: local-proxy` and asserts it does not reach upstream when no configured header has that name.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:tests/facades .venv/bin/pytest --import-mode=importlib tests/facades/test_upstream_headers.py tests/facades/test_secrets.py -q -p no:cacheprovider`
Expected: FAIL (`server_headers` unexpected keyword).

- [ ] **Step 3: Implement**

`_proxy.py`:

```python
HOP_BY_HOP_HEADERS = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "upgrade",
})
```

Add `"x-api-key"` to `DROP_REQUEST_HEADERS`. Replace the merge at L237-245 with:

```python
    injected = {name.lower() for name in inject_headers}
    headers = {key: value for key, value in request.headers.items()
               if key.lower() not in DROP_REQUEST_HEADERS
               and key.lower() not in HOP_BY_HOP_HEADERS
               and key.lower() not in injected
               and not is_correlation_header(key)}
    headers.update(inject_headers)
    headers = with_identity_headers(headers, identity)
    if correlation:
        headers.update(correlation)
```

Export `HOP_BY_HOP_HEADERS` in `__all__`. `model.py`: ctor takes `headers: Mapping[str, str]` (store `dict(headers)`), L77 passes `self._headers`. `mcp.py`: ctor takes `server_headers`; store `{sid: dict(h) for sid, h in server_headers.items()}`; L154 passes `self._server_headers.get(server_id, {})`. `control.py`: `ModelFacade(endpoint=model_endpoint, headers=model_headers, ...)`, `McpFacade(servers=mcp_servers, server_headers=mcp_headers, ...)`. Update every other constructor call (`grep -rn "ModelFacade(\|McpFacade(" runtime/src runtime/tests`).

- [ ] **Step 4: Run the facade and harness tests**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/facades tests/harness -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests
/usr/bin/git commit -q -m "feat(facades): per-upstream headers replace inbound ones case-insensitively" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 5: Model facade enforces the configured model and allow-list

**Files:**
- Modify: `runtime/src/ach_runtime/facades/model.py` (`_METHODS` L35, ctor, `handle` L62-81, routes L85-99), `runtime/src/ach_runtime/harness/control.py` (ModelFacade construction)
- Test: create `runtime/tests/facades/test_model_allowlist.py`; update `runtime/tests/harness/test_model_wire.py` (its `{"probe": True}` body needs `"model": MODEL_ID` for openai/anthropic) and any facade test posting to the model facade without a model

**Interfaces:**
- Consumes: Task 4 `ModelFacade` ctor.
- Produces: `ModelFacade.__init__(..., api: Literal["openai", "anthropic", "gemini"], model: str, ...)`; `model.check_model_request(api, model, method, tail, raw_path, query, headers, body) -> tuple[int, str] | None`.

- [ ] **Step 1: Write the failing tests**

`runtime/tests/facades/test_model_allowlist.py`:

```python
# SPDX-License-Identifier: MIT
"""Model facade: only the configured model on the allow-listed paths reaches upstream."""

from __future__ import annotations

import httpx
import pytest

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"k" * 32
MODEL = "anthropic/claude-sonnet-5.5"
GEMINI = "gemini-flash-latest"


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def _send(api: str, model: str, method: str, path: str, **kwargs) -> tuple[int, int]:
    hits = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={"ok": True})

    facade = ModelFacade(
        endpoint="https://up.example/v1", headers={"authorization": "Bearer real"}, api=api, model=model,
        key=KEY, owned_workspace=_owned, identity=ProcessIdentity(agent="a", environment="e"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    token = facade_token(KEY, "ws")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        response = await client.request(method, f"/t/{token}/{path}", **kwargs)
    return response.status_code, hits


@pytest.mark.parametrize(("api", "path"), [
    ("openai", "chat/completions"), ("openai", "responses"),
    ("anthropic", "messages"), ("anthropic", "messages/count_tokens"),
])
async def test_allowed_body_model_paths_reach_upstream(api, path):
    assert await _send(api, MODEL, "POST", path, json={"model": MODEL}) == (200, 1)


@pytest.mark.parametrize("action", ["generateContent", "streamGenerateContent", "countTokens"])
async def test_allowed_gemini_paths_reach_upstream(action):
    assert await _send("gemini", GEMINI, "POST", f"models/{GEMINI}:{action}?alt=sse", json={}) == (200, 1)


@pytest.mark.parametrize(("api", "method", "path", "kwargs", "status"), [
    ("openai", "POST", "chat/completions", {"json": {"model": "other"}}, 400),
    ("openai", "POST", "chat/completions", {"json": {}}, 400),
    ("openai", "POST", "chat/completions", {"content": b"not json"}, 400),
    ("openai", "POST", "chat/completions",
     {"json": {"model": MODEL}, "headers": {"content-encoding": "gzip"}}, 400),
    ("openai", "POST", "chat/completions?x=1", {"json": {"model": MODEL}}, 400),
    ("openai", "POST", "embeddings", {"json": {"model": MODEL}}, 404),
    ("openai", "GET", "models", {}, 405),
    ("openai", "POST", "chat%2Fcompletions", {"json": {"model": MODEL}}, 400),
    ("anthropic", "POST", "messages", {"json": {"model": "claude-other"}}, 400),
    ("gemini", "POST", "models/other:generateContent", {"json": {}}, 400),
    ("gemini", "POST", f"models/{GEMINI}:generateContent?key=x", {"json": {}}, 400),
    ("gemini", "POST", f"models/{GEMINI}:embedContent", {"json": {}}, 404),
])
async def test_rejections_never_reach_upstream(api, method, path, kwargs, status):
    model = GEMINI if api == "gemini" else MODEL
    assert await _send(api, model, method, path, **kwargs) == (status, 0)
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:tests/facades .venv/bin/pytest --import-mode=importlib tests/facades/test_model_allowlist.py -q -p no:cacheprovider`
Expected: FAIL (`api` unexpected keyword).

- [ ] **Step 3: Implement**

In `model.py` replace `_METHODS` with:

```python
_BODY_MODEL_PATHS = {
    "openai": frozenset({"chat/completions", "responses"}),
    "anthropic": frozenset({"messages", "messages/count_tokens"}),
}
_GEMINI_PATH = re.compile(r"models/([^/:]+):(generateContent|streamGenerateContent|countTokens)")


def check_model_request(api: str, model: str, method: str, tail: str, raw_path: bytes,
                        query: Mapping[str, str], headers: Mapping[str, str], body: bytes) -> tuple[int, str] | None:
    """(status, reason) when the request must not leave the facade; None when it may."""
    if method != "POST":
        return 405, "method not allowed"
    if b"%" in raw_path:
        return 400, "encoded path not allowed"
    if headers.get("content-encoding", "identity").lower() != "identity":
        return 400, "encoded body not allowed"
    if api == "gemini":
        match = _GEMINI_PATH.fullmatch(tail)
        if match is None:
            return 404, "path not allowed"
        if match.group(1) != model:
            return 400, "model not allowed"
        if set(query) - {"alt"} or query.get("alt", "sse") != "sse":
            return 400, "query not allowed"
        return None
    if tail not in _BODY_MODEL_PATHS[api]:
        return 404, "path not allowed"
    if query:
        return 400, "query not allowed"
    try:
        document = json.loads(body)
    except ValueError:
        return 400, "body is not JSON"
    if not isinstance(document, dict) or document.get("model") != model:
        return 400, "model not allowed"
    return None
```

Ctor gains `api: str` and `model: str` (stored). In `handle`, after authentication and before `join_tail`:

```python
        body = await request.body()
        rejected = check_model_request(
            self._api, self._model, request.method, request.path_params.get("tail", ""),
            request.scope.get("raw_path", b""), request.query_params, request.headers, body)
        if rejected is not None:
            status, reason = rejected
            return JSONResponse({"error": reason}, status_code=status)
```

and pass `body=body` to `forward`. Routes: keep both `Route`s with `methods=["GET", "POST", "PUT", "PATCH", "DELETE"]` so the handler (not Starlette) answers 405 with the JSON body; the `"/t/{token}"` route has tail `""` and returns 404. Imports: `json`, `re`, `Mapping`. In `control.py`: `ModelFacade(..., api=config.model.type, model=config.model.name, ...)`. Update existing tests that post a body without `model`.

- [ ] **Step 4: Run the facade and harness tests**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/facades tests/harness -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests
/usr/bin/git commit -q -m "feat(facades): model facade enforces configured model and path allow-list" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 6: Metrics, identity, redaction and unpriced cost in both modes

**Files:**
- Modify: `runtime/src/ach_runtime/harness/app.py` L121-130, `runtime/src/ach_runtime/observability/runs.py` L16-21, `runtime/src/ach_runtime/harness/control.py` (`_record_run` L738-749, end of `assemble`)
- Test: `runtime/tests/harness/test_app.py` (L64 stub), `runtime/tests/observability/test_runs.py`, `runtime/tests/harness/test_standalone_assembly.py`

**Interfaces:**
- Consumes: `config.identity_environment` (Task 2), `control.upstream_secrets` (Task 3).
- Produces: `runs.register_secrets(values: Iterable[str]) -> None`.

- [ ] **Step 1: Write the failing tests**

In `tests/observability/test_runs.py`:

```python
def test_registered_upstream_secrets_are_redacted(caplog):
    from ach_runtime.observability.runs import RunRecord, record_run, register_secrets

    register_secrets(["serper-secret-123456"])
    record = RunRecord("agent", "dev", "inv", "run", "actor serper-secret-123456", None, "webhook",
                       "ws", "sess", "version", "model", 1.0, "completed", None)
    with caplog.at_level(logging.INFO):
        record_run(record)
    assert "serper-secret-123456" not in caplog.text
```

(Match `RunRecord`'s positional order from the existing `test_completion_log_redacts_legacy_bearer_and_provider_tokens`; adjust the field carrying the secret if `actor` is not the fifth.)

In `tests/harness/test_app.py`, add a copy of `test_metrics_is_mounted_with_static_process_identity` whose stub config is `SimpleNamespace(channels=[], agent=SimpleNamespace(name="metrics-agent"), identity_environment="team-ns")` (no `ach`), asserting `/metrics/` returns 200 and contains `environment="team-ns"`; update the original stub to carry `identity_environment="metrics-env"` instead of `ach`.

In `tests/harness/test_standalone_assembly.py`:

```python
async def test_standalone_run_cost_is_unpriced_without_catalog_cost():
    control = await assemble(_config(), environ=ENV)
    try:
        assert control.catalog.model.metadata.input_cost_per_token is None
        assert control._priced is False
    finally:
        await control.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/observability/test_runs.py tests/harness/test_app.py tests/harness/test_standalone_assembly.py -q -p no:cacheprovider`
Expected: FAIL.

- [ ] **Step 3: Implement**

`runs.py`:

```python
_registered: tuple[str, ...] = ()
_MIN_REGISTERED_SECRET = 8


def register_secrets(values: Iterable[str]) -> None:
    """Add process secrets (resolved upstream headers) to log redaction. Values shorter
    than 8 characters are ignored: replacing them would corrupt ordinary text."""
    global _registered
    merged = set(_registered) | {value for value in values if len(value) >= _MIN_REGISTERED_SECRET}
    _registered = tuple(sorted(merged, key=len, reverse=True))


def redact(text: str) -> str:
    text = _SECRET.sub("[redacted]", text)
    for value in _registered:
        text = text.replace(value, "[redacted]")
    return text
```

`app.py` L121-130:

```python
    app.mount(
        "/metrics",
        make_asgi_app(registry=IdentityRegistry(identity=ProcessIdentity(
            agent=control.config.agent.name, environment=control.config.identity_environment))),
    )
```

`control.py`: at the end of `assemble`, `register_secrets(upstream_secrets)` and `control._priced = bool(catalog.model.metadata and catalog.model.metadata.input_cost_per_token is not None)`. In `_record_run`, `environment=self.config.identity_environment`, and when `not self._priced and usage is not None`, pass `dataclasses.replace(usage, cost_usd=None)` so `observe()` counts it under `COST_UNPRICED` instead of trusting the plugin's zero fallback. Set `_priced` wherever `Control` is built via `Control.__new__` in tests if a test fails on the missing attribute (default `True` via a class attribute `_priced = True`).

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/observability tests/harness -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests
/usr/bin/git commit -q -m "feat(observability): metrics and redaction in standalone, unpriced cost" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Task 7: Gateway outage handling

**Files:**
- Modify: `runtime/src/ach_runtime/harness/hosted.py` (`HostedSource` L51-, `create_host_app` L216-312), `runtime/src/ach_runtime/harness/workspaces.py` (`WorkspaceSource` Protocol L122-146, `_adopt_one` L579-603, new `gateway_available`), `runtime/src/ach_runtime/harness/local.py`, `runtime/tests/harness/fake_source.py`, `runtime/src/ach_runtime/harness/lifecycle.py` (L507-513), `runtime/src/ach_runtime/harness/delivery.py` (`start` L145-204), `runtime/src/ach_runtime/observability/runs.py` (`AnomalyKind`), `runtime/src/ach_runtime/harness/control.py` (`_alarm` known reasons L108-126), `runtime/docs/schemas/ach-runtime-contract-v3.md` (§5.3, §5.4)
- Test: `runtime/tests/harness/test_hosted.py`, `test_lifecycle.py`, `test_workspaces.py`, `test_delivery.py`

**Interfaces:**
- Produces:
  - Host API `PUT /host/v1/gateway`, body `{"available": bool}`, `204` on success, `422` on a bad body; the operator (Plan 2) calls it on every availability change and after each adoption inventory.
  - `HostedSource.gateway_available() -> bool`; `async HostedSource.wait_gateway(deadline: float) -> bool` (absolute monotonic deadline; True when available).
  - `WorkspaceSource.gateway_available() -> bool` (Protocol); `LocalSource` returns `True`; `FakeSource.gateway = True` attribute, method returns it.
  - `WorkspaceManager.gateway_available() -> bool`.
  - `AnomalyKind` gains `"gateway_unavailable"`.

- [ ] **Step 1: Write the failing tests**

`tests/harness/test_hosted.py` (use the file's `source`/`client`/`AUTH` fixtures):

```python
async def test_gateway_flag_is_set_by_the_host(source, client):
    assert source.gateway_available() is True
    response = await client.put("/gateway", json={"available": False}, headers=AUTH)
    assert response.status_code == 204
    assert source.gateway_available() is False
    assert (await client.put("/gateway", json={"available": "no"}, headers=AUTH)).status_code == 422


async def test_wait_gateway_wakes_when_the_gateway_returns(source, client):
    await client.put("/gateway", json={"available": False}, headers=AUTH)
    waiter = asyncio.ensure_future(source.wait_gateway(asyncio.get_running_loop().time() + 5))
    await asyncio.sleep(0)
    assert not waiter.done()
    await client.put("/gateway", json={"available": True}, headers=AUTH)
    assert await asyncio.wait_for(waiter, 1) is True
```

(If the fixture client's base URL already includes `/host/v1`, keep `/gateway`; otherwise use the prefix the other tests use.)

`tests/harness/test_lifecycle.py`: copy `test_owner_health_failure_calls_recover_under_operation_lock` into `test_health_failure_does_not_recover_while_gateway_unavailable`, give the `_Manager` stub `gateway_available = lambda self: False` (as a method), and assert `recover` was not called.

`tests/harness/test_workspaces.py`: copy `test_failed_adoption_keeps_the_slot_until_the_host_confirms` into `test_failed_adoption_is_deferred_while_gateway_unavailable`: set `h.source.gateway = False` before `await h.manager.adopt()`, assert `bad_name not in h.source.released`; then set `h.source.gateway = True`, clear `probe_fail`, call `h.manager.adopt()` again (or the deferred retry hook, see Step 3) and assert the unit is serving.

`tests/harness/test_delivery.py`:

```python
def _real_clock():
    started = time.monotonic()
    return lambda: NOW + timedelta(seconds=time.monotonic() - started)


async def test_connect_failure_is_retried_until_it_succeeds(public_config_fixture):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) < 3:
            raise httpx.ConnectError("refused", request=request)
        payload = json.loads(request.content)
        return pod_response(202, json={"invocationId": payload["invocationId"], "sessionRef": payload["sessionRef"],
                                       "runId": "run-1", "configVersion": payload["configVersion"]})

    delivery = make_delivery(handler, public_config_fixture, clock=_real_clock())
    started = await delivery.start(ticket(seconds=30))
    assert calls == ["/start", "/start", "/start"]
    assert started.run_id == "run-1"


async def test_connect_failure_until_deadline_fails_without_replay(public_config_fixture):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        raise httpx.ConnectError("refused", request=request)

    manager = Manager(public_config_fixture.agent.uid)
    manager.gateway_available = lambda: False
    delivery = make_delivery(handler, public_config_fixture, clock=_real_clock(), manager=manager)
    with pytest.raises(DeliveryError, match="gateway_unavailable"):
        await delivery.start(ticket(seconds=2))
    assert set(calls) == {"/start"}
```

Add `import time` and `from datetime import timedelta` if missing, and give the file's `Manager` stub a default `def gateway_available(self) -> bool: return True`. `ticket(seconds=...)` must compute its deadline from `NOW`; if it already does, the real clock makes the deadline expire in real seconds. Check `Started`'s attribute name for the run id (`run_id` or `runId`) and assert on that.

- [ ] **Step 2: Run to verify failure**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/harness/test_hosted.py tests/harness/test_lifecycle.py tests/harness/test_workspaces.py tests/harness/test_delivery.py -q -p no:cacheprovider`
Expected: the new tests FAIL.

- [ ] **Step 3: Implement**

`hosted.py` — in `HostedSource.__init__`: `self._gateway = True; self._gateway_back = asyncio.Event(); self._gateway_back.set()`. Methods:

```python
    def gateway_available(self) -> bool:
        return self._gateway

    def set_gateway(self, available: bool) -> None:
        self._gateway = available
        if available:
            self._gateway_back.set()
        else:
            self._gateway_back.clear()

    async def wait_gateway(self, deadline: float) -> bool:
        remaining = deadline - asyncio.get_running_loop().time()
        if self._gateway or remaining <= 0:
            return self._gateway
        try:
            await asyncio.wait_for(self._gateway_back.wait(), remaining)
        except TimeoutError:
            pass
        return self._gateway
```

Route in `create_host_app` (same auth and size checks as `PUT /inventory`):

```python
    async def put_gateway(request: Request) -> Response:
        body = await _json_body(request)          # the helper PUT /inventory uses; 422 on bad JSON
        available = body.get("available") if isinstance(body, dict) else None
        if not isinstance(available, bool):
            return JSONResponse({"error": "available must be a boolean"}, status_code=422)
        source.set_gateway(available)
        return Response(status_code=204)
```

registered as `Route("/gateway", put_gateway, methods=["PUT"])` beside `/inventory`. Use the deadline clock `place()` uses if it is not `loop.time()` (match `_startup_deadline`).

`workspaces.py`: add `def gateway_available(self) -> bool: ...` to the `WorkspaceSource` Protocol; `WorkspaceManager.gateway_available(self) -> bool: return self._source.gateway_available()`. In `_adopt_one`'s `except BaseException as exc:` (after re-raising `CancelledError`), before scheduling retirement:

```python
            if not self._source.gateway_available():
                log.warning("Workspace adoption deferred: gateway unavailable", extra={"workspace": placement.name})
                asyncio.ensure_future(self._adopt_when_gateway_returns(ref, placement))
                return
```

with

```python
    async def _adopt_when_gateway_returns(self, ref: WorkspaceRef, placement: Placement) -> None:
        wait = getattr(self._source, "wait_gateway", None)
        while not self._source.gateway_available():
            if wait is None:
                return
            await wait(asyncio.get_running_loop().time() + 30)
        await self._adopt_one(ref, placement)
```

(match the names `_adopt_one` takes; keep the task handle in the manager's existing background-task set if it has one, so `close()` cancels it). `LocalSource.gateway_available` returns `True`; `FakeSource` gets `gateway = True` and `gateway_available()` returning it.

`lifecycle.py` L507-513: before `await self.control.manager.recover(...)`:

```python
            if health_status != 200:
                if not self.control.manager.gateway_available():
                    return
                self._saw_activity = True
                await self.control.manager.recover(endpoint.ref, endpoint.instance_id)
                return
```

`delivery.py` `start`: wrap the first `_started` call:

```python
        delay = 0.5
        while True:
            try:
                return await self._started(endpoint, payload, ticket.deadline_at)
            except (httpx.ConnectError, httpx.ConnectTimeout) as error:
                # Nothing was sent: retrying is safe until the admission deadline.
                remaining = self._remaining(ticket.deadline_at)
                if remaining <= delay:
                    reason = ("gateway_unavailable" if not self._manager.gateway_available()
                              else "workspace unreachable")
                    raise DeliveryError(reason) from error
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)
            except (httpx.TransportError, TimeoutError) as first_error:
                ...  # existing same-instance replay block, unchanged
```

(use the attribute name `Delivery` already holds for the manager.) `runs.py`: add `"gateway_unavailable"` to `AnomalyKind`. `control.py` `_alarm` known reasons: map `"gateway_unavailable"` -> `"gateway_unavailable"`. Contract doc §5.3: document `PUT /host/v1/gateway`; §5.4: host obligation "report gateway availability on every change and after adoption; the runtime defers recovery and retirement while it is false".

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness .venv/bin/pytest --import-mode=importlib tests/harness tests/observability -q -p no:cacheprovider`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
/usr/bin/git add -A runtime/src runtime/tests runtime/docs
/usr/bin/git commit -q -m "feat(harness): defer recovery and retry unsent starts while the gateway is down" -m "Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01Qwpo6cYcvZjm7NUn5DbYmA"
```

---

### Final gate (after Task 7, alongside the final review)

```bash
cd /home/coder/workspace/local/ach-agent/runtime
make test-unit > /tmp/ach-agent-unit.log 2>&1; echo "EXIT=$?"
grep -E "passed|failed|error" /tmp/ach-agent-unit.log | tail -3
make schema-check; echo "EXIT=$?"
grep -rn "config\.ach\b\|workspace-v2" src tests examples docs/schemas scripts || echo "clean"
```

Expected: both `EXIT=0`, and `clean`.
