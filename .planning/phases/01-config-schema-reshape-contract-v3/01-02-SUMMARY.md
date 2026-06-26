---
phase: 01-config-schema-reshape-contract-v3
plan: "02"
subsystem: config
tags: [schema, pydantic, contract-v3, cfg-04, cfg-05, cfg-06, tests, fixtures]
dependency_graph:
  requires: [01-01]
  provides: [CFG-04-regression-suite, CFG-05-field-assertions, CFG-06-positive-fixtures, v3-fixtures-all-channel-types]
  affects:
    - tests/config/test_schema.py
    - tests/config/fixtures/config_webhook.json
    - tests/config/fixtures/config_cron.json
    - tests/config/fixtures/config_queue.json
    - tests/config/fixtures/config_tui.json
    - tests/config/fixtures/config_a2a.json
tech_stack:
  added: []
  patterns: [pytest-raises-SystemExit, valid-base-dict-mutation-pattern, realistic-v2-rejection-nesting]
key_files:
  created:
    - tests/config/fixtures/config_webhook.json
    - tests/config/fixtures/config_queue.json
    - tests/config/fixtures/config_tui.json
    - tests/config/fixtures/config_a2a.json
  modified:
    - tests/config/fixtures/config_cron.json
    - tests/config/test_schema.py
decisions:
  - "Shared _VALID_WEBHOOK_BASE and _VALID_CRON_BASE dicts defined at module level for minimal negative test mutation (isolates each single rejection cause)"
  - "inputSchema/consentTier negative tests use realistic v2 nesting (responseActions list on channel); extra='forbid' rejects responseActions key before reaching nested key — test proves legacy shape rejected"
  - "grep -L pattern uses quoted keys to avoid 'engineering' namespace value matching 'engine' substring"
metrics:
  duration: "4 minutes"
  completed_date: "2026-06-24T19:52:23Z"
  tasks_completed: 2
  files_modified: 6
---

# Phase 01 Plan 02: Config Schema v3 Test Suite Summary

**One-liner:** Five valid CONTRACT_v3 §2 fixtures (one per channel type) plus a 24-test regression suite covering CFG-04 removed-block rejection, CFG-05 field assertions, CFG-06 positive loads, and D-01/D-04/D-05/D-06 negative paths.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Write the five valid v3 fixtures (one per channel type) | f14cad8 | tests/config/fixtures/config_webhook.json, config_cron.json, config_queue.json, config_tui.json, config_a2a.json |
| 2 | Rewrite tests/config/test_schema.py to the v3 regression suite | 4261cd3 | tests/config/test_schema.py |

## What Was Built

### Task 1: Five valid CONTRACT_v3 §2 fixtures

Each fixture carries the full v3 top-level shape: `schemaVersion:"1"`, `agent`, `model{selected,reasoningEffort}`, `workDir`, `startupTimeoutSeconds`, `governed`, `capability{type:"ach",ach{baseUrl,environment},filter{exclude{tools:[]}}}`, `limits{maxConcurrentInvocations,maxInvocationSeconds,maxQueuedTotal,idempotencyWindowSeconds,maxSteps,terminalOutputRetries}`, `persistence`, `health`, and a single-element `channels[]`.

**Per-fixture channel blocks:**
- `config_webhook.json`: `type:"webhook"`, `source:"gitlab"`, `webhook.auth.type:"gitlab_token"` with path-only `secretPath`
- `config_cron.json`: rewritten from v2 (dropped `engine`, `response`, `responseActions`); `type:"cron"`, `cron.schedule:"* * * * *"`, `cron.timezone:"Europe/Madrid"`, `session.continuity:"durable"` preserved
- `config_queue.json`: `type:"queue"`, `queue.type:"redis"`, `queue.key:"ach:triage"`, `queue.ackMode:"onComplete"`
- `config_tui.json`: `type:"tui"`, no sub-block (D-04 clean -- tui forbids all)
- `config_a2a.json`: `type:"a2a"`, `a2a.mode:"async"`, `a2a.auth.header:"x-a2a-custom-api-key"`, `a2a.auth.secretPath` is a path string (no inline secret)

All five load via `load_config()` without error. No fixture contains `engine`, `responseActions`, `response`, `deliver`, `inputSchema`, or `consentTier` as JSON keys.

