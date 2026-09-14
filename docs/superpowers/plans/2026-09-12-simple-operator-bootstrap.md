> Archived design history. Superseded by the [current split contract](../../references/2026-09-14-three-role-split.md). Do not use as implementation instructions.

# Simple Operator Bootstrap Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. User requires GPT-5.6-Luna implementation and root review; do not spawn reviewer agents.

**Goal:** Three containers start from image role args, harness-only full config and narrow mounts, preserving engine.forwardEnv without operator-generated internal configuration.

**Architecture:** Harness produces bounded atomic bootstrap files in two separate ephemeral mounts. Channels receives config, identity and a harness-owned stable HMAC key; engine receives public config only. Existing execution protocols and native adapters remain unchanged.

**Tech Stack:** Existing Python/asyncio/Pydantic, stdlib filesystem/secrets helpers, Docker, pytest.

**Spec:** [Approved operator boundary](../specs/2026-09-12-simple-operator-contract.md). Latest user explicitly authorized implementing our side; no further approval needed for local edits/tests.

**Completed:** Tasks 1–3 implemented in `98393ad`, `a58c43c`, and `929397a`.
Root reviewed each task; the final source passed the unchanged repository gate,
both-engine Compose acceptance, and actual native Pi/OpenCode terminal checks.
See [final validation](../../reports/simple-operator-final-validation.md).
Image publication and external ACH operator rendering remain separate work.

## Global constraints

- Continue isolated `feat/phase1-split` worktree; baseline adf27fe.
- Root reviews and may write documents/diagnostics. Luna implements production/tests.
- Every shell command starts with rtk; Python/tooling runs through Docker scripts/dev.sh.
- No push, merge, release, external cluster changes or edits to ACH repo.
- Preserve local/TUI path, phase1 session/cleanup/proxy guarantees and existing public schema.
- No new CR env blocks, queue, KEDA, S3 or lease mechanism.
- Managed credentials and full config never become engine bootstrap data.
- Keep existing explicit internal env overrides as advanced/manual compatibility paths where feasible; ordinary manifests must not require them.

## Task 1: Preserve split forwarding

Files: boot/roles.py, execution/wire.py/service.py as necessary; tests/test_split_roles.py, tests/execution/test_wire.py and relevant native child tests; operator contract/docs.

- [x] Add regression: cfg.engine.forwardEnv=[DEBUG,CUSTOM_TOOL_TOKEN] yields only names in public engineEnvNames, no values, no split rejection.
- [x] Demonstrate native process env resolves engine-side DEBUG value even if harness-side value differs. Explicit operator token may be forwarded; managed/channel-generated/preparation-only credentials remain excluded.
- [x] Remove split rejection, retain sanitized local behavior and managed-name checks. Reuse existing engineEnvNames path. Do not add schema fields.
- [x] Run focused tests, Ruff/mypy on changed scope, commit. Root reviews before Task2.

## Task 2: Harness-owned bootstrap and defaults

Files: new boot/bootstrap.py; boot/roles.py, main.py, boot/paths.py if needed; new tests/boot/test_bootstrap.py and role integration tests.

Produces concrete filesystem contract:

```text
/run/ach-agent/channels/bootstrap.json  H read/write, C read-only, absent E
/run/ach-agent/engine/bootstrap.json    H read/write, E read-only, absent C
```

Channels bundle contains a version, existing source projection, configured agent identity and internal authentication key. Engine bundle contains the existing PublicEngineConfig only. Exact DTO names internal, keep one small implementation.

- [x] Test engine/channels started before bootstrap wait boundedly and do not read full ACH_CONFIG_PATH. Missing/malformed bootstrap yields concrete startup failure, not silent native fallback.
- [x] Test harness publication produces both projections; engine has no managed secret values; key is reusable after harness restart, so existing channels can reconnect.
- [x] Implement atomic same-directory write/rename with restricted permissions, bounded regular-file reads, no-follow where available, explicit format validation. Separate mounts enforce trust. Do not put authentication in workspace/public-context.
- [x] Harness derives identity and generates key via secrets; respect explicit key override where intentionally supplied. A malformed existing key/bundle fails closed; no silent independent regeneration against live channels.
- [x] Defaults: H localhost8090, E localhost8081, C ingress0.0.0.0:8080. Only H reads full config. Roles own bootstrap default paths and bounded startup wait. Publish at an order that permits E connection and C readiness without a hydration/startup cycle.
- [x] Existing explicit role config/path/url env can remain manual opt-in, but no runtime config fields added. Preserve local launcher/native TUI behavior.
- [x] Test actual main/role startup path using synthetic hydration and native fake driver where appropriate; H restart authentication continuity and EOF cleanup remain tested.
- [x] Run focused integration tests plus source lint/type checks; commit for root review.

## Task 3: Image contract, manifests and final acceptance

Files: Dockerfile, docker/split/*, scripts/test-split.sh, tests/test_split_manifest.py, relevant integration tests, docs/schemas/operator-contract.md and reports.

- [x] All role images own tini entrypoint; containers select role using args/Compose command only, no overriding entrypoint. Preserve combined default local invocation.
- [x] Replace pre-generated role config mounts and mandatory internal URL/host/key/identity env in ordinary manifests with bootstrap mounts. H alone gets full config; source/operator credential routing remains explicit deployment work.
- [x] Provide fixed paths, permissions, default ports, exec/http probes and simple persistent/ephemeral examples. Preserve private state/session migration and engine-specific paths.
- [x] Adapt existing synthetic Compose acceptance to start from H config only and no injected internal HMAC; test both real Pi and OpenCode, same native session across turns, failure cleanup. Add explicit DEBUG/custom token forwarding proof without external secret use.
- [x] Update spec/contract and /tmp/to-ach.md with final mount/probe contract and completed vs pending status. Keep phase2/3 deferred; eliminate claims requiring operator projections.
- [x] Run appropriate targeted suites, actual Compose both engines, native local/TUI regression appropriate to changed entrypoint, and unchanged full pre-push gate in clean local clone if scanner needs .git directory. Record exact tested commits/results; final root review.

## Preflight review

| Pair/task | Shared contract | Resolution |
| --- | --- | --- |
| 1 vs 2 | roles.py projection | Sequential writers; bootstrap reuses accepted forwarding names. |
| 2 vs 3 | paths, env and image startup | Fixed two-mount contract; manifests consume implementation defaults, no separate DTO renderer. |
| Task1 | explicit token vs managed-secret rule | User-selected custom values allowed, automatic managed credentials excluded. |
| Task2 | key restart vs startup independence | Stable key in H/C-only ephemeral mount, not per-process rotation. Ordinary config rolls pod; no hot-reload framework. |
| Task3 | Kubernetes operator simplicity vs private config | Mounts/args remain operator concern, contents/bootstrap stay agent-owned. |
