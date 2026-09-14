# Startup Hydration, Engine Environment and Operator Storage — Implementation Plan

> Execute with GPT-5.6-Luna implementers. Root reviews every diff and runs final verification. No release or push.

**Goal:** Install hydration before first work, let ACH supply selected engine environment, and provide role healthchecks so the operator renders only generic storage/probes.

**Baseline:** main `1b240ef`. Worktree `feat/operator-storage` contains interrupted drafts; reconcile them with this plan rather than treating them as approved implementation.

**Binding decisions:** one logical channels → harness → engine flow; sockets are process seams. Harness downloads using credentials, mini-harness copies/validates native boot inputs, deletes the batch, then reports startup success. Channel prepare/cleanup remain separate and use workspace.

## Contract

- Distributed base = persistence.mountPath or /tmp/ach-agent. H-private base/state; E-private base/home; H/E base/workspace. A separate temporary /run/ach-agent/transfer mount contains mkdtemp batches named .ach-harness-shared-files-*.
- Keep two IPC directories and private per-container /tmp. No operator knowledge of native files/tools.
- Public engine wire config carries hydrationDir and engineEnvNames, not env values. Existing forwardEnv public agent field is unchanged. C/H operator env equal; E receives selected operator env; local launcher applies same selection to its child.
- Mini-harness resolves allowed native/MCP env references from its own environment, retaining managed-credential filtering and existing pinned HOME/PATH rules.
- H resolves prompt text it needs before handing the completed batch to E. H never reads the consumed batch afterward.
- Installed context is in E HOME, not persistent links into transfer. Native skills directories remain engine-specific inside E HOME.
- Keep existing controller/open seam and native session/cleanup/result contracts. Discovery endpoint works pre-init; public startup health/readiness do not.
- Init/install/delete failure => unhealthy + nonzero engine-role exit, no invocation. Native turn launch failures remain per invocation.
- Role health command: python -m ach_agent.healthcheck --role ROLE --check startup|readiness|liveness. Operator sees no socket paths in probe scripts. 15-second startup delay, 5-second period, 3-second timeout, 6 failures.
- No changes to frozen public agent schema fields; no queue, lease, new scheduler or HMAC.
- Preserve original persistent data. Unsupported custom distributed paths fail explicitly; never silently create an empty replacement database. Standalone explicit paths remain supported.

## Task 1 — Harness preparation, path/env wire and compatibility

Files: boot/paths.py, boot/roles.py (projection only), boot/local.py, main.py,
execution/wire.py; affected path/role/local/config tests. Helper modules only for focused data compatibility.

- [x] Replace interrupted path code: path resolution must be pure, never rename another container's HOME.
- [x] Resolve defaults for distributed/standalone, retain explicit compatible paths; move optional native db defaults inside E-private storage. Preserve legacy native data with agent-owned bounded handoff, not renderer logic.
- [x] Create a fresh download batch in shared transfer mount (local launcher owns an equivalent temporary mount directory). Download into it; resolve H-owned prompt before init.
- [x] Set hydrationDir on controller-open config. Stop writing/reading that batch after handoff.
- [x] Replace public engineEnv values with engineEnvNames. Distributed H does not resolve/transmit values. Local parent sets eligible selected values in child environment.
- [x] Update both local default and native TUI paths. Keep pre-init discovery distinct from completed health; no deadlock before sending init.
- [x] Bounded retries must not reuse a deleted batch for a new E instance. Reconnect to initialized same instance preserves native sessions without reinitializing per event.
- [x] Test path defaults, explicit paths, selected values absent from wire, local child selection and hydration ownership.

## Task 2 — Mini-harness init and role health

Files: execution/service.py, execution/app.py, engine/context.py, engine adapters/config helpers,
healthcheck.py; execution/native/health tests. Coordinate run_engine watcher changes with Task1 owner.

- [x] Consume hydrationDir at controller-open startup before admitting work; reject concurrent initialization.
- [x] Copy managed skills/prompts/artifacts into engine HOME with no links into staging. Preserve native session/cache files, remove stale managed skills. Validate transfer batch boundaries.
- [x] Generate/validate boot-static native config using existing writers, check required executable/integration files, no dummy native session or model turn.
- [x] E resolves engineEnvNames from its own env for native env/MCP config.
- [x] Delete batch only after successful install/check; deletion failure fails init.
- [x] Make /healthz and /readyz unsuccessful before initialized. Preserve /execution/v1/health as pre-init transport discovery. Set unhealthy/shutdown on genuine init failure and exit E role nonzero via current supervisor.
- [x] Implement role healthcheck CLI with internal socket/TCP discovery, finite2s request, exit0/1. No credential/log body dumping.
- [x] Test delayed init, valid content retained after staging removal, config output, failures/copy/delete/binary, concurrent controller and restart ownership. Tests using fake drivers need an explicit test seam, not production skip-validation flags.

## Task 3 — Packaging and operator handoff

Files: Dockerfile, docker/split manifests/config examples, tests/integration/fixtures/split,
scripts/test-split.sh, packaging tests. Root owns general docs/operator message.

- [x] Exactly generic data mounts + temporary transfer + existing twoIPC; C/H same env; E only explicit forwarding selected variables in Compose acceptance.
- [x] Remove permanent public-context and codemem-specific mounts/config snippets. Existing native tool paths are internal agent logic.
- [x] Use role healthcheck commands and agreed startup scheduling.
- [x] Persistent/nonpersistent fixtures use defaults and actually exercise boot init before first event.
- [x] Real acceptance checks transfer batch gone, installed content survives, engine HOME private, H state private, shared workspace hooks and selected env.
- [x] Update README/reference/spec/handoff; /tmp/to-ach.md is self-contained and exact.

## Task 4 — Root review and acceptance

- [x] Review runtime and packaging diffs; resolve all correctness/compatibility gaps.
- [x] Full Docker pytest, Ruff, mypy, schema artifact drift and MkDocs strict build.
- [x] Real OpenCode/Pi three-container startup/two turns/cancel/failure plus nonpersistent smoke. Re-test native TUI path if launch wiring changed.
- [x] Record precise new evidence and limits, commit on working branch; keep release/push paused.