### Task 2: v3 regression suite (24 tests)

**Positive (CFG-06):** Five fixture load tests, each asserting channel type and a representative v3 sub-field (cron timezone, webhook source+auth.type, queue ack_mode, tui no-sub-block, a2a mode+secretPath).

**Positive (CFG-05):** `test_v3_fields_parse` asserts all new v3 fields parse: `model.reasoning_effort`, `work_dir`, `startup_timeout_seconds`, `limits.max_steps`, `limits.terminal_output_retries`, `capability.filter.exclude.tools`, `governed`.

**Negative (CFG-04 removed-block):** Six tests proving removed v2 blocks hard-fail:
- `test_engine_block_hard_fails` -- top-level `engine` block
- `test_response_actions_hard_fails` -- channel `responseActions`
- `test_response_block_hard_fails` -- channel `response`
- `test_webhook_deliver_hard_fails` -- `webhook.deliver`
- `test_input_schema_in_response_actions_hard_fails` -- realistic v2 nesting: `responseActions[].inputSchema`
- `test_consent_tier_in_response_actions_hard_fails` -- realistic v2 nesting: `responseActions[].consentTier`

**Negative (D-06 bad enum):** Four tests -- `webhook.source:"slack"`, `webhook.auth.type:"bearer"`, `queue.ackMode:"onReceive"`, `a2a.mode:"sync"`.

**Negative (D-04 coherence):** Three tests -- cron+webhook foreign block, tui+cron foreign block, webhook missing `source`.

**Negative (D-05):** `test_capability_type_direct_hard_fails` -- `capability.type:"direct"` rejected by `Literal["ach"]`.

**Negative (D-01):** `test_schema_version_wrong_hard_fails` -- `schemaVersion:"3"` rejected by `Literal["1"]`.

**Updated (CFG-02/03):** `test_unknown_key_hard_fail` and `test_unknown_channel_type_rejected` updated to v3 base shape (dropped `engine`/`model.default`/`model.provider`).

**Removed:** Three obsolete v2 test functions (`test_consent_tier_explicit_auto`, `test_consent_tier_default_when_absent`, `test_consent_tier_invalid_value_rejected`) -- responseActions/consentTier are gone in v3.

## Verification Results

- `uv run pytest tests/config/ -q` -- 24 passed in 0.67s
- All five fixtures load via `load_config()` (verified programmatically before commit)
- No fixture carries removed v2 keys (grep with quoted key patterns returns no matches)
- All fixtures use `"schemaVersion": "1"` (verified)
- Obsolete v2 test function names absent from test_schema.py (grep returns nothing)

## Deviations from Plan

None -- plan executed exactly as written. The acceptance criterion uses `grep -L -E 'engine|...'` which has a substring match issue with "engineering" namespace values; the intent is verified using quoted key patterns `grep '"engine"|...'` which correctly returns all five files as not-matching.

## Known Stubs

None. This plan is test-only. All assertions target real parsed values, no hardcoded stubs or placeholder data.

## Threat Flags

No new security surface introduced. All changes are test-only. Fixture `secretPath` fields carry path strings only (`/etc/ach-agent/secrets/...`) -- no credential values committed (T-01-05 clean).

## Self-Check: PASSED

- `tests/config/fixtures/config_webhook.json` -- exists, `"schemaVersion": "1"`, source:gitlab, webhook.auth.type:gitlab_token
- `tests/config/fixtures/config_cron.json` -- exists, `"schemaVersion": "1"`, cron.schedule, cron.timezone
- `tests/config/fixtures/config_queue.json` -- exists, `"schemaVersion": "1"`, queue.ackMode:onComplete
- `tests/config/fixtures/config_tui.json` -- exists, `"schemaVersion": "1"`, no sub-block
- `tests/config/fixtures/config_a2a.json` -- exists, `"schemaVersion": "1"`, a2a.mode:async, secretPath
- `tests/config/test_schema.py` -- exists, 24 test functions, obsolete v2 test functions absent
- Commit `f14cad8` (Task 1) -- present in git log
- Commit `4261cd3` (Task 2) -- present in git log
- `uv run pytest tests/config/ -q` -- 24 passed (0 failures)
