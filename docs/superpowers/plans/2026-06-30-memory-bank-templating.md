# Memory Bank Rename + `{{ }}` Templating Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename `memory.scope` → `memory.bank` (static bank_id, closing the deferred contract item) and add a deterministic, zero-dependency `{{ }}` template engine wired to its first real consumer: per-channel `channel.prompt` substitution.

**Architecture:** A new pure `ach_agent.templating` module resolves `{{ namespace.path | default("x") }}` tokens against a context dict assembled per-invocation from the `MessageEvent` payload + harness internals. There is **no `env` namespace** — process env (where the `ek_` lives) is structurally unreachable from a template. `build_engine_prompt` consults `channel.prompt` and renders it through this engine; when `channel.prompt` is unset, the existing hardcoded fallback is preserved. The memory adapter reads the renamed `MemoryBlock.bank` as its `bank_id`.

**Tech Stack:** Python 3.12 + asyncio, Pydantic v2 (`extra='forbid'`, `populate_by_name`), structlog, pytest (`asyncio_mode=auto`), uv / ruff / mypy.

## Global Constraints

- venv only — run everything via `uv run ...`; never system-wide `pip install`.
- `uv run mypy src` is authoritative for types (Pyright LSP diagnostics in this repo are FALSE — wrong interpreter).
- All Pydantic models keep `ConfigDict(extra="forbid")`; camelCase contract keys use `alias=` + `populate_by_name=True`.
- **ek-hygiene (CONTRACT §3):** the `ek_` (`ACH_TOKEN`/`ACH_API_KEY`) is NEVER logged, NEVER reaches opencode, NEVER appears in a template namespace. The template engine MUST NOT expose an `env`/process-environment namespace.
- Contract stays **backend-agnostic**: no hindsight-specific concepts (`observation_scopes`, `document_id`, `tags_match`) enter the schema or contract doc in this plan.
- DRY, YAGNI, TDD, frequent commits. One commit per task.
- Lint/format gate before each commit: `uv run ruff check .` then `uv run ruff format --check .`.

## Out of scope — deferred to the memory-tags epic (BLOCKED, do NOT build here)

The per-event memory **tag** system (identity-tag stamping, `memoryTags` config, observation_scopes, stable `document_id`, recall auto-scoping by tags) is intentionally **excluded**. It is blocked on: a memory *retain* path (none exists harness-side — `adapter.py` is recall-only), a *stateful* JSON-RPC-parsing proxy with session→event correlation (`mcp_proxy.py` is a transparent pass-through that cannot do either), and a locked memory backend. The full design is captured in the memory note `ach-agent-memory-bank-tags-design`. The pure `resolve_path` primitive built in Task 2 is the deliberate substrate that epic will reuse — its tag-omit semantics live in that future resolver, not in this engine.

## File Structure

- `src/ach_agent/templating/__init__.py` (NEW) — package marker, re-exports.
- `src/ach_agent/templating/render.py` (NEW) — `resolve_path`, `render_template`, `build_template_context`. Pure, zero-dependency.
- `src/ach_agent/config/schema.py` (MODIFY) — `MemoryBlock.scope` → `MemoryBlock.bank`.
- `src/ach_agent/memory/adapter.py` (MODIFY) — `bank_id = memory_cfg.bank`.
- `src/ach_agent/main.py` (MODIFY) — `build_engine_prompt` consults `channel.prompt` via the engine; `_make_engine_runner` accepts `channels_by_name` + `agent_name` + `memory_bank`; boot wiring builds the map.
- `tests/templating/test_render.py` (NEW) — engine unit tests.
- `tests/config/test_schema.py` (MODIFY) — `bank` field assertions.
- `tests/memory/test_memory_adapter.py` (MODIFY) — `bank` → `bank_id` derivation.
- `tests/test_build_engine_prompt.py` (NEW or extend existing main test) — `channel.prompt` rendering.
- `docs/plan/CONTRACT_v3.md`, `docker/sample-config.yaml`, `docker/quickstart/config.yaml`, `CHANGELOG.md` (MODIFY) — docs reconcile.

