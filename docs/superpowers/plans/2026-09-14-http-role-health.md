# HTTP Role Health Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development. Luna implements runtime code; the root agent reviews and verifies.

**Goal:** Kubernetes uses ordinary HTTP probes for all roles, with identical startup and downstream readiness semantics in standalone and distributed placement.

**Architecture:** Keep application traffic over existing Unix sockets. Add health-only TCP listeners for Harness (8090) and Engine (8081); Channels retains 8080. Standalone retains its configured public port and shares the engine readiness gate.

**Tech Stack:** Python, FastAPI, uvicorn, pytest, Docker Compose, Kubernetes.

**Spec:** User-approved HTTP probe contract in this conversation and `docs/schemas/ach-deployment-modes.md` (updated by Task 2).

## Constraints

- No schema, queue, session, workspace or hydration redesign.
- `/readyz` cannot succeed before startup hydration installation and configuration finish.
- Engine loss makes downstream readiness fail in either placement.
- Expose only health endpoints on new TCP listeners; internal execution APIs stay on Unix sockets.
- Kubernetes uses `httpGet`, never exec probes. Docker healthchecks necessarily execute a client, which must use ordinary HTTP.
- Preserve concurrent local standalone launches without fixed child health-port collisions.

## Task 1: Runtime health listeners and readiness parity

Files: `src/ach_agent/boot/roles.py`, `src/ach_agent/boot/health.py`, `src/ach_agent/main.py`, `src/ach_agent/http/app.py`, execution health routes and related tests.

- [ ] Add failing tests: HTTP health is 503 before initialization and 200 after installation; private execution routes return 404 over TCP; standalone and distributed lose readiness when E disappears.
- [ ] Add health-only listeners and share readiness state with existing role apps. Ensure listener shutdown follows role lifecycle.
- [ ] Use the existing engine readiness check in both placements; do not let app lifespan overwrite managed readiness.
- [ ] Remove the image probe command and its obsolete tests.
- [ ] Run focused runtime tests, Ruff and strict mypy. Commit runtime changes.

## Task 2: Operator and deployment contract

Files: `docker/split/{pod,compose,compose-ephemeral}.yaml`, `tests/test_split_manifest.py`, `scripts/test-split.sh`, README and current reference/handoff docs.

- [ ] Replace Kubernetes exec probes with `httpGet`; startup/readiness `/readyz`, liveness `/healthz`, ports C8080/H8090/E8081.
- [ ] Change Compose checks and acceptance checks to direct HTTP URLs; remove obsolete socket-health documentation.
- [ ] Assert every Kubernetes role has exactly the approved HTTP probe handlers and startup budget; run manifest tests.
- [ ] Update `/tmp/to-ach.md` from the canonical operator handoff.

## Task 3: Review and verification

- [ ] Root reviews source, parity and lifecycle behavior, including absence of private routes on TCP.
- [ ] Run full test suite, lint, types, schema check and strict documentation build.
- [ ] Exercise real three-container startup and engine failure with HTTP probes; exercise standalone startup and engine loss.
- [ ] Record evidence and commit reviewed changes. Do not claim the published v0.16.2 image includes this correction.
