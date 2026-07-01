# ach-agent

`ach-agent` is the **execution plane** of the ACH ecosystem: a single-process Python runtime
that boots from a rendered runtime config, runs channel adapters, serializes inbound events
through a governed FIFO router, drives the **opencode** engine over HTTP/SSE, and delivers
results via a `reply` / `sideEffect` action contract. It consumes the frozen seam produced by
`ach-runtime` (the Go operator) and is designed for platform and AI-engineering teams running
managed AI agents — such as a GitLab MR reviewer — on top of the `runtime.ackstorm.ai/v1alpha1`
API.

## Core value — the router

The router must be correct: per-session FIFO lanes with the pinned ordering
`dedup → backpressure → lane` and three always-enforced finite bounds
(`maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`). Its behavior is pinned
by the CONTRACT §6 conformance suite (`make conformance`).

## Engine — per-session opencode servers

The engine pool is keyed by `session_key`: each session identity (cron name, gitlab server+repo,
tui-console) gets its **own** `opencode serve` process with its own isolated HOME (config, session
store, `node_modules`), so distinct sessions never share filesystem state. The working directory
(`/workspace`) stays shared. See the design record:
[Keyed EnginePool](references/2026-07-01-keyed-engine-pool.md).

## Getting started

See the [README](https://github.com/ackstorm/ach-agent#readme) for the quick-start and the
`ACH_*` configuration contract. In production the `ach-runtime` operator deploys the harness
(it owns the Deployment); locally, run the container directly — see [Getting started](getting-started.md).

All tooling runs inside a content-addressed devtools container — no host pip/venv:

```bash
make verify   # full local gate: lint + mypy + unit + conformance + secrets
```