---

## Task 1: Rename `memory.scope` → `memory.bank`

**Files:**
- Modify: `src/ach_agent/config/schema.py:106-116` (`MemoryBlock`)
- Modify: `src/ach_agent/memory/adapter.py:90-133` (`prepare_memory`), and the docstring at `:7-8`, `:98`
- Test: `tests/config/test_schema.py`, `tests/memory/test_memory_adapter.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `MemoryBlock.bank: str` (was `scope`). `prepare_memory` still returns `tuple[bool, str]`; internally `bank_id = memory_cfg.bank`.

- [ ] **Step 1: Find every `scope` reference**

Run: `grep -rn "scope" src/ach_agent/ tests/ docs/plan/CONTRACT_v3.md docker/`
Expected: hits in `config/schema.py` (field + deprecation comment), `memory/adapter.py` (docstrings + `bank_id = memory_cfg.scope`), the CONTRACT doc (`"scope": "{project_id}"`), and any test fixtures/asserts (including `tests/conformance/`). Record them — every one must move to `bank` (docs in Task 4).

- [ ] **Step 2: Write the failing schema test**

In `tests/config/test_schema.py`, add:

```python
def test_memory_block_uses_bank_not_scope():
    from ach_agent.config.schema import MemoryBlock
    from pydantic import ValidationError
    import pytest

    m = MemoryBlock(endpoint="http://mem:8080", bank="gitlab-pr-review")
    assert m.bank == "gitlab-pr-review"

    # the old key is gone — extra='forbid' must reject it
    with pytest.raises(ValidationError):
        MemoryBlock(endpoint="http://mem:8080", scope="x")
