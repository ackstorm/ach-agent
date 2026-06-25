# Plan 4 — Conformance Re-green + Integration Guard

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.
>
> **Design-forward plan.** Prereqs: Plans 1–3 merged.
>
> **Execution (see `README.md`).** Owns: `tests/conformance/*` (new INV-12/13 + INV-01 extension) and `tests/e2e/*`. Test-only — no `src/` conflict with siblings; forks from the **merge of Plan 3**. Parallel-safe *within* this plan: Tasks 1-3 (independent conformance test files) can run concurrently; Tasks 4 (integration guard) + 5 (e2e re-green / `make verify`) after.

**Goal:** Bring the full gate green on the v3 harness — the CONTRACT §6 behavioral invariants (router IP untouched) plus the two new ones (§6.9 egress, §6.10 secret-hygiene), idempotency for queue/a2a, an opencode+MCP+structured-output integration guard, and green e2e. `make verify` passes.

**Architecture:** The router invariants (idempotency, pre-lane order, bounds, expire, fail-open, startup deadline, A′) are unchanged and already covered — this plan adds the v3-specific invariants and the end-to-end guard, and re-greens the e2e suite against opencode + the localhost proxy.

**Tech Stack:** pytest(+asyncio), the existing conformance harness (`tests/conformance/`), a mock ACH/opencode for the integration test.

## Global Constraints

- `uv run`; router untouched; `make verify` is the final gate.
- §6.10 (secret hygiene) is the headline new invariant: the `ek_` never appears in `opencode.json`, opencode's env, or logs.
- Idempotency keys: queue = redis message id; a2a = task id (CONTRACT §6.1).
- The integration guard must run on pinned versions and must not require network egress (mock ACH).

---

### Task 1: §6.10 — secret hygiene conformance

**Files:** Create `tests/conformance/test_inv12_secret_hygiene.py`.

**Interfaces:** Consumes `write_opencode_config` (Plan 2) + `configure_logging`/`SanitizedEnv` (existing).

- [ ] **Step 1: Write the test**

```python
import json
from pathlib import Path

def test_ek_never_in_opencode_json(tmp_path, monkeypatch):
    monkeypatch.setenv("ACH_TOKEN", "ek-conformance-secret")
    from ach_agent.engine.lifecycle import write_opencode_config, EngineConfig
    cfg = EngineConfig(model="openai.gpt-5", provider="openai",
                       model_base_url="http://127.0.0.1:9001/v1",
                       mcp_local_urls={"m": "http://127.0.0.1:9002/mcp/m"})
    write_opencode_config(tmp_path, cfg)
    blob = (tmp_path / ".config" / "opencode" / "opencode.json").read_text()
    assert "ek-conformance-secret" not in blob
    assert "127.0.0.1" in blob  # points at the local proxy

def test_ek_redacted_in_logs(capsys):
    import structlog
    from ach_agent.engine.sanitized_env import configure_logging
    configure_logging()
    structlog.get_logger("t").info("boot", token="ek-should-be-redacted")
    out = capsys.readouterr().out + capsys.readouterr().err
    assert "ek-should-be-redacted" not in out
```

- [ ] **Step 2: Run → PASS** if Plan 2 landed correctly (this is a regression lock). If `test_ek_redacted_in_logs` fails, confirm the redact processor matches the `ek` pattern.
- [ ] **Step 3: Commit** — `git commit -m "test(conformance): INV-12 secret hygiene (ek never in opencode.json/logs)"`

---

### Task 2: §6.9 — egress is the agent's via MCP, not the channel's

**Files:** Create `tests/conformance/test_inv13_egress_via_mcp.py`.

**Interfaces:** Asserts the harness has no channel-side posting path: the engine_runner (Plan 1) never calls a delivery adapter; `actions/` is gone.

- [ ] **Step 1: Write the test**

```python
def test_no_harness_side_delivery_module():
    import importlib, pytest
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ach_agent.actions.gitlab_comment")

def test_engine_runner_does_not_post(monkeypatch):
    # A webhook (async) event with no reply_future / no on_complete returns with
    # NO outbound HTTP — egress happened via the agent's MCP tool calls (mocked engine).
    ...
```

