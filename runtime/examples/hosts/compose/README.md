# Reference single-unit host: Docker Compose

> **Not for production.** This runs the whole agent in one process (the `agent` role). Agent tools
> share that process's credentials. Production always runs split, on Kubernetes, under the ACH
> operator (contract v2 D5). Use this on a laptop, a test or pre-production machine, or in CI.

Services: `s3` (SeaweedFS, named volume), `s3-init` (creates bucket `ach`), `ach-stub` (an
ACH-compatible stand-in, contract 3.3.1) and `agent` (the runtime `agent` image).

## Quickstart

    docker compose -f examples/hosts/compose/compose.yaml up --build -d
    curl -i -X POST http://localhost:8080/channels/tasks/events \
      -H 'x-webhook-token: dev-webhook-token' -H 'x-request-id: demo-1' \
      -H 'content-type: application/json' \
      -d '{"session": "demo", "task": "Say hello"}'

The webhook token defaults to `dev-webhook-token`; set `WEBHOOK_TOKEN` in the environment of
`docker compose up` to change it.

A webhook answers `202` when the task is admitted; the run completes later. Follow it with

    docker compose -f examples/hosts/compose/compose.yaml logs -f agent

and look for the line `"message": "invocation completed"` with `"outcome": "completed"`.
The same `session` value keeps one conversation. A Workspace is saved to S3 only after it goes idle
(session idle 3 s, then Workspace idle 6 s in this `config.json`); shutdown saves nothing. Wait that
long before `docker compose down` (without `-v`) and the conversation is restored after `up`.
Real deployments should raise these timeouts.

## Wiring a model

The runtime needs a model in four places that must agree:

1. `config.json` `model.name` (`local-model`) and `model.type` (`openai`).
2. `stub/catalog.json` `runtime.models[]`: an entry with the same `id` and a non-empty `endpoint`.
   Hydration rejects an empty catalog and a missing model.
3. `MODEL_UPSTREAM_URL`: the base URL of an OpenAI-compatible server, for example Ollama
   `http://host.docker.internal:11434` or a LiteLLM proxy. The stub forwards `/v1/*` there unchanged.
4. `MODEL_API_KEY` (optional): sent to the upstream as `Authorization: Bearer`. The model name your
   upstream knows must be what the engine requests, so set `model.name` and the catalog `id` to it.

    MODEL_UPSTREAM_URL=http://host.docker.internal:11434 \
      docker compose -f examples/hosts/compose/compose.yaml up --build -d

## After editing `config.json`

`configVersion` is a hash of the file and must be recomputed. In the runtime image:

    docker compose -f examples/hosts/compose/compose.yaml run --rm --no-deps --entrypoint python agent \
      -c "import json; from ach_runtime.config.canonical import compute_config_version as c; print(c(json.load(open('/etc/ach-runtime/config.json'))))"

Paste the printed value into `configVersion`, then restart `agent`.

## Shared machines

The agent port is published on loopback only. One compose project per developer, each with its own host port:

    AGENT_PORT=8091 docker compose -p alice -f examples/hosts/compose/compose.yaml up -d

## Running from source (without the image)

`ach_runtime.__version__` is read from installed package metadata, so install the package into a
virtualenv first:

    python -m venv .venv && . .venv/bin/activate && pip install -e .
    python -m ach_runtime agent --config examples/hosts/compose/config.json --data-dir ./.ach-data

`--data-dir` must be writable. The facade listens on `127.0.0.1:8081` and that port is fixed.