```

- [ ] **Step 3: Run it to verify it fails**

Run: `uv run pytest tests/config/test_schema.py::test_memory_block_uses_bank_not_scope -v`
Expected: FAIL — `MemoryBlock` has no `bank` field / `scope` is accepted.

- [ ] **Step 4: Rename the field in the schema**

In `src/ach_agent/config/schema.py`, replace the `MemoryBlock` body with:

```python
class MemoryBlock(BaseModel):
    """CONTRACT §2 memory block (fail-open — §31)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    endpoint: str
    mission: str = ""
    # Static memory bank_id (the memory namespace for this agent's mission, e.g.
    # "gitlab-pr-review"). Per-event tag-based partitioning is a separate future layer
    # (see the memory bank+tags design note) and does NOT change this static field.
    bank: str = ""
    mental_models: list[str] = Field(default_factory=list, alias="mentalModels")
```

- [ ] **Step 5: Update the adapter to read `bank`**

In `src/ach_agent/memory/adapter.py`, change `bank_id = memory_cfg.scope` to:

```python
        bank_id = memory_cfg.bank
```

Then fix the stale docstrings: change `use MemoryBlock.scope as bank_id` → `use MemoryBlock.bank as bank_id`; change `Decision (Task 1: derive-from-scope): bank_id = memory_cfg.scope.` → `bank_id = memory_cfg.bank (static, operator config — never from inbound payload).`

- [ ] **Step 6: Update the existing memory-adapter + conformance tests**

Run `grep -rn "scope" tests/` and replace every `MemoryBlock(... scope=...)` / `.scope` with `bank=` / `.bank` — this includes `tests/memory/test_memory_adapter.py` AND `tests/conformance/test_inv05_memory_fail_open.py`.

- [ ] **Step 7: Run the schema + memory + conformance tests, then the full suite**

Run: `uv run pytest tests/config/test_schema.py tests/memory/ tests/conformance/ -v` then `uv run pytest -q`
Expected: PASS (no remaining `scope` ValidationError anywhere).

- [ ] **Step 8: Type-check + lint**

Run: `uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 9: Commit**

```bash
git add src/ach_agent/config/schema.py src/ach_agent/memory/adapter.py tests/config/test_schema.py tests/memory/ tests/conformance/test_inv05_memory_fail_open.py
git commit -m "refactor(memory): rename memory.scope -> memory.bank (static bank_id)"
```

---

## Task 2: The `{{ }}` template engine

**Files:**
- Create: `src/ach_agent/templating/__init__.py`
- Create: `src/ach_agent/templating/render.py`
- Test: `tests/templating/test_render.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `resolve_path(context: dict[str, Any], dotted_path: str) -> str | None` — pure traversal; scalar→str, missing/non-scalar→None.
  - `render_template(template: str, context: dict[str, Any]) -> str` — substitutes `{{ path }}` / `{{ path | default("x") }}`.
  - `build_template_context(payload, *, channel_name, channel_type, channel_source, agent_name, memory_bank, event_id, session_key) -> dict[str, Any]`.

- [ ] **Step 1: Create the test directory + failing tests**

Create `tests/templating/test_render.py`:

```python
from ach_agent.templating.render import (
    build_template_context,
    render_template,
    resolve_path,
)


def _ctx():
    return build_template_context(
        {"project": {"full_name": "backend/payments"}, "commits": [{"id": "abc"}]},
        channel_name="gitlab-mr",
        channel_type="webhook",
        channel_source="gitlab",
        agent_name="reviewer",
        memory_bank="gitlab-pr-review",
        event_id="evt-1",
        session_key="ses-1",
    )


def test_resolve_payload_nested():
    assert resolve_path(_ctx(), "payload.project.full_name") == "backend/payments"


def test_resolve_list_index():
    assert resolve_path(_ctx(), "payload.commits.0.id") == "abc"


def test_resolve_internal_namespace():
    ctx = _ctx()
    assert resolve_path(ctx, "internal.channel.name") == "gitlab-mr"
    assert resolve_path(ctx, "internal.channel.source") == "gitlab"
    assert resolve_path(ctx, "internal.agent.name") == "reviewer"
    assert resolve_path(ctx, "internal.memory.bank") == "gitlab-pr-review"


def test_resolve_missing_returns_none():
    assert resolve_path(_ctx(), "payload.nope.deeper") is None


def test_resolve_non_scalar_returns_none():
    # a dict/list is not a usable scalar
    assert resolve_path(_ctx(), "payload.project") is None
    assert resolve_path(_ctx(), "payload.commits") is None


def test_resolve_env_namespace_is_unreachable():
    # there is NO env namespace — ek-hygiene at the template layer
    assert resolve_path(_ctx(), "env.ACH_TOKEN") is None


def test_render_substitutes_scalar():
    out = render_template("Review {{ payload.project.full_name }} now", _ctx())
    assert out == "Review backend/payments now"


def test_render_whitespace_insensitive():
    assert render_template("{{payload.project.full_name}}", _ctx()) == "backend/payments"


def test_render_default_used_when_missing():
    out = render_template('{{ payload.missing | default("unknown") }}', _ctx())
    assert out == "unknown"


def test_render_missing_no_default_becomes_empty():
    out = render_template("x={{ payload.missing }};", _ctx())
    assert out == "x=;"


def test_render_internal_event_and_session():
    out = render_template("{{ internal.event.id }}/{{ internal.session.key }}", _ctx())
    assert out == "evt-1/ses-1"


def test_header_namespace_reserved_empty():
    # headers are not threaded across the seam yet — reserved, resolves missing
    assert resolve_path(_ctx(), "header.x-api-key") is None
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/templating/test_render.py -v`
Expected: FAIL — `ach_agent.templating` does not exist (ModuleNotFoundError).

- [ ] **Step 3: Create the package marker**

Create `src/ach_agent/templating/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Deterministic {{ }} template substitution (zero-dependency, no env exposure)."""

from ach_agent.templating.render import (
    build_template_context,
    render_template,
    resolve_path,
)

