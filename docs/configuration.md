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

## Blocks

| Block | Required | What |
|-------|----------|------|
| `schemaVersion` | ✓ | Must be the quoted string `"1"`. |
| `agent.name` | ✓ | The agent's name. |
| `model` | ✓ | `name` (ACH-served model id, verbatim), `type` (`openai`\|`gemini`\|`anthropic` — picks the compat wire), `params` (open dict, splatted to the client). |
| `capability` | ✓ | `type: ach`; `ach.baseUrl` / `ach.environment`; `filter.exclude` withholds `tools` / `mcpServers` / `skills` **before** the model sees them. |
| `prompt` | | `system` (inline persona, markdown ok). `compose` is contract-reserved (accepted; prompt-layering not yet executed by the harness). |
| `memory` | | Fail-open. `endpoint`, `bank` (static memory bank_id), `mentalModels`. `mission` is contract-reserved (accepted; not yet consumed). Backend down → run without it. |
| `limits` | | `maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`, `idempotencyWindowSeconds`, `maxSteps`, `terminalOutputRetries`. |
| `engine` | | Harness-local. `home`, `workDir`, `startupTimeoutSeconds`, `forwardEnv` (default-deny env allowlist — see below). |
| `persistence` | | `enabled` (false → in-memory dedup, no volume), `mountPath`. |
| `health` | | `host` / `port` for the HTTP surface (healthz/readyz/metrics + webhooks). |
| `channels` | | List of channel adapters (below). |

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
  system: "You are a senior code reviewer for the platform team."
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
