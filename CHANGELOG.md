# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

### Added
- **`ACH_OPENCODE_PORT`** pins the opencode `serve` port (default `0` = ephemeral). Set a
  fixed port so a container can publish it (`ports:`) and the opencode web UI is reachable
  from the host. Pair with `ACH_OPENCODE_BIND_HOST=0.0.0.0`. Dev/test only — collides if two
  harness instances share the port.

## [0.3.2] - 2026-06-27

### Added
- **`ACH_OPENCODE_BIND_HOST`** controls the interface opencode `serve` binds to (default
  `127.0.0.1`). Set to `0.0.0.0` to expose the opencode HTTP API + web UI on all interfaces
  (e.g. to open the web UI from your host's browser). Binding a non-loopback interface logs a
  loud security warning — the opencode API runs without authentication, dev/test only. The
  harness HTTP client always connects via loopback regardless.
- **`--tui` pre-warms opencode at boot**: the console now launches the opencode server up
  front (instead of lazily on the first prompt) and holds it for the whole REPL, so there is
  no idle TTL between prompts — only Ctrl-C / EOF ends the session.

### Changed
- **Engine idle TTL is now a per-channel constant** (`_CHANNEL_IDLE_TTL_S`), `0` for all v1
  channels, so the opencode server stops as soon as a conversation ends. Replaces the global
  `ACH_ENGINE_IDLE_TTL_SECONDS` env / `engine.idleTtlSeconds` resolution. The
  `engine.idleTtlSeconds` config field is retained for back-compat but no longer has any effect.

## [0.3.1] - 2026-06-26

### Changed
- **opencode runtime bumped `1.16.0` → `1.17.11`** (`OPENCODE_VERSION` in the Dockerfile).
  Verified live against ACH (streaming console + tool chrome + calendar MCP flow).

## [0.3.0] - 2026-06-26

### Added
- **Live streaming console**: the `--tui`/`--prompt` console now streams the assistant's
  text as it is produced and shows one-line tool-lifecycle chrome (`⚙ running` / `⚠ error`),
  so a long-blocking tool (e.g. a calendar `auth_wait`) is no longer dead air. Text comes
  from opencode's cumulative `message.part.updated` snapshots (suffix-diffed per part).
- **Model-proxy upstream override (dev/test only)** via `ACH_MODEL_BASE_URL` /
  `ACH_MODEL_HEADER` / `ACH_MODEL_TOKEN`: swap just the model backend (hydration + MCP stay
  on `ACH_BASE_URL`) to A/B a different gateway. The token is injected verbatim as the header
  value. Uses a raw provider key, not the `ek_` — bypasses ACH governance; never for production.
- **`ACH_DEBUG_SSE=1`** raw per-event SSE trace and **`ACH_LOG_LEVEL`** console verbosity knob.
- **Boot-time MCP tool probe**: lists the tools each hydrated MCP server exposes and warns when
  a server returns zero (surfaces an unauthorized/unconsented server at boot, not mid-invocation).

### Changed
- **`capability.ach.baseUrl` is now optional and overridable via `ACH_BASE_URL`**: the env var,
  when set, wins over the contract's `baseUrl` (and supplies it when the contract omits it).
  `load_config` hard-fails only if neither provides a host. The shipped `docker/` sample +
  quickstart configs no longer carry a hardcoded ACH host — they expect `ACH_BASE_URL` at runtime.

### Fixed
- **Clear boot failure on an unresolvable model endpoint**: when neither an `ek_` (`ACH_TOKEN`)
  nor `ACH_BASE_URL` is set, the harness now exits at boot with an actionable message instead
  of letting opencode fail every invocation with the opaque
  `"/chat/completions" cannot be parsed as a URL`. Also warns when `ACH_MODEL_TOKEN` carries an
  empty credential (e.g. an unexpanded `${...}` var).

## [0.2.1] - 2026-06-26

### Fixed
- **Proxy teardown latency**: bounded the localhost MCP/model proxy `shutdown_timeout` to 1s.
  aiohttp's 60s default made shutdown hang ~60s after the reply when a long-lived MCP/SSE
  stream was still open.

### Changed
- **Slimmer image (−21MB, 339→318)**: dropped `uvicorn[standard]` (the uvloop/httptools/
  websockets/watchfiles extras aren't needed for this single-replica surface) and pruned
  `__pycache__`/`*.pyi`/bundled test suites from deps.
- **`capability.ach.environment` is now optional** (defaults to `"platform"`): the EK scopes
  the environment server-side, so hand-authored configs can omit it.

### Added
- `docker/quickstart/` — standalone `docker-compose.yaml` + `config.yaml` using the public
  image (`docker compose run --rm agent`).

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