__all__ = ["build_template_context", "render_template", "resolve_path"]
```

- [ ] **Step 4: Implement the engine**

Create `src/ach_agent/templating/render.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Deterministic {{ }} template substitution for config-authored strings.

Greenfield, zero-dependency. Pure dict/list path traversal — NOT jinja, NOT eval:
no logic, no loops, no attribute access, no method calls. One filter: default("literal").

Namespaces (roots of the context dict): `payload`, `header`, `internal`. There is NO
`env` namespace — process env (where the ek_ lives) is structurally unreachable from a
template. That is the ek-hygiene guarantee at the template layer (CONTRACT §3).

Consumer: channel.prompt substitution (main.build_engine_prompt). The pure `resolve_path`
primitive is the deliberate substrate for the future per-event memory-tag resolver (see the
memory bank+tags design note); tag-omit semantics live in that resolver, not here.
"""
from __future__ import annotations

import re
from typing import Any

import structlog

log = structlog.get_logger(__name__)

# {{ ns.a.b.c }}  or  {{ ns.a.b | default("x") }} — whitespace inside braces ignored.
_TOKEN_RE = re.compile(
    r"\{\{\s*"
    r"([A-Za-z0-9_.\-]+)"  # group 1: dotted path
    r'(?:\s*\|\s*default\(\s*"([^"]*)"\s*\))?'  # group 2: optional default literal
    r"\s*\}\}"
)


def resolve_path(context: dict[str, Any], dotted_path: str) -> str | None:
    """Resolve a dotted path against the context. Return the scalar as str, or None.

    None means a segment was missing OR the value is a container (dict/list) or null —
    not a usable scalar. Pure traversal: dict keys and list integer indices only.
    """
    cur: Any = context
    for seg in dotted_path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        elif isinstance(cur, list):
            if not (seg.isdigit() and int(seg) < len(cur)):
                return None
            cur = cur[int(seg)]
        else:
            return None
    # bool is a subclass of int — both are acceptable scalars.
    if isinstance(cur, (str, int, float, bool)):
        return str(cur)
    return None


def render_template(template: str, context: dict[str, Any]) -> str:
    """Substitute every {{ path }} / {{ path | default("x") }} token.

    Per token: scalar found → its value; missing/non-scalar with default → default;
    missing/non-scalar without default → empty string (logged).
    """

    def _sub(m: re.Match[str]) -> str:
        path = m.group(1)
        default = m.group(2)
        value = resolve_path(context, path)
        if value is not None:
            return value
        if default is not None:
            return default
        log.warning("template: unresolved token -> empty", path=path)
        return ""

    return _TOKEN_RE.sub(_sub, template)


def build_template_context(
    payload: dict[str, Any],
    *,
    channel_name: str,
    channel_type: str,
    channel_source: str,
    agent_name: str,
    memory_bank: str,
    event_id: str,
    session_key: str,
) -> dict[str, Any]:
    """Assemble the substitution context.

    `header` is reserved (empty) until inbound headers are threaded across the
    channel->router seam (deferred — current seam drops them by design).
    """
    return {
        "payload": payload,
        "header": {},
        "internal": {
            "channel": {
                "name": channel_name,
                "type": channel_type,
                "source": channel_source,
            },
            "agent": {"name": agent_name},
            "memory": {"bank": memory_bank},
            "event": {"id": event_id},
            "session": {"key": session_key},
        },
    }
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/templating/test_render.py -v`
Expected: PASS (all 12 tests).

- [ ] **Step 6: Type-check + lint**

Run: `uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: clean.

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/templating/ tests/templating/
git commit -m "feat(templating): zero-dep {{ }} substitution engine (payload/internal, no env)"
```

---

## Task 3: Wire the engine into `channel.prompt`

**Files:**
- Modify: `src/ach_agent/main.py` — `build_engine_prompt` (consults `channel.prompt`), `_make_engine_runner` (new params), the `build_engine_prompt` call site, and the boot wiring that calls `_make_engine_runner`.
- Test: `tests/test_build_engine_prompt.py` (NEW)

**Interfaces:**
- Consumes: `render_template`, `build_template_context` (Task 2); `MemoryBlock.bank` (Task 1).
- Produces: `build_engine_prompt(event: MessageEvent, channel_cfg=None, agent_name: str = "", memory_bank: str = "") -> str` — renders `channel_cfg.prompt` when set, else the existing fallback. `_make_engine_runner(..., channels_by_name: dict[str, ChannelConfig] | None = None, agent_name: str = "", memory_bank: str = "")`.

NOTE on the codebase (verify with `sed`/`grep` first — exact line numbers may have shifted after the rebase onto main):
- `build_engine_prompt(event: MessageEvent) -> str:` is defined in `main.py` (cron `scheduled_tick` path, free-form `payload['text']` path, webhook MR path). Its ONLY call site is inside `engine_runner` (`base_prompt = build_engine_prompt(event)`).
- `_make_engine_runner(pool, engine_cfg, max_invocation_seconds, terminal_output_retries=1, memory_cfg=None, channel_ttl=None)` is defined in `main.py`; `engine_runner` is the inner async closure it returns.
- The boot call to `_make_engine_runner(...)` is in `main.py`'s startup path where `cfg` (the `AgentConfig`) is in scope.

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_engine_prompt.py`:

```python
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, WebhookBlock
from ach_agent.main import build_engine_prompt


def _webhook_channel(prompt: str | None) -> ChannelConfig:
    return ChannelConfig(
        name="gitlab-mr",
        type="webhook",
        source="gitlab",
        prompt=prompt,
        webhook=WebhookBlock(),
    )


def test_channel_prompt_template_is_rendered():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={"object_attributes": {"url": "https://gl/mr/7"}},
    )
    ch = _webhook_channel("Review this merge request: {{ payload.object_attributes.url }}")
    out = build_engine_prompt(event, channel_cfg=ch)
    assert out == "Review this merge request: https://gl/mr/7"


def test_channel_prompt_internal_namespace():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={},
    )
    ch = _webhook_channel("agent={{ internal.agent.name }} bank={{ internal.memory.bank }}")
    out = build_engine_prompt(event, channel_cfg=ch, agent_name="rev", memory_bank="b1")
    assert out == "agent=rev bank=b1"


def test_no_channel_prompt_falls_back_to_text_payload():
    # console / queue path: payload['text'] is returned verbatim (existing behavior)
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="console",
        payload={"text": "hello there"},
    )
    assert build_engine_prompt(event) == "hello there"


def test_unset_channel_prompt_uses_fallback():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={"scheduled_tick": "tick-9"},
    )
    ch = _webhook_channel(None)
    # prompt is None on the channel -> fall through to existing cron/scheduled logic
    assert build_engine_prompt(event, channel_cfg=ch) == "tick-9"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_build_engine_prompt.py -v`
Expected: FAIL — `build_engine_prompt()` got an unexpected keyword argument `channel_cfg`.

- [ ] **Step 3: Add the import and rewrite `build_engine_prompt`**

In `src/ach_agent/main.py`, add near the other `ach_agent` imports:

```python
from ach_agent.templating import build_template_context, render_template
```

Replace `build_engine_prompt` with a version that consults `channel.prompt` first, then falls through to the unchanged fallback body. Keep the ENTIRE existing fallback (cron `scheduled_tick`, free-form `payload['text']`, webhook MR build) byte-for-byte below the new branch:

```python
def build_engine_prompt(
    event: MessageEvent,
    channel_cfg: Any = None,
    agent_name: str = "",
    memory_bank: str = "",
) -> str:
    """Build a meaningful engine prompt from a MessageEvent.

    When the channel declares a `prompt` template, it wins: it is rendered through the
    {{ }} engine against the event payload + harness internals (channel.prompt is the
    contract-specified per-channel instruction). Otherwise the legacy fallback applies:
    cron `scheduled_tick`, free-form `payload['text']`, or a built MR review instruction.

    Never raises; falls back to an empty string if no usable content is found.
    """
    # Channel-prompt path: render the contract-authored template (CONTRACT §2 channel.prompt)
    if channel_cfg is not None and getattr(channel_cfg, "prompt", None):
        ctx = build_template_context(
            event.payload,
            channel_name=event.channel_name,
            channel_type=getattr(channel_cfg, "type", "") or "",
            channel_source=getattr(channel_cfg, "source", "") or "",
            agent_name=agent_name,
            memory_bank=memory_bank,
            event_id=event.idempotency_key,
            session_key=event.session_key,
        )
        return render_template(channel_cfg.prompt, ctx)

    # <<< KEEP the existing fallback body exactly as-is from here down >>>
```

(Append the current `build_engine_prompt` body unchanged after the new branch — the cron path, the free-form text path, and the webhook MR path. Do not alter that logic.)

- [ ] **Step 4: Run the unit test (call-site not yet threaded)**

Run: `uv run pytest tests/test_build_engine_prompt.py -v`
Expected: PASS — the function now accepts `channel_cfg`/`agent_name`/`memory_bank`.

- [ ] **Step 5: Thread the channel map through `_make_engine_runner`**

Add three params to `_make_engine_runner` after `channel_ttl`:

```python
    channels_by_name: dict[str, Any] | None = None,
    agent_name: str = "",
    memory_bank: str = "",
```

Add right after `ttl_by_channel = channel_ttl or {}`:

```python
    channels_by_name = channels_by_name or {}
```

Update the call site from `base_prompt = build_engine_prompt(event)` to:

```python
                base_prompt = build_engine_prompt(
                    event,
                    channel_cfg=channels_by_name.get(event.channel_name),
                    agent_name=agent_name,
                    memory_bank=memory_bank,
                )
```

- [ ] **Step 6: Pass the map + agent/bank at boot**

At the `_make_engine_runner(...)` boot call, build the map + bank just before the call (read the existing call with `sed` first and append the three new kwargs):

```python
    channels_by_name = {c.name: c for c in cfg.channels}
    memory_bank = cfg.memory.bank if cfg.memory is not None else ""
```

then pass `channels_by_name=channels_by_name, agent_name=cfg.agent.name, memory_bank=memory_bank`.

- [ ] **Step 7: Full suite + types + lint**

Run: `uv run pytest -q && uv run mypy src && uv run ruff check . && uv run ruff format --check .`
Expected: PASS / clean. (Existing console/cron/webhook prompt tests still green — the fallback path is unchanged when no `channel.prompt` is set.)

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/main.py tests/test_build_engine_prompt.py
git commit -m "feat(engine): render channel.prompt via {{ }} engine; static fallback preserved"
```

---

## Task 4: Reconcile contract + sample configs + changelog

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (memory block + channel.prompt example syntax)
- Modify: `docker/sample-config.yaml`, `docker/quickstart/config.yaml`
- Modify: `CHANGELOG.md` (`[unreleased]`)

**Interfaces:**
- Consumes: the field/behavior names finalized in Tasks 1-3.
- Produces: docs that match the code (no `scope`, `bank` documented; `{{ payload.* }}` namespace syntax for `channel.prompt`).

- [ ] **Step 1: Update the CONTRACT memory block**

In `docs/plan/CONTRACT_v3.md`, change the memory block so `scope` becomes `bank` and drops the per-event placeholder:

```jsonc
  "memory": {                               // null if not configured; fail-open (§31)
    "endpoint": "http://hindsight.engineering.svc:8080",
    "mission": "AI code reviewer…", "bank": "gitlab-pr-review",   // static memory bank_id
    "mentalModels": ["architecture", "conventions", "recurring-issues"]
  },
```

- [ ] **Step 2: Fix the channel.prompt example syntax**

In `docs/plan/CONTRACT_v3.md`, change the Go-template-style leading-dot placeholder to the harness namespace syntax:

```jsonc
      "prompt": "Review this merge request: {{ payload.object_attributes.url }}",
```

Add one clarifying line near the channels block documenting the namespaces:

```jsonc
      // channel.prompt is rendered with {{ }} substitution. Namespaces: payload.* (the
      // inbound JSON body), internal.* (channel.name|type|source, agent.name, memory.bank,
      // event.id, session.key). One filter: | default("x"). No env namespace (ek-hygiene).
```

- [ ] **Step 3: Update the sample configs**

In `docker/sample-config.yaml`, add a commented memory example near the `prompt:` section:

```yaml
# Optional memory (fail-open). `bank` is the static memory bank_id (the agent's mission
# namespace). Per-event tag partitioning is a separate future layer — not configured here.
# memory:
#   endpoint: http://hindsight.engineering.svc:8080
#   mission: "AI code reviewer"
#   bank: gitlab-pr-review
#   mentalModels: [architecture, conventions]
```

Run `grep -n "scope" docker/sample-config.yaml docker/quickstart/config.yaml` and rename any real `scope:` memory key to `bank:`.

- [ ] **Step 4: Add the CHANGELOG entries**

In `CHANGELOG.md`, under `## [unreleased]`, under `### Changed`:

```markdown
- **`memory.scope` renamed to `memory.bank`** — the static memory bank_id (the agent's
  mission namespace, e.g. `gitlab-pr-review`). Per-event tag-based partitioning is a
  separate future layer and does not affect this field.
- **`channel.prompt` is now rendered** through a zero-dependency `{{ }}` substitution
  engine. Namespaces: `payload.*` (inbound JSON body) and `internal.*` (`channel.name`/
  `type`/`source`, `agent.name`, `memory.bank`, `event.id`, `session.key`); one filter,
  `| default("x")`. There is no `env` namespace — process env (the `ek_`) is structurally
  unreachable from a template (ek-hygiene at the template layer). Channels without a
  `prompt` keep the previous built-in instruction behavior unchanged.
```

- [ ] **Step 5: Verify no stray memory `scope` references remain**

Run: `grep -rn "memory.*scope\|\"scope\"\|\.scope\b" src/ ach_agent docs/plan/CONTRACT_v3.md docker/ 2>/dev/null`
Expected: no memory-related `scope` hits remain (router/session `scope` usages, if any, are unrelated — confirm each).

- [ ] **Step 6: Commit**

```bash
git add docs/plan/CONTRACT_v3.md docker/sample-config.yaml docker/quickstart/config.yaml CHANGELOG.md
git commit -m "docs: reconcile memory.bank rename + channel.prompt {{ }} namespaces"
```

---

## Self-Review

**Spec coverage** (against the memory note `ach-agent-memory-bank-tags-design` + the discussion):
- `memory.scope` → `memory.bank` (static) — Task 1. ✓
- `{{ }}` engine, `payload`/`internal` namespaces, `| default()`, missing→empty/None, no `env` — Task 2. ✓
- `header.*` namespace — **reserved/deferred** (seam doesn't carry headers); engine returns missing for it, documented — Task 2 + Task 4. ✓ (intentional scope cut)
- `channel.prompt` substitution as the engine's live consumer — Task 3. ✓
- Identity-tag stamping / `memoryTags` / retain / observation_scopes / document_id / recall-by-tags — **deferred epic**, documented in the out-of-scope section + memory note. ✓ (intentional)

**Placeholder scan:** no TBD/TODO/"handle edge cases" — every code step shows complete code. ✓

**Type consistency:** `resolve_path`/`render_template`/`build_template_context` signatures match between Task 2 definition and Task 3 usage; `build_engine_prompt`'s new params (`channel_cfg`, `agent_name`, `memory_bank`) match the `_make_engine_runner` call site and the boot wiring; `MemoryBlock.bank` is the single name used in schema, adapter, tests, and docs. ✓
