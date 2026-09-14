# Three-container split example

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
["--role", "harness|channels|engine"]`. H and E health checks use HTTP over
their Unix sockets; C remains on public HTTP `0.0.0.0:8080`. Health checks allow
a 300 second startup period.

The PVC-backed Pod example uses one claim with separate subpaths:

| Owner | Mount | Physical PVC subpath | Purpose |
| --- | --- | --- | --- |
| H | `/var/lib/ach-agent/state` | `state` | dedup `state.db` (existing path) |
| H + E | `/var/lib/ach-agent/workspace` | `workspace` | prepared session workspaces |
| H | `/var/lib/ach-agent/public-context` | `public-context` | hydrated public prompts/artifacts |
| E | `/var/lib/ach-agent/home` | `home` | native home/session files (existing path) |
| E | `/var/lib/ach-agent/state` | `engine-codemem` | codemem DB and SQLite siblings |

The two `/state` mounts deliberately have different physical sources. The
logical codemem path remains `<mountPath>/state/codemem.db`, while E cannot see
H's `<mountPath>/state/state.db`. Channels receives only its source projection;
it has no state, workspace, home, public-context, or PVC mounts.

`config.yaml` is also a concrete custom path example: H and E use the
role-owned `/var/lib/ach-agent/home` and `/var/lib/ach-agent/workspace` paths,
while codemem uses E's separate `/var/lib/ach-agent/state` mount. If an
operator chooses another `engine.home` or `engine.workDir`, mount those exact
paths in E and the shared workspace in H/E.

For an ephemeral deployment, use the concrete `compose-ephemeral.yaml` example
with `config-ephemeral.yaml`. H's persistence is
disabled, H state is `/tmp/ach-harness-state`, E home/codemem are under
`/tmp/ach-home`, and the shared workspace/public-context volumes are mounted
at those same `/tmp` paths in both roles. This keeps H's hydrated public
context visible to E. The private `/tmp` data disappears when its container
exits; the shared named workspace and public-context volumes remain until the
operator removes them (`docker compose ... down -v`). A Kubernetes renderer
can apply the same explicit substitutions:
PVC-backed `state`, `home`, `engine-codemem`, `workspace`, and
`public-context` become five `emptyDir` volumes. No generic renderer is supplied.

The operator must create the PVC subdirectories before using `subPath` mounts
(or use its equivalent volume renderer), including `state`, `home`,
`engine-codemem`, `workspace`, and `public-context`. No init
container or download endpoint is part of this example. H hydrates public
context before admitting the first invocation; E creates its own home/workspace
links after receiving its public configuration over the socket and reports
readiness independently.
There is no additional filesystem readiness flag or socket handshake.

## Existing codemem data relocation

Before changing a persistent deployment, stop the old H/E pair and make an
offline backup of the old `<mountPath>/state/codemem.db`; copy its `-wal` and
`-shm` siblings too when they are present. While the old process is stopped,
create the PVC's `engine-codemem` directory and copy the database and any
present siblings as one coherent set, preserving ownership and permissions.
Run `sqlite3 <new>/codemem.db 'PRAGMA integrity_check;'` (or the equivalent
codemem SQLite check) before starting E. Leave H's `state/state.db` in `state`.

Do not deploy with an empty `engine-codemem` directory while expecting old
memory to appear: codemem can create a fresh database, which would silently
discard the old history. A clean closed database may have no WAL/SHM files;
their absence is valid for a fresh or cleanly checkpointed database. If an
existing database is present with a WAL/SHM set, copy the present set
coherently; if the backup is incomplete, stop the rollout and restore it. No
automatic migration or reset is performed by these manifests.

`pod.yaml` references the full-config ConfigMap `ach-agent-config` and Secret
`ach-agent-secrets`. Production ACH must render those objects, images, PVC,
custom `engine.home`/`workDir` mount maps, and secret references. Adding these
examples does not mutate a cluster or complete that separate repository handoff.
