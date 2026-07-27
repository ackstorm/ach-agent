# Configuration

The harness boots from **one config file** plus a few `ACH_*` environment variables. In
production `ach-runtime` renders the file (JSON) into the pod; locally you hand-author it as
YAML. Both validate against the same schema, and **unknown keys are rejected** (`extra=forbid`).

## Environment

| Variable | Purpose |
|----------|---------|
| `ACH_TOKEN` / `ACH_API_KEY` | The `ek_` bearer for the engine — never logged, dereferenced only at runtime, never reaches opencode. |
| `ACH_BASE_URL` | ACH endpoint. Overrides `capability.ach.baseUrl` when set; required if the config omits `baseUrl`. |
| `ACH_CONFIG_PATH` | Path to the config file (default `/etc/ach-agent/config.json`). |

## Metrics and Identity

Every sample exposed by `GET /metrics/` carries process-authoritative `agent` and `environment`
labels. Stamping is applied at exposition across all metrics (including Python/process default
series and `name[]`-restricted scrapes), so current and future metric families require no call-site
labeling.

- **Identity Sources**: `agent` comes from rendered `agent.name`. `environment` comes from the
  successfully validated hydration manifest; the hydration request itself uses `capability.ach.environment`
  as its requested environment.
- **Outbound Identity Headers**: All model, MCP, outbound A2A, and hydration requests receive
  canonical `x-ach-agent` and `x-ach-environment` headers injected by the harness.
- **Header Sanitization**: Any client-supplied model or MCP identity headers are removed
  case-insensitively before forwarding; harness process identity always wins.
- **Direct Model Overrides**: Development model overrides (`ACH_MODEL_BASE_URL` / `ACH_MODEL_HEADER`)
  retain full identity header injection.
- **Prometheus Series Identity**: Adding these labels to every exposed sample changes Prometheus
  series identity in `v0.10.1`. Range queries spanning rollout show a series discontinuity
  between pre-`v0.10.1` and post-`v0.10.1` metrics.
- **Operator Impact**: No `PodMonitor` relabeling rules or `group_left` metric joins are required
  or used by this repository.

## Blocks

| Block | Required | What |
|-------|----------|------|
| `schemaVersion` | ✓ | Must be the quoted string `"1"`. |
| `agent.name` | ✓ | The agent's name. |
| `model` | ✓ | `name` (ACH-served model id, verbatim), `type` (`openai`\|`gemini`\|`anthropic` — picks the compat wire), `params` (open dict, splatted to the client). |
| `capability` | ✓ | `type: ach`; `ach.baseUrl` / `ach.environment`; `filter.exclude` withholds `tools` / `mcpServers` / `skills` **before** the model sees them. |
| `prompt` | | `system` is a typed source: `{type: text, text: "…"}` inline; `{type: ach, ach: "<prompt-name>"}` for a hydrated prompt addressed by name (harness resolves its sole file, or a given `file:` subpath) — the preferred form; or `{type: file, file: "prompts/<name>/<file>.md"}` addressed by path. `file`/`ach` resolve under `<home>/.ach-state` (absolute or `..` rejected; missing = hard boot failure). The bare-string form is rejected. `compose` is contract-reserved (accepted; prompt-layering not yet executed by the harness). |
| `memory` | | Fail-open. `endpoint`, `bank` (static memory bank_id), `mentalModels`. `mission` is contract-reserved (accepted; not yet consumed). Backend down → run without it. |
| `limits` | | `maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`, `idempotencyWindowSeconds`, `maxSteps`, `terminalOutputRetries`. |
| `engine` | | Harness-local. `home`, `workDir`, `startupTimeoutSeconds`, `forwardEnv` (default-deny env allowlist — see below). |
| `cost` | | `source`: `engine` (default), `litellm_usage`, `litellm_headers`, or `none`. Controls the source of the per-invocation cost figure. |
| `persistence` | | `enabled` (false → in-memory dedup, no volume), `mountPath`. |
| `health` | | `host` / `port` for the HTTP surface (healthz/readyz/metrics + webhooks). |
| `channels` | | List of channel adapters (below). |

### `cost.source` turn accounting

`cost` is optional. If it is omitted, or if `source` is omitted inside the block, the
source defaults to `engine`, preserving the existing engine-reported cost behavior.
Unknown source values are rejected at config load. The cost metric name and labels are
unchanged for every source. When `source` is not `engine`, the source-selected value is
also written to the stats row and emitted in the turn summary log; this is intentional.

