# Plan 1 — De-cruft & Reconnect opencode to the v3 Config

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Get the tree green on **opencode + the v3 config**, removing all Codex/v2/slack/telegram cruft, with the engine reconnected to the reshaped `model{name,type,params}` block and a single-object terminal contract.

**Architecture:** The opencode bridge in `src/ach_agent/engine/` already exists and stays. This plan (a) deletes the slack/telegram channels + Hermes dep + the v2 delivery layer (`actions/`), (b) reshapes `ModelBlock` to `{name,type,params}` and rewires `EngineConfig` from it (deleting the dead `cfg.engine.*` reads), (c) reshapes the terminal contract from a `{"actions":[...]}` list to a single `{action,text,thoughts}` object and retires harness-side delivery (egress is the agent's via MCP), and (d) removes the Phase-2 mypy override + `ResponseActionBlock` alias and re-greens the suite. Localhost proxy, hydration, queue/tui, and a2a egress are **later plans** — Plan 1 keeps the current ACH_BASE_URL/ACH_API_KEY-env opencode wiring.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, FastAPI, pytest(+asyncio), ruff, mypy, uv.

## Global Constraints

- Always run via the project venv: `uv run <cmd>` (never system pip).
- `mypy --strict src` and `ruff check + format --check src` must stay green (`make lint`).
- Router (`src/ach_agent/router/`) is the IP — **do not modify it** in this plan.
- Engine must never import the router or hermes (`D-08`/`RTR-06`).
- Secrets: config carries paths, never values; `ek_` never logged.
- Conventional commits; commit after each task.
- `schemaVersion` is `"1"` (already in `schema.py`); do not reintroduce `"3"`.
- Terminal contract is a **single object** `{action,text,thoughts}` — NOT a list.

---

### Task 1: Reshape `ModelBlock` to `{name, type, params}`

**Files:**
- Modify: `src/ach_agent/config/schema.py:36-42` (ModelBlock)
- Modify: `tests/config/test_schema.py` (CFG-05 model assertions)
- Modify: `tests/config/fixtures/config_webhook.json`, `config_cron.json`, `config_queue.json`, `config_tui.json`, `config_a2a.json` (the `model` section)

**Interfaces:**
- Produces: `ModelBlock(name: str, type: Literal["openai","gemini","anthropic"], params: dict[str, Any])`. Consumed by `AgentConfig.model` and (Task 2) `EngineConfig`.

- [ ] **Step 1: Write the failing test**

In `tests/config/test_schema.py`, replace the CFG-05 `model.reasoningEffort` assertions with the new shape. Add:

```python
def test_model_block_name_type_params(tmp_path):
    cfg = _load_fixture(tmp_path, "config_webhook.json")
    assert cfg.model.name == "openai.gpt-5"
    assert cfg.model.type == "openai"
    assert cfg.model.params == {"temperature": 1}

def test_model_type_rejects_unknown_provider(tmp_path):
    raw = _read_fixture("config_webhook.json")
    raw["model"] = {"name": "x", "type": "bedrock", "params": {}}
    with pytest.raises(SystemExit):
        _load_raw(tmp_path, raw)

def test_model_params_is_open_dict(tmp_path):
    raw = _read_fixture("config_webhook.json")
    raw["model"] = {"name": "g", "type": "gemini", "params": {"thinking_level": "medium", "x": 1}}
    cfg = _load_raw(tmp_path, raw)
    assert cfg.model.params["thinking_level"] == "medium"
```

(Use the file's existing `_load_fixture` / `_read_fixture` / `_load_raw` helpers; if they are not present, read the top of `test_schema.py` and reuse its load pattern — the suite already loads fixtures via `load_config`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/config/test_schema.py -q`
Expected: FAIL — `ModelBlock` has no `name`/`type`/`params`; `model.selected` required.

- [ ] **Step 3: Reshape `ModelBlock`**

In `src/ach_agent/config/schema.py`, add `Any` to the typing import (`from typing import Any, Literal`) and replace the `ModelBlock` class:

```python
class ModelBlock(BaseModel):
    """CONTRACT_v3 §2 model block: provider-selecting name + open params."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str                                            # e.g. "openai.gpt-5"; passed verbatim
    type: Literal["openai", "gemini", "anthropic"]       # selects the ACH compat endpoint
    params: dict[str, Any] = Field(default_factory=dict)  # open, unvalidated, splatted to the client
```

- [ ] **Step 4: Update the five fixtures**

In each `tests/config/fixtures/config_*.json`, replace the `"model"` block with:

```json
"model": { "name": "openai.gpt-5", "type": "openai", "params": { "temperature": 1 } }
```

(For `config_a2a.json` etc. you may vary `type`/`name` if a test asserts it; default the above.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/config/test_schema.py -q`
Expected: PASS (all config tests green).

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/config/schema.py tests/config/test_schema.py tests/config/fixtures/
git commit -m "feat(config): reshape ModelBlock to {name,type,params}"
```

---

### Task 2: Drop slack/telegram + Hermes; reconnect `EngineConfig` from the v3 model block

**Files:**
- Delete: `src/ach_agent/channels/slack.py`, `src/ach_agent/channels/telegram.py`
- Modify: `pyproject.toml:31` (remove Hermes dep)
- Modify: `src/ach_agent/main.py` (imports, `WIRED_CHANNEL_TYPES`, EngineConfig construction, drop slack/telegram branches, drop `response_action_config_by_channel`)
- Modify: `src/ach_agent/engine/lifecycle.py` (`EngineConfig`: drop `binary_path`-from-config coupling; add `params`)

**Interfaces:**
- Consumes: `ModelBlock` (Task 1).
- Produces: `EngineConfig(model: str, provider: str, params: dict[str, Any], work_dir, session_dir, startup_timeout_seconds, max_invocation_seconds, shared_ttl_seconds, mcp_servers)`. `engine_runner` (Task 3) consumes it.

- [ ] **Step 1: Delete slack/telegram source + tests**

```bash
git rm src/ach_agent/channels/slack.py src/ach_agent/channels/telegram.py \
       tests/channels/test_slack.py tests/channels/test_telegram.py \
       tests/e2e/test_slack_e2e.py tests/e2e/test_telegram_e2e.py
```

- [ ] **Step 2: Remove the Hermes dependency**

In `pyproject.toml`, delete line 31 (the `hermes-agent[messaging] @ git+...` entry) from `dependencies`. Then:

Run: `uv lock && uv sync`
Expected: lock updates, hermes removed.

- [ ] **Step 3: Add `params` to `EngineConfig`**

In `src/ach_agent/engine/lifecycle.py`, add to `EngineConfig` (after `model`):

```python
    params: dict[str, object] = field(default_factory=dict)  # model params (temperature, thinking_level, …)
```

And in `write_opencode_config`, pass params into the provider model options (merge into the existing `provider[config.provider]["options"]` dict):

```python
        "provider": {
            config.provider: {
                "options": {
                    "apiKey": "{env:ACH_API_KEY}",
                    "baseURL": "{env:ACH_BASE_URL}",
                    **config.params,
                }
            }
        },
```

- [ ] **Step 4: Rewire `main.py`**

In `src/ach_agent/main.py`:

a) Remove the slack/telegram imports (lines 42-43) and the `ResponseActionBlock` import path is the alias on line 52 — leave the alias for now (removed in Task 5).

b) Trim `WIRED_CHANNEL_TYPES` (line 62):

```python
WIRED_CHANNEL_TYPES: frozenset[str] = frozenset({"cron", "webhook", "a2a"})
```

c) Replace the `EngineConfig(...)` construction (lines 465-475) — it must read the v3 model block + top-level fields, NOT `cfg.engine.*` / `cfg.model.default/provider`:

```python
    engine_cfg = EngineConfig(
        work_dir=cfg.work_dir,
        session_dir=f"{cfg.persistence.mount_path}/opencode/sessions",
        provider=cfg.model.type,
        model=cfg.model.name,
        params=cfg.model.params,
        startup_timeout_seconds=cfg.startup_timeout_seconds,
        max_invocation_seconds=cfg.limits.max_invocation_seconds,
        shared_ttl_seconds=0,
    )
```

d) Delete the `response_action_config_by_channel` block (lines 478-485) and remove the `response_action_config_by_channel=...` argument from the `_make_engine_runner(...)` call (it is removed in Task 3). For now pass nothing for it.

e) Delete the slack/telegram branches in the channel loop (lines 619-629) and the `slack_adapters`/`telegram_adapters` lists + their drain wiring (lines 581-582, 385-397, and the `_drain(...)` call args). Remove `disconnect_slack_adapter`/`disconnect_telegram_adapter` references.

- [ ] **Step 5: Verify import + config tests still pass**

Run: `uv run python -c "import ach_agent.main"` → Expected: no ImportError.
Run: `uv run pytest tests/config tests/router tests/engine -q` → Expected: PASS (these don't touch slack/telegram/engine_runner delivery).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "feat(engine): drop slack/telegram+Hermes; wire EngineConfig from v3 model block"
```

---

### Task 3: Single-object terminal contract; retire the harness delivery layer

**Files:**
- Modify: `src/ach_agent/engine/validator.py` (single-object extract + Pydantic terminal models)
- Modify: `src/ach_agent/engine/lifecycle.py:334-431` (`run_invocation` returns the terminal object)
- Modify: `src/ach_agent/main.py` (`_make_engine_runner`: relay `text`, drop `dispatch_actions`)
- Delete: `src/ach_agent/actions/gitlab_comment.py`, `side_effect.py`, `log.py`, `delivery.py`, `__init__.py` (the whole `actions/` package), `tests/actions/test_gitlab_comment.py`
- Modify: `tests/engine/test_validator.py`, `tests/engine/test_lifecycle.py`

**Interfaces:**
- Produces: `TerminalAction` (discriminated union `NoneAction | A2AReply`), `extract_terminal(text: str) -> dict | None`, `run_invocation(...) -> dict` (the validated terminal object). `engine_runner` consumes `result["text"]`.

- [ ] **Step 1: Write the failing test for the new terminal models + extractor**

Replace `tests/engine/test_validator.py` content with:

```python
from ach_agent.engine.validator import NoneAction, A2AReply, extract_terminal

def test_extract_none_action():
    text = 'thinking...\n{"action":"none","text":"done","thoughts":"ok"}'
    obj = extract_terminal(text)
    assert obj == {"action": "none", "text": "done", "thoughts": "ok"}

def test_extract_a2a_reply():
    text = '{"action":"a2a_reply","text":"hello peer"}'
    obj = extract_terminal(text)
    assert obj["action"] == "a2a_reply"
    assert obj["text"] == "hello peer"

def test_extract_returns_none_when_absent():
    assert extract_terminal("no json here") is None

def test_none_action_model_defaults():
    m = NoneAction(action="none")
    assert m.text == "" and m.thoughts == ""

def test_a2a_reply_requires_text():
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        A2AReply(action="a2a_reply")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/engine/test_validator.py -q`
Expected: FAIL — `NoneAction`/`A2AReply`/`extract_terminal` not defined.

- [ ] **Step 3: Rewrite `validator.py`**

Replace the body of `src/ach_agent/engine/validator.py` (keep `_find_matching_brace`, drop `extract_actions`/`validate_actions`/`repair_turn`/`InvocationResult` and the `jsonschema` import):

```python
from __future__ import annotations

import json
import re
from typing import Literal

import structlog
from pydantic import BaseModel, ConfigDict

log = structlog.get_logger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


class NoneAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["none"]
    text: str = ""
    thoughts: str = ""


class A2AReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["a2a_reply"]
    text: str
    thoughts: str = ""


# (_find_matching_brace stays as-is — copy it from the current file)


def extract_terminal(accumulated_text: str) -> dict | None:
    """Find the last {"action": ...} object in the model's text output."""
    text = accumulated_text
    fence = _FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()
    pos = text.rfind('{"action"')
    if pos == -1:
        return None
    end = _find_matching_brace(text, pos)
    if end == -1:
        return None
    try:
        return json.loads(text[pos : end + 1])
    except json.JSONDecodeError:
        return None
```

- [ ] **Step 4: Run validator tests to verify they pass**

Run: `uv run pytest tests/engine/test_validator.py -q`
Expected: PASS.

- [ ] **Step 5: Update `run_invocation` to return the terminal object (with one backstop retry)**

In `src/ach_agent/engine/lifecycle.py`, change `run_invocation`'s signature and tail. Replace the `response_actions_schema` parameter with `terminal_retries: int = 1`, and replace the extraction/repair block (lines ~393-431) with:

```python
    from ach_agent.engine.validator import extract_terminal

    obj = extract_terminal(accumulated_text)
    if obj is None and terminal_retries > 0:
        repair = (
            'Reply with ONLY a terminal JSON object: '
            '{"action":"none","text":"..."} or {"action":"a2a_reply","text":"..."}.'
        )
        accumulated_text = await consume_sse_after_send(client, session_id, repair)
        obj = extract_terminal(accumulated_text)
    return obj if obj is not None else {"action": "none", "text": accumulated_text}
```

Change the return type annotation to `dict` and drop the `InvocationResult` import. Update `tests/engine/test_lifecycle.py` to call `run_invocation(..., terminal_retries=1)` and assert it returns a dict with an `action` key (read the current test and adjust the mock's accumulated text to include a terminal object).

- [ ] **Step 6: Retire the delivery layer + rewire `engine_runner`**

```bash
git rm -r src/ach_agent/actions tests/actions/test_gitlab_comment.py
```

In `src/ach_agent/main.py`:
- Remove `from ach_agent.actions.gitlab_comment import GitlabCommentAdapter, dispatch_actions` (line 38) and all `gitlab_adapter` construction/usage (lines 459-460, the `delivery_adapter=` arg, the `_drain` `gitlab_adapter` arg + `await gitlab_adapter.close()`).
- Rewrite `_make_engine_runner` to drop `delivery_adapter` and `dispatch_actions`. The runner now: run_invocation → if `reply_future`: set_result(`obj["text"]`) → elif `on_complete`: call with `obj["text"]` → else: nothing (async; egress already happened via the agent's MCP tool calls). Replace the body's result handling:

```python
            obj = await run_invocation(
                server=server,
                session_id=event.session_key,
                prompt=full_prompt,
                terminal_retries=1,
                max_invocation_seconds=max_invocation_seconds,
                on_kill=on_kill,
            )
            text = str(obj.get("text", ""))
            if future is not None:
                if not future.done():
                    future.set_result(text)
                return
            on_complete = event.delivery_context.get("on_complete")
            if on_complete is not None:
                on_complete(event.session_key, text)
            # else: async channel — nothing to deliver (agent acted via MCP tools)
```

(Keep the `try/except` that sets `future.set_exception` on error, and the `finally: pool.release`.)

- [ ] **Step 7: Run engine + memory + router tests**

Run: `uv run pytest tests/engine tests/memory tests/router -q`
Expected: PASS. (If `test_lifecycle.py`/`test_roundtrip.py` reference the old `actions` shape, adjust their accumulated-text mocks to the terminal object.)

- [ ] **Step 8: Commit**

```bash
git add -A
git commit -m "feat(engine): single-object terminal contract; retire harness delivery (egress=MCP)"
```

---

### Task 4: Fix the v2-config test helpers (http + main wiring) and conformance

**Files:**
- Modify: `tests/test_main_wiring.py` (`_make_webhook_cfg`)
- Modify: `tests/http/test_app.py` (`_make_channel_cfg`)
- Modify/Delete: `tests/conformance/test_inv09_dual_delivery.py`
- Modify: `tests/conformance/test_inv01_idempotency.py`, `tests/router/test_dedup.py`, `tests/router/conftest.py`, `tests/e2e/test_skeleton.py` (trim slack/telegram cases)

**Interfaces:**
- Consumes: the v3 `ChannelConfig` (webhook needs `source`, no `deliver` block) and the new `ModelBlock`.

- [ ] **Step 1: Fix the webhook config helpers**

In `tests/test_main_wiring.py` `_make_webhook_cfg` and `tests/http/test_app.py` `_make_channel_cfg`, remove the `"deliver": {"type": ...}` key and add `"source": "gitlab"`. The webhook block becomes only `{"auth": {"type": "gitlab_token", "secretPath": str(secret_file)}}`. Drop the `deliver_type` parameter and any reply-mode branch that depended on it (v3 webhooks are async-202; reply mode is a later plan).

- [ ] **Step 2: Run those suites to verify they collect + the non-reply cases pass**

Run: `uv run pytest tests/test_main_wiring.py tests/http/test_app.py -q`
Expected: PASS for the async/202 + readiness/draining/413 cases. Delete or `pytest.mark.skip` (with a Plan-3 TODO) any test that asserted the **reply-mode 200+body** behavior — reply mode is deferred.

- [ ] **Step 3: Retire the dual-delivery invariant test**

`tests/conformance/test_inv09_dual_delivery.py` tested the v2 reply-vs-gitlab_comment split, which is retired (egress=MCP). Delete it:

```bash
git rm tests/conformance/test_inv09_dual_delivery.py
```

(Plan 4 re-adds the §6.9 egress invariant + §6.10 secret-hygiene test.)

- [ ] **Step 4: Trim slack/telegram references**

In `tests/conformance/test_inv01_idempotency.py`, `tests/router/test_dedup.py`, `tests/router/conftest.py`, `tests/e2e/test_skeleton.py`: remove the slack/telegram parametrize cases / imports. Find them with:

Run: `uv run grep -rn "slack\|telegram" tests/`
Then delete those specific cases. Keep webhook/cron/queue/a2a cases.

- [ ] **Step 5: Run the full non-e2e suite**

Run: `uv run pytest -q --ignore=tests/e2e`
Expected: PASS (or only e2e remaining red — addressed in Plan 4).

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "test: align webhook/conformance fixtures to v3; drop slack/telegram + dual-delivery"
```

---

### Task 5: Remove the Phase-2 mypy override + `ResponseActionBlock` alias; re-green `make lint`

**Files:**
- Modify: `pyproject.toml:87-93` (delete the scoped override)
- Modify: `src/ach_agent/main.py:49-52` (delete the `ResponseActionBlock = Any` alias + its annotations)

**Interfaces:** none produced; this is the green-gate.

- [ ] **Step 1: Delete the scoped mypy override**

In `pyproject.toml`, delete the second `[[tool.mypy.overrides]]` block (the `ach_agent.main` + `ach_agent.actions.*` one, lines 87-93) and its comment.

- [ ] **Step 2: Delete the `ResponseActionBlock` alias**

In `src/ach_agent/main.py`, delete lines 49-52 (the alias + comment) and remove the `response_action_config_by_channel: dict[str, dict[str, ResponseActionBlock]]` annotation from `_make_engine_runner`'s signature (it was removed in Task 3 — confirm no `ResponseActionBlock` references remain: `uv run grep -rn ResponseActionBlock src/`).

- [ ] **Step 3: Run lint to verify it's green**

Run: `uv run mypy --strict src`
Expected: PASS (Success: no issues). If `attr-defined` errors remain, they point at a missed `cfg.engine.*` / `response_actions` read — fix the read (it should already be gone from Task 2/3).

Run: `uv run ruff check src && uv run ruff format --check src`
Expected: PASS.

- [ ] **Step 4: Run the full non-e2e gate**

Run: `make lint && uv run pytest -q --ignore=tests/e2e`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add -A
git commit -m "chore: remove Phase-2 mypy override + ResponseActionBlock alias (tree green on opencode+v3)"
```

---

## Self-Review

**Spec coverage** (against `docs/plan/rescope-opencode/`): de-cruft (slack/telegram/Hermes ✓ Task 2; v2 delivery ✓ Task 3; obsolete tests ✓ Tasks 2-4), model block reshape ✓ Task 1, engine reconnect ✓ Task 2, single-object terminal + `text` ✓ Task 3, mypy-override/alias removal ✓ Task 5. **Not in Plan 1 (later plans):** localhost proxy + `ek` hygiene (Plan 2), `/platform/hydrate` + context tar→dir (Plan 2), webhook `source`-select runtime parsing + queue/tui (Plan 3), a2a egress MCP (Plan 3), §6.9/§6.10 conformance + integration guard (Plan 4). Reply-mode webhook is deferred (noted in Task 4).

**Placeholder scan:** the opencode model-string format under ACH/litellm (`f"{type}/{name}"`) and `params` passthrough into `opencode.json` options are concrete here but **verify against a live ACH/litellm model id in Plan 2** when the proxy lands (the model must also be in the hydrated `runtime.models`). This is a verification step, not a TODO.

**Type consistency:** `ModelBlock{name,type,params}` (Task 1) → `EngineConfig{model=name, provider=type, params}` (Task 2) → `write_opencode_config` options (Task 2) → `run_invocation -> dict` terminal (Task 3) → `engine_runner` reads `obj["text"]` (Task 3). `extract_terminal` replaces `extract_actions` everywhere (validator, lifecycle, tests).

---

## Plans 2-4 (to be written next, one file each)

- **Plan 2 — Localhost proxy + hydration + context:** harness reverse-proxy for model (`/v1`,`/gemini`,`/anthropic`) + MCP injecting `ek`; opencode points at localhost (no `ek` in `opencode.json`); `POST /platform/hydrate`; resolve `model.name` vs hydrated models; context `tar.gz` → dir.
- **Plan 3 — Channel redraw:** webhook `source`-selected parser/auth; implement queue (redis) + tui; a2a egress MCP tools (port ackbot `handlers/a2a/{tools,client,notification_store}.py`); add a2a-ingress FAILED-on-invalid-terminal.
- **Plan 4 — Conformance re-green + integration guard:** §6 invariants incl. §6.10 secret-hygiene (ek never in opencode.json/env/logs); §6.9 egress; opencode + MCP-via-proxy + structured-output integration test; e2e green.
