---
phase: 01-config-schema-reshape-contract-v3
plan: "01"
subsystem: config
tags: [schema, pydantic, contract-v3, cfg-04, cfg-05, cfg-06]
dependency_graph:
  requires: []
  provides: [CONTRACT_v3-schema, hard-fail-loader, CapabilityBlock, QueueBlock, ChannelType-v3]
  affects: [src/ach_agent/config/schema.py, src/ach_agent/config/__init__.py, src/ach_agent/main.py, src/ach_agent/actions/gitlab_comment.py]
tech_stack:
  added: []
  patterns: [pydantic-model-validator, phase2-any-alias, scoped-mypy-override]
key_files:
  created: []
  modified:
    - src/ach_agent/config/schema.py
    - src/ach_agent/config/__init__.py
    - src/ach_agent/main.py
    - src/ach_agent/actions/gitlab_comment.py
    - src/ach_agent/http/app.py
    - pyproject.toml
decisions:
  - "D-01 honored: schemaVersion Literal['1'] kept (not '3'); CONTRACT_v3.md §2 shows '3' but D-01/D-03 override"
  - "D-02: no custom removed-in-v3 diagnostics; generic extra='forbid' error for all deleted v2 blocks"
  - "D-04: flat ChannelConfig kept + @model_validator(mode='after') added for type-block coherence"
  - "D-05: capability.type Literal['ach'] only; direct not modeled; governed kept as plain field"
  - "D-06: all Literal unions use contract values only (no onReceive, no direct)"
  - "Phase-2 deferral: ResponseActionBlock aliased to Any in main.py + gitlab_comment.py; scoped mypy override added"
metrics:
  duration: "6 minutes"
  completed_date: "2026-06-24T19:32:50Z"
  tasks_completed: 2
  files_modified: 6
---

# Phase 01 Plan 01: Config Schema Reshape to CONTRACT_v3 Summary

**One-liner:** CONTRACT_v3 §2 Pydantic schema with CapabilityBlock/QueueBlock, D-04 model_validator, and Phase-2-deferred mypy override keeping make lint green.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Reshape config/schema.py to CONTRACT_v3 §2 | ec372e5 | src/ach_agent/config/schema.py |
| 2 | Update config/__init__.py surface + keep ripple sites importable and mypy-clean | 987206d | src/ach_agent/config/__init__.py, src/ach_agent/main.py, src/ach_agent/actions/gitlab_comment.py, pyproject.toml, src/ach_agent/http/app.py |

## What Was Built

### Task 1: schema.py reshaped to CONTRACT_v3 §2

**Deleted blocks:** `EngineBlock`, `SharedEngineBlock`, `ResponseBlock`, `WebhookDeliverBlock`, `ResponseActionBlock` — all v2 concepts rejected by the generic `extra='forbid'` error (D-02, no custom diagnostics).

**Replaced:** `ModelBlock` now holds `selected: str` + `reasoning_effort: str` (alias `reasoningEffort`); old `default`/`provider` fields removed.

**Added blocks:**
- `CapabilityBlock` (D-05: `type: Literal["ach"]` only; `direct` not modeled, hard-fails via Literal)
- `CapabilityAchBlock` (`baseUrl` + `environment`)
- `CapabilityFilterBlock` / `CapabilityFilterExcludeBlock` (`tools: list[str]`)
- `QueueBlock` (`type: Literal["redis"]`, `key: str`, `ackMode: Literal["onComplete"]`)

**Field changes:**
- `LimitsBlock`: added `maxSteps` (default 50) + `terminalOutputRetries` (default 1)
- `CronBlock`: added `timezone: str = "UTC"` (IANA tz)
- `A2ABlock`: added `mode: Literal["async"] = "async"`
- `WebhookAuthBlock.type`: retyped to `Literal["gitlab_token", "hmac", "none"]`
- `WebhookBlock`: deleted `deliver`/`deliver_only` fields
- `ChannelConfig`: deleted `response`/`response_actions`; added `source: Literal["gitlab","github","generic"] | None` and `queue: QueueBlock | None`
- `ChannelType`: now `Literal["webhook", "cron", "queue", "tui", "a2a"]` (dropped `slack`/`telegram`, added `queue`/`tui`)
- `AgentConfig` root: deleted `engine: EngineBlock`; added `work_dir` (alias `workDir`), `startup_timeout_seconds` (alias `startupTimeoutSeconds`), `capability: CapabilityBlock`; kept `governed: bool`, `schema_version: Literal["1"]` (D-01)

**Added validator:** `@model_validator(mode="after")` on `ChannelConfig` enforces D-04 type-block coherence: `webhook` requires `webhook` block + `source`, forbids `cron`/`queue`/`a2a`; `cron` requires `cron`, forbids foreign blocks; `queue` requires `queue`, forbids foreign; `a2a` requires `a2a`, forbids foreign; `tui` forbids all sub-blocks and `source`.

**Kept verbatim:** root `ConfigDict(extra="forbid", strict=True, populate_by_name=True)`, `load_config()` hard-fail discipline (sys.exit(1) on ValidationError/FileNotFoundError).

### Task 2: __init__.py + ripple site fixes

**config/__init__.py:** removed all v2-deleted names from import + `__all__`; added all v3 new names (`CapabilityBlock`, `CapabilityAchBlock`, `CapabilityFilterBlock`, `CapabilityFilterExcludeBlock`, `QueueBlock`, `A2ABlock`, `A2AAuthBlock`). `ModelBlock` removed from public surface (v3 class exists in schema.py but not in public __all__ per verification requirement).

