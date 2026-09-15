# Stable Workspace Placement Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Luna implements; root reviews and verifies.

**Goal:** Preserve the original persistent workspace path and native sessions when switching standalone to distributed.

**Architecture:** Keep the existing standalone workspace at base/home/workspace. Distributed mounts the same physical PVC home/workspace directory there; Engine sees it within its home mount, Harness mounts only the workspace. Shared path resolution applies to Pi, OpenCode and future engines, without native export/import or new protocols.

**Tech Stack:** Python, existing path resolver, Docker, existing engine integrations.

**Spec:** User-approved stable-folder correction and /tmp/to-ach-pvc-transition.md; historical failure documented in docs/reports/2026-09-15-pvc-placement-transition.md.

## Constraints

- Preserve standalone defaults and behavior; no schema changes.
- Distributed base remains persistence.mountPath or /tmp/ach-agent.
- Distributed default workspace is base/home/workspace; validate workDir within that mounted root. Preserve valid explicit paths under the mounted roots.
- Operator changes both mount path and physical subPath to home/workspace. No old workspace copy is needed for the original standalone layout.
- Do not add native session migration, edit native databases, reset mappings, or change engine adapters.
- Do not claim arbitrary custom-layout or already-distributed base/workspace migration support.
- Use Docker development tooling via rtk proxy ./scripts/dev.sh, never host Python.

## Task 1: Common paths and deployment contract

Files: src/ach_agent/boot/paths.py; existing path/bootstrap tests; docker/split manifests and associated integration fixtures; docs/schemas/ach-deployment-modes.md.

- [x] Add regression tests asserting persistent standalone/distributed workspace equality, parametrized for Pi and OpenCode, and no relocation of files from the original directory.
- [x] Run focused tests and observe the old base/workspace default fail.
- [x] Set distributed workspace root to mount/home/workspace and default workDir to engine_home/workspace; keep validation against the mounted root.
- [x] Update split fixtures/manifests to mount physical home/workspace at base/home/workspace. Engine uses its home mount; Harness sees workspace only.
- [x] Update deployment contract, retain standalone defaults, document coordinated image/operator rollout.
- [x] Run focused tests, lint and review; commit only task files.

## Task 2: Native continuity acceptance

Files: scripts/test-pvc-transition.sh and/or existing tests/integration/fixtures/split mock-provider fixtures.

- [x] Use real Pi and OpenCode binaries with the existing deterministic mock model and persistent Docker data.
- [x] Complete a standalone turn; record session reference and workspace marker.
- [x] Restart in distributed mode using the same data and identical absolute workspace path; mutate marker via Harness.
- [x] Complete another turn with unchanged session reference, retained conversation and a native tool reading the active marker.
- [x] Verify Harness cannot read Engine home outside the shared workspace. Clean up temporary containers/data.
- [x] Root reviews diffs, runs required full tests/lint/types and relevant acceptance; record actual evidence and cluster limitation.

## Release

- [ ] Publish only after local validation; pair the resulting image with the revised ACH mounts. Do not roll production on old mounts.
- [ ] Cluster transition validation follows ACH's aligned renderer and permissions fix.

## Superseded approach

An earlier plan proposed OpenCode native export/import to compensate for moved paths. The stable-folder decision replaces that approach entirely. No native relocation code is required.