| `cost.source` | Semantics |
|---|---|
| `engine` | Default and wire-independent. Preserve the engine-reported usage/cost record; the cost layer is inert and does not inspect or mutate model traffic. |
| `litellm_usage` | Parse per-response usage and price it with the ACH model-info cache. OpenAI and Gemini wires are supported. OpenAI streaming requests receive `stream_options.include_usage: true`; Gemini's repeated cumulative usage uses the final payload, with thinking tokens billed at the output rate. |
| `litellm_headers` | Read `x-litellm-response-cost` on non-streaming responses and sum it for the invocation. Streaming responses contribute `0.0` and one bounded warning per invocation. This mode never changes `stream` or any other request field. |
| `none` | Suppress the cost value and cost-counter increment while preserving invocation, duration, status, error, and token metrics. |

`litellm_usage` is the only wire-restricted source: it supports `model.type: openai`
and `model.type: gemini`. `model.type: anthropic` hard-fails at boot with this source.
The `engine`, `litellm_headers`, and `none` sources are wire-independent.

The source override is applied exactly once per invocation, at the turn boundary, on the
usage record that feeds `SessionStat`. That same record feeds the Prometheus counter, the
`ach-stats` row, and the `engine: summary` log. The engine-reported figure is never mixed
into a harness-computed total, and the override is not applied separately at each of
those sinks.

### Cost-source failure handling (A.5)

Cost failures are fail-soft: the invocation continues and the affected contribution is
`0.0`. Price-load failures are reported once at boot; response-level warnings are bounded
to at most one per condition per invocation.

| Condition | Cost result | Log behavior |
|---|---|---|
| `fetch_failed` while loading prices | Empty price cache; affected invocations contribute `0.0` | One boot error; never a boot hard-fail. |
| `no_entry` for the requested model | Model is unpriced; contribution `0.0` | One typed load result and boot warning naming the model. |
| `unpriced` base input/output price absent, null, or zero | Contribution `0.0`; no base price is synthesized | One typed load result and boot warning naming the model. |
| Cache-read or cache-creation price absent/null while both base prices are valid | That cache price falls back to `input_cost_per_token` | No failure; this is a successful partial-price fallback. |
| `malformed` price response or price value | Contribution `0.0` | One typed load result and boot warning naming the model. |
| Successful model response with no parseable usage | Contribution `0.0` | One `usage_missing` warning per invocation; non-success responses do not create a duplicate usage warning. |
| `litellm_headers` header absent or unparseable, or response is streaming | Contribution `0.0` | One `litellm_headers_unpriced` warning per invocation. |
| Usage cannot be attributed to an in-flight invocation | Never billed to a turn | One unattributed-usage warning per turn boundary. |

### Price endpoint (B.10)

For `litellm_usage`, the harness requests the model's prices from:

```text
GET /v2/model/info?model=<name>
x-ach-key: <ek_>
```

The full URL is `{capability.ach.baseUrl}/v2/model/info?model=<name>`, with
`model.name` URL-encoded and sent verbatim as the model identifier. The response is the
paginated model-info envelope (`current_page`, `data`, `size`, `total_count`,
`total_pages`); pricing is read from the matching entry in `data`. The `ek_` authenticates
this price request and is never persisted or logged. See the [cost-source evidence note](references/2026-07-25-cost-source.md)
for the reserved P0-v2 price-path output and B.7 streaming payload record.

### `engine.forwardEnv` — clean-slate env

opencode's subprocess env is built **clean-slate**: only a small base allowlist (`PATH`,
`SHELL`, `LANG`, …) plus the names you list in `forwardEnv` are forwarded from the harness
env. **Never list the `ek_`** (`ACH_TOKEN`/`ACH_API_KEY`) — it must never reach opencode.

## Channels

Each entry has `name`, `type`, an optional `concurrency` (per-channel cap, ≤ the global
`maxConcurrentInvocations`), an optional `prompt`, and the type's own sub-block.

| Type | Sub-block | Notes |
|------|-----------|-------|
| `webhook` | `webhook.auth` + `source` (`gitlab`\|`github`\|`generic`) | Auth `type`: `gitlab_token` \| `hmac` \| `header_token` \| `none`. `secretPath` is a file path, never a value; `header_token` also takes a `header` name. |
| `cron` | `cron.schedule` + `cron.timezone` | Cron expression + IANA tz. |
| `queue` | `queue` (`type: redis`, `key`, `ackMode: onComplete`) | Redis only in v1. |
| `a2a` | `a2a` (`mode: async`, `auth.header` + `auth.secretPath`) | Async only in v1. |

