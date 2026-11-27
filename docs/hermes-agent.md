# HermesAgent (experimental)

A `HermesAgent` runs [Hermes Agent](https://github.com/NousResearch/hermes-agent) as a long-running agent:
`hermes gateway run` in one OpenShell sandbox that the gateway restarts when it exits. Hermes keeps its own
scheduler, memory, learned skills and messaging platforms; the ach-agent runtime (channels, journal, engines) is not
involved.

| Piece | What it is |
|---|---|
| Sandbox | `spec.sandbox.openshell.image` (for example `nousresearch/hermes-agent:v0.23.4`), started without the image's `/init`, as uid 10001 |
| PVC | `spec.persistence.volume`, mounted at `/sandbox/ach`; Hermes' home is `/sandbox/ach/hermes`. Use block storage: Hermes keeps SQLite databases there |
| Credential proxy | StatefulSet `hermes-<name>` running `ach-runtime credential-proxy` from `spec.proxy.image` as `spec.serviceAccountName`; it holds the ACH credential from `spec.ach.keyFrom` (a KeyGrant names `<namespace>/<serviceAccountName>` for `tokenExchange`) |
| Config | `spec.config` is Hermes' `config.yaml`, written at every sandbox start: edits Hermes makes to it do not survive a restart |
| Secrets | every key of `spec.secretRef` reaches Hermes as an environment variable (`TELEGRAM_BOT_TOKEN`, `SLACK_BOT_TOKEN`, ...) |

The sandbox reaches ACH only through the proxy. The config refers to it with two variables Hermes expands:

```yaml
config:
  model:
    provider: custom
    base_url: ${ACH_PROXY_URL}/v1
    api_key: ${ACH_PROXY_TOKEN}
    default: ackstorm.smart
    context_length: 200000   # not discovered for ACH aliases
  mcp_servers:
    serper:
      url: ${ACH_PROXY_URL}/mcp/mcp-serper
      headers: {Authorization: "Bearer ${ACH_PROXY_TOKEN}"}
```

Messaging platforms need an egress rule in `spec.sandbox.openshell.policy`, with `tls: NETWORK_TLS_MODE_SKIP`
(Hermes does not trust the supervisor's re-signing CA):

```yaml
network_policies:
  telegram:
    name: telegram
    endpoints: [{host: api.telegram.org, port: 443, tls: NETWORK_TLS_MODE_SKIP}]
    binaries: [{path: "/**"}]
```

A change to `spec` or to a value of `spec.secretRef` replaces the sandbox (checked every 30 s); the PVC stays.
Deleting the HermesAgent deletes the sandbox and the PVC.

Cost: Hermes sends about 40k prompt tokens per turn, and its cron jobs run turns on their own. Its spend is capped
only by the Environment's budget.
