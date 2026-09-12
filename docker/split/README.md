# Three-container split example

This directory is a runnable contract example for the Phase 1 split. It keeps
the role boundaries explicit while leaving production rendering to
`ach-runtime`. Build the images with the targets below, then provide the
operator values as environment variables (the example never contains a token):

```sh
docker build --target harness -t ach-agent:harness -f Dockerfile .
docker build --target channels -t ach-agent:channels -f Dockerfile .
docker build --target engine-opencode -t ach-agent:engine-opencode -f Dockerfile .
ACH_TOKEN=ek_example ACH_BASE_URL=https://ach.example \
  ACH_CHANNELS_HMAC_KEY=example docker compose -f docker/split/compose.yaml config --quiet
```

The Compose file has exactly three services. Channels and engine join the
harness network namespace (`network_mode: service:harness`), so only the
channels ingress is published. H listens on `127.0.0.1:8090`, E on
`127.0.0.1:8081`, and C binds its published ingress to `0.0.0.0:8080` inside
that shared namespace. Set `ACH_ENGINE_TARGET=engine-pi` together with an
engine artifact whose `engineType` is `pi` for the Pi image.

The image without `--target` remains the combined native image. It carries both
Pi and OpenCode, keeps Git and SSH for preparation hooks, and preserves the
existing `--tui` and `--prompt` local launchers. The split E images use `/usr/bin/tini`
as PID 1; the mini-harness is its child and owns native engine descendants.

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
H's `<mountPath>/state/state.db`. Channels receives only `channels.json`; it
has no state, workspace, home, public-context, or PVC mounts.

For an ephemeral deployment, replace each PVC volume in `pod.yaml` with an
`emptyDir: {}` volume of the same name and set `persistence.enabled: false` in
the full H config plus matching `/tmp` paths in `engine.json`. The five role
mount paths and their read/write directions stay unchanged. This is suitable
for a disposable test pod only.

The operator must create the PVC subdirectories before using `subPath` mounts
(or use its equivalent volume renderer), including `state`, `home`,
`engine-codemem`, `workspace`, and `public-context`. No init
container or download endpoint is part of this example. H may create files
inside those directories at startup; E retries its HTTP readiness until H's
public context and workspace layout are available.

## Existing codemem data relocation

Before changing a persistent deployment, stop the old H/E pair and make an
offline backup of the old `<mountPath>/state/codemem.db` together with its
`-wal` and `-shm` siblings. While the old process is stopped, create the
PVC's `engine-codemem` directory and copy all three files into it as one set,
preserving ownership and permissions. Run `sqlite3 <new>/codemem.db
'PRAGMA integrity_check;'` (or the equivalent codemem SQLite check) before
starting E. Leave H's `state/state.db` in `state`.

Do not deploy with an empty `engine-codemem` directory while expecting old
memory to appear: codemem can create a fresh database, which would silently
discard the old history. If the old database is absent or its WAL/SHM set is
incomplete, stop the rollout and restore the backup or complete the offline
copy first. No automatic migration or reset is performed by these manifests.

`pod.yaml` references ConfigMaps `ach-agent-config`, `ach-agent-channels`, and
`ach-agent-engine`, and Secret `ach-agent-secrets`. Production `ach-runtime`
must render those objects, images, PVC, custom `engine.home`/`workDir` mount
maps, and secret references. Adding these examples does not mutate a cluster
or complete that separate repository handoff.
