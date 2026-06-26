# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

## [0.2.0] - 2026-06-26

The v1.1 milestone — engine rescoped to **opencode** with localhost-proxy ek-hygiene,
full v3-contract alignment, and a zero-friction local dev-loop.

### Added
- **YAML-authored contracts**: `load_config` accepts `.yaml`/`.yml`, validated against the
  same schema as the rendered JSON contract.
- **`--prompt TEXT`** one-shot launch modifier (non-interactive sibling of `--tui`): boot,
  run a single free-form prompt, print the reply, exit.
- **`--tui`** launch modifier: boot engine/proxies/hydration and run a console REPL,
  ignoring configured channels (the typed line is the prompt).
- **Localhost reverse-proxy** fronting model + MCP that injects the ACH key (`ek`), so
  opencode never sees it (§6.10 ek-hygiene).
- **Hydration** via `POST /platform/hydrate`; context (skills/prompts/artifacts) downloaded
  and extracted (path-traversal guarded).
- **Channels**: webhook (gitlab|github|generic + auth), cron, queue (redis Streams), a2a (async).
- **Runnable container**: opencode baked in, default contract baked, `ENTRYPOINT` so
  `docker run -it -e ACH_TOKEN=ek-... IMAGE --tui` works with zero mounted files.
- `prompt.base` wired into opencode's append-mode `instructions` (inline agent persona).

### Changed
- Engine reverted to **opencode** (`opencode serve` + SSE); single-object terminal contract
  (`{action,text,thoughts}`), harness-validated (extract + Pydantic + ≤1 repair).
- ACH auth via the **`x-ach-key`** header; `ek-` keys; `runtime.models` are objects
  `{id,endpoint}` and the model proxy uses the model's real endpoint path.

### Removed
- slack/telegram channels + the Hermes dependency; harness-side delivery (egress is
  model-initiated via external MCP, never posted on the model's behalf).

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
