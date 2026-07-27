# CONTRACT_v3 follow-up — `memory.type` backend selector

**Status:** OPEN (cross-repo). Opened 2026-07-01.
**Owner repos:** `ach-runtime` (Go operator, renders the config) + `docs/plan/CONTRACT_v3.md` (the seam).
**Blocks:** production `codemem` deployments only. Does NOT block the harness (merged to main
2026-07-01, backward-compatible). NOTE: this file lives under `docs/plan/` which is git-ignored
(internal, on-disk only) — it is a local reference next to CONTRACT_v3.md, not a committed artifact.

## What shipped in the harness (merged to main: 03361ed)

`ach-agent` now accepts `memory.type` as a discriminated union (`hindsight | codemem`):

```yaml
# hindsight (legacy shape still accepted with NO `type` — defaults to hindsight)
memory:
  type: hindsight            # optional; absent => hindsight (backward-compat)
  endpoint: http://...
  bank: <bank-id>
  mentalModels: [...]

# codemem (new)
memory:
  type: codemem
  dbPath: /var/lib/codemem/agent.db   # absolute, no '..'; static per-agent
```

Backward-compatible: a legacy `memory:` block with no `type` loads as `hindsight`, so existing
rendered configs keep working with no operator change.

## What the operator (`ach-runtime`) must do BEFORE production codemem

`memory` is contract-reserved (CONTRACT_v3 §2) — the Go operator renders it from the CRD.
Today it renders only the flat hindsight shape. To deploy a codemem agent it must:

1. **CONTRACT_v3.md:** document `memory.type` (`hindsight | codemem`) + per-type fields
   (`codemem.dbPath`; hindsight keeps `endpoint`/`bank`/`mentalModels`). `dbPath` is a static
   per-agent absolute path (operator-provided, trusted — NOT from inbound payload; NOT templated
   per-repo — the harness pool fixes opencode.json at server launch, so per-event db paths would
   break pool reuse).
2. **CRD → render:** add the `memory.type` selector to the `Agent` CRD spec and render the
   matching sub-block. For codemem, render `{type: codemem, dbPath: <path>}` and ensure the
   `dbPath` parent dir is a writable, persistent mounted volume in the pod (uid 10001).
3. **Image:** deploy the `ach-agent` image built with codemem baked in (Node 24 + `codemem`
   CLI on PATH — see the `codemem-bin` Dockerfile stage; pinned `codemem@0.37.1`).

## Locked decisions (harness side)

- codemem runs as a **stdio `type:local` MCP** child of opencode (opencode owns its lifecycle,
  1:1 per opencode process). No extra port, no harness-managed sidecar. `opencode serve --pure`
  stays (trust boundary vs the untrusted workspace repo).
- **Model-managed** (v1): no auto-capture, no prompt-injection. The model calls codemem's MCP
  tools (`search`/`remember`/`timeline`) on demand — keeps the system-prompt prefix stable
  (prompt-cache friendly).
- codemem defaults to SQLite **WAL** (verified) — concurrent stdio children on the same db get
  N readers + 1 writer; no per-repo pool affinity needed at the model-managed write rate.
- **Out of v1 scope:** per-repo/multi-tenant `dbPath` templating; auto-capture via the harness
  SSE event stream (`engine/events.py` → codemem ingest queue) if ever wanted.

Reference: harness plan `docs/superpowers/plans/2026-06-30-codemem-memory-backend.md`.
Local test env: `../ach-agent-test/{config.codemem.yaml,docker-compose.codemem.yaml}`.
