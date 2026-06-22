# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

## [0.1.0] - 2026-06-22

First public release — the v1.0 MVP of the ACH execution-plane harness.

### Added

- **Engine bridge** — drive the opencode engine over HTTP/SSE (`opencode serve`): subprocess
  launch with a startup deadline (`sys.exit(1)` on timeout), SSE text-accumulation action
  extraction, the `maxInvocationSeconds` watchdog, a bounded repair turn, a shared/TTL engine
  pool, and `ek_` bearer redaction.
- **Router (core IP)** — per-session FIFO lanes enforcing the pinned `dedup → backpressure →
  lane` order with three always-enforced finite bounds (`maxConcurrentInvocations`,
  `maxInvocationSeconds`, `maxQueuedTotal`).
- **Channels** — `webhook` (GitLab MR, HMAC body auth, real dedup from `X-Gitlab-Event-UUID`),
  `slack`, `telegram`, `a2a`, and `cron` (croniter with deterministic dedup keys), all
  normalized to a canonical `MessageEvent`.
- **Actions** — `reply` and `gitlab_comment` delivery (synchronous and out-of-band); a
  consent-gated, dry-run `sideEffect` path with an audit trail.
- **Durability** — dedup store with a split fail policy, graceful drain on `SIGTERM` preserving
  in-flight invocations, and the A′ proven-start admission gate.
- **Memory** — fail-open memory adapter: degrade and continue when the backend is unreachable.
- **HTTP surface** — inbound channel events, `/healthz`, `/readyz`, and `/metrics`.
- **Conformance** — the authoritative CONTRACT §6 conformance suite (`make conformance`),
  chained into `make verify`.

[unreleased]: https://github.com/ackstorm/ach-agent/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ackstorm/ach-agent/releases/tag/v0.1.0
