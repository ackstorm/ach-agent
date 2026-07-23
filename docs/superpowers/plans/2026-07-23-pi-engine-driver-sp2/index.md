# Pi Engine Driver (SP2) — Implementation Plan Index

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Source spec:** [`docs/superpowers/specs/2026-07-23-pi-engine-driver-sp2-design.md`](../../specs/2026-07-23-pi-engine-driver-sp2-design.md)

**Goal:** Make the SP1 Pi engine **deployable** — ship the `pi` binary + pinned pi-mcp-adapter in the runtime image, finish the `engine.pi` cross-repo contract, prove Pi stats parity, and run the real-Pi e2e in CI. No new engine behaviour, no IP change.

**Architecture:** SP1 already delivered the harness code (EngineDriver seam, `engine/pi/` driver, `engine.type` config). SP2 is packaging + contract + tests: a `pi-bin` Docker stage mirroring the existing `codemem-bin` stage (reuses the image's Node 26), a docs-only CONTRACT polish, one stats-parity test, and a CI e2e job that installs the pinned Pi so `tests/e2e/test_pi_e2e.py` runs instead of skipping. The `../ach` operator change (render `engine.type`/`engine.pi`) is delivered as a **handoff prompt**, not executed here.

**Tech stack:** Docker multi-stage (explicit `COPY`, `.dockerignore`), npm-pinned Pi + pi-mcp-adapter, Python 3.12 + pytest(asyncio_mode=auto), uv / ruff / mypy --strict, GitHub Actions.

## Global Constraints

- **ek hygiene is absolute** — the `ek_`/`ACH_*` bearer never appears in Pi's env, `models.json`, `settings.json`, `mcp.json`, or the pi-mcp-adapter. SP2 adds no secret-carrying surface (CRDs carry env NAMES, never values).
- **Router / lanes / the three finite bounds are untouched.** SP2 is ops + contract + tests only.
- **Dockerfile:** multi-stage, **explicit `COPY` paths only — never `COPY . .`**, `.dockerignore` maintained, both npm versions pinned as `ARG`s.
- **Pins (D-1, resolved):** `PI_VERSION=0.81.1`, `PI_MCP_ADAPTER_VERSION=2.11.0` — npm-installed in the Dockerfile (no git-vendoring). Adapter package root resolves to `/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter` (npm flattens deps as siblings; Node resolves them).
- **Stats:** redis-stream entries keep `v="1"`; Prometheus labels stay low-cardinality (`model`, `channel`, `tool`, `status`) — never `session_key`.
- **CI (D-4, resolved):** the real-Pi e2e runs in CI against the pinned binary; the Dockerfile build-time `pi --version` smoke is the in-image packaging authority.

## Resolved decisions (spec §12)

| # | Decision | Resolution |
|---|----------|------------|
| D-1 | adapter vendoring | **npm-pin in Dockerfile** (mirror `codemem-bin`; no repo bloat) |
| D-2 | `../ach` `EngineSpec.Type` | **free `string`** (harness is the enforcer) |
| D-3 | Pi `cost` source | **parity — already satisfied.** Pi maps engine-reported `cost` exactly like opencode (`engine/pi/events.py:95-109` ≟ `engine/opencode/events.py:216-220`); `tokens_per_s` is computed centrally (`stats/models.py:57`). Item 3 shrinks to a verification test — **no mapping rewrite.** |
| D-4 | e2e in CI | **run the real-Pi e2e in CI** against the pinned binary |

## Phases

| Phase | Delivers | Files |
|-------|----------|-------|
| 1 | `pi-bin` Docker stage (pin Pi + adapter) + adapter-path fix + build smoke | `Dockerfile`, `engine/pi/driver.py:39`, `tests/e2e/test_pi_e2e.py:75` |
| 2 | CONTRACT_v3 `engine.pi` cross-repo contract polish (docs) | `docs/plan/CONTRACT_v3.md` |
| 3 | Pi stats-parity verification test | `tests/stats/test_pi_turn_stat_parity.py` |
| 4 | CI e2e job — pinned Pi installed, `test_pi_e2e.py` runs instead of skipping | `.github/workflows/ci.yml` |
| — | **Handoff:** `../ach` renders `engine.type`/`engine.pi` | [`handoff-ach-render.md`](handoff-ach-render.md) (separate repo — prompt only) |

**Dependency order:** `1 → (2, 3 in parallel) → 4 → handoff`. Phase 4 consumes Phase 1's `ARG` pins (grep'd from the Dockerfile — single source, no drift). The handoff has a hard precondition: **an ach-agent image carrying `pi` must be released before `../ach` advertises `engine.type: pi`** (spec §2).

## Handoff

After Phase 4 is green, hand [`handoff-ach-render.md`](handoff-ach-render.md) to the ACH operator agent (`../ach` repo). It is a self-contained prompt; do not execute it in this repo.

## Self-review

Every spec item maps to a phase: §4→P1, §5→P2, §6→P3, §7→P4, §8→handoff. No placeholders — pins are concrete (`0.81.1`/`2.11.0`), the adapter path is resolved against the real npm tarball, and D-3 is verified already-parity rather than assumed.