### `channel.prompt` templating

A channel's `prompt` is rendered with `{{ }}` substitution against the inbound event, so one
channel adapts per event:

```yaml
prompt: "Review {{ payload.object_attributes.url }} in {{ payload.project.path_with_namespace | default(\"this repo\") }}."
```

- Namespaces: `payload.*` (inbound JSON body) and `internal.*` (`channel.name`/`type`/`source`,
  `agent.name`, `memory.bank`, `event.id`, `session.key`). `header.*` is reserved.
- One filter: `{{ path | default("fallback") }}`. A missing token with no default renders empty.
- **No `env` namespace** — process env (the `ek_`) is structurally unreachable from a template.

## Full example

A complete, schema-valid contract showing every block lives at
[`example.yaml`](https://github.com/ackstorm/ach-agent/blob/main/example.yaml) in the repo root:

```yaml
schemaVersion: "1"

agent:
  name: gitlab-ackstorm

model:
  name: openai.gpt-5
  type: openai                          # openai | gemini | anthropic
  params:
    temperature: 1
    top_p: 0.95

cost:
  source: engine                        # engine (default) | litellm_usage | litellm_headers | none
                                        # omit cost, or source, to keep the engine default

capability:
  type: ach
  ach:
    baseUrl: https://ach.ackstorm.ai     # or supply via ACH_BASE_URL (env wins)
    environment: engineering-prod
  filter:
    exclude:
      tools: [gitlab_merge_merge_request]
      mcpServers: [dangerous-admin]
      skills: [send-email]

prompt:
  system:
    type: text
    text: "You are a senior code reviewer for the platform team."
  # ach form (preferred) — name a hydrated prompt; the harness resolves its file:
  # system:
  #   type: ach
  #   ach: <prompt-name>          # add `file: <subpath>` only if the prompt ships >1 file
  # file form — address a hydrated prompt file by path (relative to <home>/.ach-state):
  # system:
  #   type: file
  #   file: prompts/<prompt-name>/<file>.md
  compose: append

memory:
  endpoint: http://hindsight.engineering.svc:8080
  mission: "AI code reviewer for the platform team"
  bank: gitlab-pr-review
  mentalModels: [architecture, conventions, recurring-issues]

limits:
  maxConcurrentInvocations: 2
  maxInvocationSeconds: 1800
  maxQueuedTotal: 100
  idempotencyWindowSeconds: 3600
  maxSteps: 50
  terminalOutputRetries: 1

engine:
  home: /var/lib/ach-agent/home
  workDir: /workspace
  startupTimeoutSeconds: 30
  forwardEnv:
    - SSL_CERT_FILE
    - HTTPS_PROXY

persistence:
  enabled: true
  mountPath: /var/lib/ach-agent

health:
  host: 0.0.0.0
  port: 8000

channels:
  - name: gitlab-mr-review
    type: webhook
    source: gitlab                      # gitlab | github | generic
    concurrency: 4
    prompt: "Review this merge request: {{ payload.object_attributes.url }}"
    webhook:
      auth:
        type: gitlab_token              # gitlab_token | hmac | header_token | none
        secretPath: /etc/ach-agent/secrets/gitlab-webhook/secret

  - name: generic-hook
    type: webhook
    source: generic
    concurrency: 2
    prompt: 'Handle event {{ payload.id | default("?") }} via your tools.'
    webhook:
      auth:
        type: header_token
        header: X-Api-Key
        secretPath: /etc/ach-agent/secrets/generic-hook/secret

  - name: daily-security
    type: cron
    concurrency: 1
    cron:
      schedule: "0 8 * * 1-5"
      timezone: Europe/Madrid
    prompt: "Scan main for new CVEs; open an issue via your tools if any are critical."

  - name: ticket-triage
    type: queue
    concurrency: 2
    queue:
      type: redis
      key: ach:triage
      ackMode: onComplete
    prompt: "Triage this ticket and act via your tools."

  - name: peer-intake
    type: a2a
    concurrency: 2
    a2a:
      mode: async
      auth:
        header: x-a2a-custom-api-key
        secretPath: /etc/ach-agent/secrets/a2a/key
```