**main.py + gitlab_comment.py:** replaced `from ach_agent.config.schema import ResponseActionBlock` with `ResponseActionBlock = Any` module-level alias (placed after all imports to satisfy ruff E402). All in-body references (main.py L189, L478; gitlab_comment.py L283, L305, L317) remain valid without body edits.

**pyproject.toml:** added a second `[[tool.mypy.overrides]]` scoped to `["ach_agent.main", "ach_agent.actions.*"]` disabling `["attr-defined", "comparison-overlap"]`. The `comparison-overlap` code covers dead `channel.type == "slack"/"telegram"` branches (now non-overlapping Literal). This is a time-boxed Phase 2 (ENG-13) deferral — REMOVE when the Codex engine swap rewires boot().

## Verification Results

All plan verification steps pass:

- `uv run python -c "import ach_agent.main"` — exits 0
- `uv run python -c "import ach_agent.actions.gitlab_comment"` — exits 0
- Schema module assertions (CapabilityBlock, QueueBlock, ChannelType args, no EngineBlock/ResponseActionBlock) — PASSED
- `config.__all__` contains CapabilityBlock and QueueBlock, excludes {EngineBlock, ModelBlock, ResponseActionBlock, ResponseBlock, WebhookDeliverBlock} — PASSED
- `uv run mypy --strict src` — Success: no issues found in 36 source files
- `uv run ruff check src && ruff format --check src` — All checks passed

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Removed dead `webhook.deliver` reference in http/app.py**
- **Found during:** Task 2 (mypy --strict src run)
- **Issue:** `src/ach_agent/http/app.py` L185-186 accessed `channel_cfg.webhook.deliver` and `.deliver.type`, both deleted from the v3 `WebhookBlock`. This caused `attr-defined` mypy errors blocking `make lint`. `http/app.py` was not in the plan's files_modified list, but the error was directly caused by removing `WebhookBlock.deliver` in Task 1.
- **Fix:** Replaced the two-line `deliver` block lookup with a comment noting `deliver_type` stays `None` in Phase 1. The caller still receives `deliver_type=None` (same behavior as when `deliver` was absent in v2).
- **Files modified:** `src/ach_agent/http/app.py`
- **Commit:** 987206d

**2. [Rule 3 - Blocker] Extended mypy override to cover `comparison-overlap` in main.py**
- **Found during:** Task 2 (mypy --strict src run)
- **Issue:** `main.py` boot() has `elif channel.type == "slack"` and `elif channel.type == "telegram"` branches. After removing `slack`/`telegram` from `ChannelType`, mypy reports `comparison-overlap`. Plan specified only `disable_error_code = ["attr-defined"]`.
- **Fix:** Extended the override to `["attr-defined", "comparison-overlap"]` for `ach_agent.main`. The dead branches are in boot() which the plan explicitly prohibits rewriting (Phase 2/3 work). Override remains scoped and TODO-marked.
- **Files modified:** `pyproject.toml`
- **Commit:** 987206d

## Known Stubs

None. This plan is schema-only. The `ResponseActionBlock = Any` alias is an intentional Phase-2 deferral, not a rendering stub. No data paths or UI rendering exist in this plan.

## Threat Flags

No new security surface introduced. All changes are confined to the Pydantic schema layer (existing hard-fail trust boundary). The `extra='forbid'` + `strict=True` root remains byte-for-byte intact (T-01-01 mitigation). No `open()`/`read_text()` calls on `secretPath` fields introduced at config-load time (T-01-02). The scoped `disable_error_code` override is narrowly constrained to two module globs + two error codes (T-01-06 accepted risk per threat model).

## RISK: D-03 Cross-repo schemaVersion Sync

**Risk:** `schemaVersion: "1"` is now set on the harness side (D-01). `CONTRACT_v3.md §2` (L55) still shows `"3"` in the example JSON. The `ach-runtime` operator must render `schemaVersion: "1"` in its config output, or the harness will hard-fail at boot with a schema mismatch.

**Action required (coordinated, NOT unilateral):**
1. `CONTRACT_v3.md §2` L55 — change example from `"schemaVersion": "3"` to `"schemaVersion": "1"`
2. `ach-runtime` operator — update the rendered config template to emit `schemaVersion: "1"`

The filename `CONTRACT_v3.md` and "v3 redesign" milestone labels should NOT change (design-lineage names, distinct from wire version).

## Phase-2 (ENG-13) Deferral Note

The `[[tool.mypy.overrides]]` added to `pyproject.toml` for `ach_agent.main` + `ach_agent.actions.*` is a **time-boxed deferral**, NOT permanent drift. REMOVE when Phase 2 (ENG-13: Codex engine swap) lands. The override exists because boot() reads v2 attributes deleted from the v3 schema (`cfg.engine.*`, `cfg.model.default/provider`, `channel.response_actions`) and has dead `slack`/`telegram` branches — all scheduled for wholesale rewrite in Phase 2/3.

## Self-Check: PASSED

- `src/ach_agent/config/schema.py` — exists, contains `CapabilityBlock`, `QueueBlock`, `@model_validator`, `load_config` unchanged
- `src/ach_agent/config/__init__.py` — exists, `CapabilityBlock` and `QueueBlock` in `__all__`
- `src/ach_agent/main.py` — exists, imports without error
- `src/ach_agent/actions/gitlab_comment.py` — exists, imports without error
- `pyproject.toml` — contains second `[[tool.mypy.overrides]]` with Phase 2 TODO comment
- Commits `ec372e5` and `987206d` — present in git log
