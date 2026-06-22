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

## Getting started

See the [README](https://github.com/ackstorm/ach-agent#readme) for the quick-start, the
`ACH_*` configuration contract, and deployment via the Helm chart / Kustomize base.

All tooling runs inside a content-addressed devtools container — no host pip/venv:

```bash
make verify   # full local gate: lint + mypy + unit + conformance + secrets
```
