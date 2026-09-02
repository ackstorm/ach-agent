# Webhook Script Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an authenticated asynchronous webhook channel that runs a deterministic shell script without launching an agent engine.

**Architecture:** Reuse the existing webhook adapter and router so authentication, filtering, deduplication, backpressure, lanes, and concurrency remain unchanged. Dispatch admitted `webhook-script` events to the existing bounded shell harness with JSON on stdin and an ephemeral workspace, returning before any memory or engine work.

**Tech Stack:** Python 3.12, Pydantic v2, asyncio, pytest; Go, Kubebuilder/controller-gen, controller-runtime.

**Spec:** `docs/spec/2026-09-02-webhook-script-channel.md`

## Global Constraints

- `webhook-script` never probes memory, acquires an engine, or invokes a model.
- Webhook JSON is normalized and supplied on stdin; payload data is never interpolated into script source.
- Secret values remain env-only, are redacted, and are stripped from engine forwarding.
- GitLab script events serialize per project and channel.
- No new dependency.

---

### Task 1: Harness contract, parsing, and execution

**Files:**
- Modify: `src/ach_agent/config/schema.py`
- Modify: `src/ach_agent/channels/webhook.py`
- Modify: `src/ach_agent/boot/prepare.py`
- Modify: `src/ach_agent/boot/engine_runner.py`
- Modify: `src/ach_agent/boot/secrets.py`
- Modify: `src/ach_agent/engine/metrics.py`
- Modify: `src/ach_agent/main.py`
- Test: `tests/config/test_schema.py`
- Test: `tests/channels/test_webhook.py`
- Test: `tests/test_prepare.py`
- Test: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: existing `WebhookBlock`, `PrepareBlock`, `MessageEvent`, router lane, and bounded script executor.
- Produces: `ChannelType="webhook-script"` and `run_webhook_script(cfg, event, work_dir)`.

- [ ] Write failing schema tests proving required/forbidden blocks and new GitLab events.
- [ ] Run those tests and verify rejection is caused by the missing channel type/events.
- [ ] Write failing adapter tests proving system-hook parsing and project-scoped lanes.
- [ ] Run those tests and verify events are currently ignored or rejected.
- [ ] Write a failing real-subprocess test proving JSON stdin, event env, bounded execution, and ephemeral workspace removal.
- [ ] Run it and verify `run_webhook_script` is missing.
- [ ] Implement the minimal schema, parser, runner dispatch, secret collection, logs, and failure metric.
- [ ] Run the focused tests until green.

### Task 2: Frozen contract and documentation

**Files:**
- Modify: `docs/schemas/operator-contract.md`
- Modify: `docs/configuration.md`
- Modify: `docs/schemas/agent-config-v1.schema.json` (generated)
- Modify: `tests/config/test_schema_artifact.py`

**Interfaces:**
- Consumes: Pydantic `AgentConfig` JSON Schema.
- Produces: published machine-readable and prose contract for `webhook-script`.

- [ ] Add a failing artifact assertion for the new channel type and script block.
- [ ] Regenerate the schema with `uv run python scripts/gen_schema.py`.
- [ ] Update contract/configuration prose with the exact asynchronous execution semantics.
- [ ] Run schema and documentation-focused tests.

### Task 3: Operator CRD and rendering

**Files:**
- Modify: `../ach/api/ach/v1alpha1/achagent_types.go`
- Modify: `../ach/internal/agentrender/config.go`
- Modify: `../ach/internal/agentrender/render.go`
- Modify: `../ach/internal/agentrender/render_test.go`
- Modify: `../ach/internal/agentrender/schema_test.go`
- Modify: generated deepcopy, CRD, Helm mirror, and API reference files.
- Modify: `../ach/examples/agent-runtime/agent.yaml`

**Interfaces:**
- Consumes: `PrepareSpec`, merged AgentProfile/ACHAgent env, and ach-agent's generated schema.
- Produces: rendered `channels[].script` plus Pod secret env aliases for `webhook-script`.

- [ ] Write a failing renderer test with literal, secret, and missing `forwardEnv` names.
- [ ] Run the focused Go test and verify `ChannelSpec.Script` is missing.
- [ ] Implement CRD field validation and renderer support, including auth and script secret references.
- [ ] Copy the regenerated harness schema into the operator fixture.
- [ ] Regenerate deepcopy, CRDs, Helm CRD mirror, and API reference.
- [ ] Add/update the curated example.
- [ ] Run focused Go tests, unit tests, lint/format, and generated-drift checks.
- [ ] Commit and push each repository after verification.

