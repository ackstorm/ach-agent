# Getting started

Launch an `ach-agent` on your machine with Docker — a config file and two environment
variables. No host Python.

## What you need

- Docker
- An **EK** (`ek-…`) for ACH — passed as `ACH_TOKEN`, never baked into an image or logged
- Your ACH endpoint — passed as `ACH_BASE_URL`

A ready-to-run example lives in the sibling repo **`../ach-agent-test`** (`config.yaml` +
`docker-compose.yaml`); the files below are the same shape.

## 1. A minimal `config.yaml`

```yaml
schemaVersion: "1"

agent:
  name: local-dev-agent

model:
  name: gemini.gemini-flash-latest   # an ACH-served model id
  type: openai                       # the ACH compat wire (gemini.* is served on /v1)

capability:
  type: ach
  ach:
    baseUrl: https://ach.ackstorm.ai   # or leave empty and set ACH_BASE_URL

prompt:
  system: "You are a concise software-engineering assistant."
  compose: append

engine:
  home: /tmp/ach-home
  workDir: /tmp/ach-home/workspace

persistence:
  enabled: false
  mountPath: /tmp/ach-state

channels: []                          # none under --tui/--prompt: the line you type IS the prompt
```

See [Configuration](configuration.md) for every block and the full `example.yaml`.

## 2. A `docker-compose.yaml`

```yaml
services:
  agent:
    image: ghcr.io/ackstorm/ach-agent:latest
    command: ["--tui"]                # console REPL
    environment:
      - ACH_TOKEN=${ACH_TOKEN:?set ACH_TOKEN=ek-... in your shell}
      - ACH_BASE_URL=${ACH_BASE_URL:?set ACH_BASE_URL=https://your-ach-host in your shell}
      - ACH_CONFIG_PATH=/etc/ach-agent/config.yaml
    volumes:
      - ./config.yaml:/etc/ach-agent/config.yaml:ro
    stdin_open: true
    tty: true
```

## 3. Run it

```bash
# interactive console — the line you type is the prompt, Ctrl-D exits
ACH_TOKEN=ek-<your-key> ACH_BASE_URL=https://<your-ach-host> docker compose run --rm agent

# one-shot instead of the REPL
ACH_TOKEN=ek-<your-key> ACH_BASE_URL=https://<your-ach-host> \
  docker compose run --rm agent --prompt "Review MR !42 in project 7"
```

To drive **real channels** (webhook / cron / queue / a2a) instead of the console, add a
`channels:` block to `config.yaml` and launch **without** `--tui`/`--prompt` — the harness then
serves its HTTP surface and runs the configured channels. See [Configuration](configuration.md).

## Launch modifiers

| Flag | Behavior |
|------|----------|
| `--tui` | Attach opencode's native TUI against the prewarmed engine (interactive). |
| `--prompt "…"` | Run one prompt, print the reply, exit. |
| `--debug` | Plain stdin/stdout REPL (no native TUI). |
| _(none)_ | Serve the HTTP surface and run the configured `channels`. |
