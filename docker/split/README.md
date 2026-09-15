# Three-container split example

See the [current split contract](../../docs/references/2026-09-14-three-role-split.md)
for responsibilities and lifecycle. Acceptance-only configuration and JSON fixtures
live under `tests/integration/fixtures/split/`; `scripts/test-split.sh` selects them.

This directory is a runnable contract example for the Phase 1 split. It keeps
the role boundaries explicit while leaving production rendering to
the ACH operator. Build the images with the targets below, then provide the
operator values as environment variables (the example never contains a token):

```sh
docker build --target harness -t ach-agent:harness -f Dockerfile .
docker build --target channels -t ach-agent:channels -f Dockerfile .
docker build --target engine-opencode -t ach-agent:engine-opencode -f Dockerfile .
ACH_TOKEN=ek_example ACH_BASE_URL=https://ach.example \
  docker compose -f docker/split/compose.yaml config --quiet
```

The Compose file has exactly three services. Channels and engine join the
harness network namespace (`network_mode: service:harness`), so only the
channels ingress is published. C binds its published ingress to `0.0.0.0:8080`
inside that shared namespace. H and E communicate over private `channel.sock`
and `agent.sock` endpoints. For Pi, switch H and E together so the selected
native driver agrees:

```sh
ACH_ENGINE_TARGET=engine-pi \
ACH_HARNESS_CONFIG_FILE=config-pi.yaml \
ACH_TOKEN=ek_example ACH_BASE_URL=https://ach.example \
docker compose -f docker/split/compose.yaml config --quiet
```

`config-pi.yaml` sets `engine.type: pi`; the harness derives the matching engine
configuration automatically.

The image without `--target` remains the combined native image. It carries both
Pi and OpenCode, keeps Git and SSH for preparation hooks, and preserves the
existing `--tui` and `--prompt` local launchers. The split E images use `/usr/bin/tini`
as PID 1; the mini-harness is its child and owns native engine descendants.

The harness is the only role that receives `/etc/ach-agent/config.yaml`. It owns
`/run/ach-agent/channels/channel.sock` and sends the public engine configuration
over `/run/ach-agent/engine/agent.sock`. Channels reads its source-only projection
with an unsigned client over the channel socket. Role containers wait up to 300
seconds for their socket and fail closed if it is unavailable.
The image creates both directories for UID 10001 for Docker named-volume
initialization. The Kubernetes pod uses fsGroup 10001 for writable `emptyDir`
mounts. Neither requires a hydration init container.

All three roles use the image entrypoint and select their role through `args:
["--role", "harness|channels|engine"]`. All roles expose ordinary HTTP health:
C on `0.0.0.0:8080`, H on `0.0.0.0:8090`, E on `0.0.0.0:8081`. H/E TCP
listeners expose only `/healthz` and `/readyz`; their APIs remain on Unix sockets.
Kubernetes uses `httpGet` probes directly. Docker Compose uses its required command
form to GET the same HTTP endpoints. No custom image probe command is needed.
Startup/readiness checks use `/readyz`; liveness uses `/healthz`. Startup allows
a 15 second initial delay and bounded retries. Both standalone and distributed
wait for hydration installation before readiness and track engine loss afterward.

The PVC-backed Pod example uses one claim with separate subpaths:

| Owner | Mount | Physical PVC subpath | Purpose |
| --- | --- | --- | --- |
| H | `/var/lib/ach-agent/state` | `state` | dedup `state.db` |
| H + E | `/var/lib/ach-agent/home/workspace` | `home/workspace` | prepared session workspaces |
| E | `/var/lib/ach-agent/home` | `home` | native home/session files and codemem DB |
| H + E | `/run/ach-agent/transfer` | separate `emptyDir` | temporary startup hydration batches |

The three data roots are rendered from one generic base. H sees `state` and
workspace; E sees `home` and workspace. Codemem is internal to E at
`<base>/home/state/codemem.db`. Hydration is copied from a temporary transfer
batch into E's home and the batch is deleted before readiness; no permanent
shared context mount exists. Channels receives only its source projection.

`config.yaml` uses the defaults derived from `persistence.mountPath`; operators
only render the generic base and the three data roots. Explicit engine paths
remain available for standalone deployments.

For an ephemeral deployment, use the concrete `compose-ephemeral.yaml` example
with `config-ephemeral.yaml`. H's persistence is
disabled, the generic base is `/tmp/ach-agent`; H state and E home are private
tmpfs paths and the shared workspace is mounted in both roles. Startup
hydration uses the temporary transfer mount and is copied into E's home. The
private `/tmp` data disappears when its container
exits; the shared named workspace volume remains until the operator removes it
(`docker compose ... down -v`). A Kubernetes renderer
can apply the same substitutions with three data volumes and the transfer
`emptyDir`. No generic renderer is supplied.

The deployment renderer owns volume and subpath setup. No init container or
download endpoint is part of this example. H hydrates requested context before
admitting the first invocation; E copies the temporary batch into its home and
reports readiness independently.
There is no additional filesystem readiness flag or socket handshake.

Legacy native data migration is agent-owned. Existing codemem state is copied
into E's private `home/state` during the bounded handoff, while H's state stays
in `state`; explicit distributed paths outside the role roots fail clearly.
Standalone deployments retain their explicit path compatibility.

`pod.yaml` references the full-config ConfigMap `ach-agent-config` and Secret
`ach-agent-secrets`. Production ACH must render those objects, images, generic
data roots, and secret references. Adding these
examples does not mutate a cluster or complete that separate repository handoff.

## Logs by role

- Channels logs inbound events and delivery.
- Harness logs hydration, prepare/cleanup, invocation lifecycle, final responses
  and usage summaries. Its proxy diagnostics describe upstream HTTP forwarding.
- Engine logs prompts, native model activity and tool actions/results. It still
  streams events to Harness for UI delivery and statistics; Harness does not log
  a second copy of each tool action.

Native model activity and proxy diagnostics describe different observations:
the engine sees the native agent's generation events; the harness sees upstream
HTTP status and transport failures. Neither requires forwarding log lines across
the control socket. In standalone mode these responsibilities stay the same,
with the local mini-harness child sharing the application's output destination.
