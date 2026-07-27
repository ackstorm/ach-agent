# ach-agent

`ach-agent` is a generic **execution plane** for managed AI agents: a single-process **Python**
runtime ("the harness") that boots from a rendered runtime config, runs channel adapters
(`webhook`, `cron`, `queue`, `a2a`), serializes inbound events through a governed
FIFO **router**, drives the [opencode](https://github.com/sst/opencode) engine over HTTP/SSE,
and lets the agent act through external MCP servers.

It consumes a frozen, rendered config seam and is control-plane-agnostic: it never reads CRDs,
talks to the Kubernetes API server, or reports status upward. It is designed for platform /
AI-engineering teams running managed AI agents (e.g. a GitLab MR reviewer); ACH is the
reference consumer, not a dependency.

## Core value — the router

The one thing that must always hold: **the router is correct.** It enforces per-session FIFO
lanes with the pinned ordering `dedup → backpressure → lane` and three always-enforced finite
bounds (`maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`). This is what
prevents duplicate firing, queue starvation under redelivery floods, and unbounded resource
use. Its behavior is pinned by an authoritative conformance suite (`make conformance`).

## How it works

```
channel adapter ──▶ router (dedup → backpressure → lane) ──▶ engine (opencode HTTP/SSE) ──▶ external MCP servers
   webhook                  per-session FIFO                    single-object terminal          (ACH-fronted, egress
   cron                     finite bounds                       contract (Pydantic-validated)    is model-initiated)
   queue
   a2a
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

## Configuration

The harness boots from a single rendered config file (JSON) plus a small `ACH_*` environment
contract. In production these are rendered into the pod by `ach-runtime`; for local runs you provide them yourself.

| Variable | Purpose |
|----------|---------|
| `ACH_CONFIG_PATH` | Path to the rendered runtime config (default `/etc/ach-agent/config.json`). |
| `ACH_BASE_URL` | ACH endpoint. Overrides `capability.ach.baseUrl` when set. |
| `ACH_API_KEY` | `ek_` bearer for the engine — never logged; dereferenced only at runtime. |

### Channel prompts (`{{ }}` templating)

A channel may carry a `prompt` — the per-invocation instruction handed to the engine. It is
rendered through a small, zero-dependency `{{ }}` substitution engine against the inbound event:

```yaml
channels:
  - name: gitlab-mr-review
    type: webhook
    source: gitlab
    prompt: "Review merge request {{ payload.object_attributes.url }} in {{ payload.project.path_with_namespace | default(\"this repo\") }}."
```

**Namespaces**: `payload.*` (webhook, queue, a2a) and `internal.*` (all channels). There is no `env` namespace.

## Deployment

In production the harness is **not** deployed by hand — the **`ach-runtime` operator** builds
the `Deployment` from your `Agent` CRD. The harness has no Kubernetes RBAC and never talks to the API server.

Released container images are published to `ghcr.io/ackstorm/ach-agent`.