(Fill the second test by building a `MessageEvent` for a webhook channel, a stub engine_runner pool returning `{"action":"none","text":"done"}`, and asserting no delivery callable is invoked — read the Plan-1 engine_runner to mirror its seams.)

- [ ] **Step 2: Run → FAIL then implement/adjust** (mostly assertion wiring; the behavior shipped in Plan 1).
- [ ] **Step 3: Run → PASS.**
- [ ] **Step 4: Commit** — `git commit -m "test(conformance): INV-13 egress via MCP (no channel-side posting)"`

---

### Task 3: idempotency for queue + a2a

**Files:** Modify `tests/conformance/test_inv01_idempotency.py` (add queue + a2a cases).

- [ ] **Step 1: Add cases** — queue event derives `idempotency_key` from the redis message id; a2a event from the task id; both unique-per-distinct-event, never empty. (Reuse the per-channel parametrize already in the file; add `queue` + `a2a` rows.)
- [ ] **Step 2: Run → FAIL → implement key derivation in the queue/a2a channels (Plan 3) if missing → PASS.**
- [ ] **Step 3: Commit** — `git commit -m "test(conformance): INV-01 idempotency for queue (msg id) + a2a (task id)"`

---

### Task 4: Integration guard — opencode + MCP-via-proxy + structured output

**Files:** Create `tests/e2e/test_opencode_mcp_structured_e2e.py`.

**Interfaces:** Uses a mock ACH (the existing e2e mock pattern — read `tests/e2e/conftest.py`) + a stub MCP server behind the localhost proxy + a stub opencode that emits a terminal object.

- [ ] **Step 1: Read `tests/e2e/conftest.py`** for the existing mock-server fixtures, then write the guard: boot the harness against a mock ACH that (a) serves `/platform/hydrate` with one model + one MCP server, (b) the model proxy returns an SSE stream whose text contains a tool call + a terminal `{"action":"none","text":"reviewed"}`, (c) the MCP server records the tool call carried `Bearer ek`. Assert: the invocation completes, the MCP tool was called through the proxy with the `ek`, and the terminal object parsed to `text == "reviewed"`.

- [ ] **Step 2: Run → FAIL.**
- [ ] **Step 3: Implement** the fixtures + wiring so the guard passes (this exercises Plan 1 terminal + Plan 2 proxy/hydration together — the replacement for the old Codex #15451 guard).
- [ ] **Step 4: Run → PASS.**
- [ ] **Step 5: Commit** — `git commit -m "test(e2e): integration guard — opencode + MCP-via-proxy + structured output"`

---

### Task 5: e2e re-green + `make verify`

**Files:** Modify remaining `tests/e2e/*` (gitlab, durability, a2a, skeleton) for the v3 wiring; ensure `make verify` passes.

- [ ] **Step 1: Run the e2e suite** — `uv run pytest tests/e2e -q` — and fix each failure: gitlab e2e drops the comment-posting assertion (egress is MCP now → assert the agent called the gitlab MCP tool via the proxy instead); durability/a2a/skeleton adjust to v3 config (new model block, no deliver, source-select). Read each failing test and align it.
- [ ] **Step 2: Run the full gate** — `make verify` (lint + test + conformance + secrets). Expected: PASS.
- [ ] **Step 3: Commit** — `git commit -m "test(e2e): re-green against v3 opencode harness; make verify green"`

---

## Self-Review

**Coverage:** §6.10 secret-hygiene ✓ T1, §6.9 egress ✓ T2, idempotency queue/a2a ✓ T3 (§6.1), integration guard ✓ T4 (TEST-03), e2e re-green + `make verify` ✓ T5 (TEST-02). Router invariants (INV-01..10 minus the retired INV-09) stay green untouched. **Verify:** the e2e mock-ACH fixtures must speak `/platform/hydrate` + the model/MCP proxy shapes — read `tests/e2e/conftest.py` and extend, don't reinvent. **Read-first steps** (conftest, each e2e test) are required actions.

**Type consistency:** uses `write_opencode_config`/`EngineConfig` (Plans 1-2), the engine_runner seams (Plan 1), the proxy URLs (Plan 2), and the queue/a2a idempotency keys (Plan 3). No new production types.
