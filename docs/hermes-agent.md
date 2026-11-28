# HermesAgent (experimental)

A `HermesAgent` runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a long-running agent:
`hermes gateway run` in one OpenShell sandbox that the gateway restarts when it exits. Hermes keeps its own
scheduler, memory, learned skills and messaging platforms; the ach-agent runtime (channels, journal, engines) is not
involved. Its model, MCP servers, skills and instructions come from its ACH Environment, as an Agent's do.

| Piece | What it is |
|---|---|
| Sandbox | `spec.sandbox.openshell.image` (for example `nousresearch/hermes-agent:v0.23.4`), started without the image's `/init`, as uid 10001 |
| PVC | `spec.persistence.volume`, mounted at `/sandbox/ach`; Hermes' home is `/sandbox/ach/hermes`. Use block storage: Hermes keeps SQLite databases there |
| Credential proxy | StatefulSet `hermes-<name>` running `ach-runtime credential-proxy` from `spec.proxy.image` as `spec.serviceAccountName`; it holds the ACH credential from `spec.ach.keyFrom` (a KeyGrant names `<namespace>/<serviceAccountName>` for `tokenExchange`) and forwards Hermes' model and MCP calls to ACH |
| Secrets | every key of `spec.secretRef` reaches Hermes as an environment variable (`TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN`, ...) |

```yaml
spec:
  ach:
    baseUrl: https://api.ackstorm.ai
    environment: agent-hermes
    model: {name: ackstorm.smart}
    keyFrom: {tokenExchange: {identity: ai.run@ackstorm.com}}
  serviceAccountName: ach-agent-control
  proxy: {image: ghcr.io/ackstorm/ach-agent-control:<version>}
  secretRef: {name: hermes-telegram}
  sandbox:
    openshell:
      image: nousresearch/hermes-agent:v0.23.4
      policy:
        network_policies:
          telegram:
            name: telegram
            endpoints: [{host: api.telegram.org, port: 443, tls: NETWORK_TLS_MODE_SKIP}]
            binaries: [{path: "/**"}]
  persistence:
    volume: {size: 5Gi}
```

## Hydration

At every sandbox start Hermes downloads its `config.yaml`, `ach-skills/` and `.ach/` from the proxy, which
hydrates the Environment then, and runs `.ach/start.py` before the gateway. The keys ACH writes are ACH's and are rewritten every start; what `spec.config` adds beside them
is kept:

| From the Environment | In Hermes |
|---|---|
| the model `spec.ach.model.name` | `model` (provider `custom`, through the proxy; `context_length` from the model's metadata) |
| `mcpServers` | one `mcp_servers` entry each, through the proxy |
| `a2aAgents` | one `mcp_servers` entry each, at ACH's `/a2a/<agent>/mcp` (a `send_message` tool), through the proxy |
| `skills`, `plugins` (their skills only) | `skills.external_dirs` → `$HERMES_HOME/ach-skills`; skills Hermes writes itself stay in its home |
| `instructions` | a block in `SOUL.md`, one section per instruction by name |

An Environment change reaches Hermes at its next start. A change to `spec` or to a value of `spec.secretRef`
replaces the sandbox (checked every 30 s); the PVC stays. Deleting the HermesAgent deletes the sandbox and the PVC.

Hermes has no list of instruction files like opencode's. `SOUL.md` is the only context file it loads for both chat
and cron turns and trusts (a prompt-injection match warns instead of dropping the file), so the instructions go there,
between `<!-- ach:instructions -->` markers. The block is rewritten every start; the rest of `SOUL.md` (Hermes'
default text, or yours) is kept. On a fresh PVC with instructions, `start.py` seeds Hermes' default text first.

Messaging platforms need an egress rule in `spec.sandbox.openshell.policy`, with `tls: NETWORK_TLS_MODE_SKIP`
(Hermes does not trust the supervisor's re-signing CA), as in the example above.

Cost: Hermes sends about 40k prompt tokens per turn, and its cron jobs run turns on their own. Its spend is capped
only by the Environment's budget.
