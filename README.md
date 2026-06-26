# ach-agent

[![ci](https://github.com/ackstorm/ach-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/ackstorm/ach-agent/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)

`ach-agent` is the **execution plane** of the ACH ecosystem: a single-process **Python**
runtime ("the harness") that boots from a rendered runtime config, runs channel adapters
(`webhook`, `slack`, `telegram`, `a2a`, `cron`), serializes inbound events through a governed
FIFO **router**, drives the [opencode](https://github.com/sst/opencode) engine over HTTP/SSE,
and delivers results via a `reply` / `sideEffect` action contract.

It consumes the frozen seam produced by `ach-runtime` (the Go operator) and never reads CRDs,
talks to the Kubernetes API server, or writes `Agent.status` — status is the operator's job.
It is designed for platform / AI-engineering teams running managed AI agents (e.g. a GitLab MR
reviewer) on top of the `runtime.ackstorm.ai/v1alpha1` API.

## Core value — the router

The one thing that must always hold: **the router is correct.** It enforces per-session FIFO
lanes with the pinned ordering `dedup → backpressure → lane` and three always-enforced finite
bounds (`maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`). This is what
prevents duplicate firing, queue starvation under redelivery floods, and unbounded resource
use. Its behavior is pinned by an authoritative conformance suite (`make conformance`).

## How it works

```
channel adapter ──▶ router (dedup → backpressure → lane) ──▶ engine (opencode HTTP/SSE) ──▶ delivery
   webhook                  per-session FIFO                    {"actions":[...]}            reply
   slack                    finite bounds                                                    gitlab_comment
   telegram                                                                                  sideEffect (consent-gated)
   a2a
   cron
```

Everything runs in one process (spec §15 topology A); the channel→router boundary is a named
in-process seam. The harness is fully runnable and testable locally from a hand-written config —
no operator or cluster required.

## Quick start (local dev)

All tooling runs inside a content-addressed devtools container — **no host pip/venv**. The only
prerequisites are Docker and `make`.

```bash
make hooks       # install the pre-push gate
make deps        # sync dependencies into the devtools layer
make lint        # ruff check + format --check + mypy --strict
make test        # pytest (unit + integration, excludes e2e)
make conformance # CONTRACT §6 conformance suite (the router IP)
make verify      # full local gate: lint + test + conformance + secrets
make e2e         # full end-to-end stack (compose up → assertions → teardown)
```

Run `make` with no target for the full self-documenting target list.

## Configuration

The harness boots from a single rendered config file (JSON) plus a small `ACH_*` environment
contract. See [`.env.example`](.env.example) for the variables. In production these are rendered
into the pod by `ach-runtime`; for local runs you provide them yourself.

| Variable | Purpose |
|----------|---------|
| `ACH_CONFIG_PATH` | Path to the rendered runtime config (default `/etc/ach-agent/config.json`). |
| `ACH_BASE_URL` | ACH endpoint. Overrides `capability.ach.baseUrl` when set, so a config can ship without a hardcoded host (required if the config omits `baseUrl`). |
| `ACH_API_KEY` | `ek_` bearer for the engine — never logged; dereferenced only at runtime. |

Channel credentials (`GITLAB_TOKEN`, `SLACK_BOT_TOKEN`, `TELEGRAM_BOT_TOKEN`, …) are supplied
per the channels your config enables.

## Deployment

A Helm chart and a Kustomize base are provided under [`deploy/`](deploy/):

```bash
make helm-lint                       # lint the chart
make helm-template                   # render to stdout
helm install ach-agent deploy/helm/ach-agent

make kustomize-build                 # render the kustomize base
kubectl apply -k deploy/kustomize/base
```

Released container images are published to `ghcr.io/ackstorm/ach-agent` and the chart to
`oci://ghcr.io/ackstorm/charts`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Run
`make verify` before pushing — the pre-push hook enforces the same gate. Security issues: see
[SECURITY.md](SECURITY.md).

## License

Apache-2.0 — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
