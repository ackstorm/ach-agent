# ACH operator handoff: standalone and distributed deployment

Status: operator implementation contract, 2026-09-14. The ach-agent startup paths
described here are implemented and merged into local `main`. The ACH operator CR
and renderer changes are work for the ACH repository; they are not implemented by
this document. No image publication or cluster rollout is claimed.

This document is self-contained. ACH does not need access to ach-agent source code
to implement the rendering contract.

## One operator decision

Support two placement modes:

| Mode | Rendered workload | Startup |
| --- | --- | --- |
| `standalone` | One Deployment, one replica, one pod with one `agent` container | Combined image, no role arguments |
| `distributed` | One Deployment, one replica, one pod with three ordinary containers | Same combined image, arguments selecting `channels`, `harness`, `engine` |

`distributed` means separation between containers within the same pod. It does not
mean three pods, multiple Deployments, independent scaling or a distributed queue.

Add one operator-owned mode selector. Proposed CR spelling:

```yaml
spec:
  runtime:
    mode: standalone  # standalone | distributed
```

The spelling is a proposal for the ACH CR, not an existing ach-agent schema field.
If ACH already has a placement selector, reuse it rather than adding a second one.
Default omitted mode to `standalone` to preserve existing single-container
deployments. Reject unknown values. Use the existing image, configuration,
environment and persistence inputs; no new engine environment section is needed.

The mode must not be inserted into the agent configuration or passed as a new
`--mode` flag. The operator implements it by choosing the container list and args.
Both modes run the same agent configuration and preserve channel, session and
workspace semantics. The frozen agent configuration schema is unchanged from v0.16.1.

Use `replicas: 1` and a replacement strategy that avoids overlapping active pods
(`Recreate`). A mode change replaces the pod; it is not a live migration of work.
In-memory accepted work/results may be lost during replacement. Preserve configured
persistent storage across that replacement.

## Image and commands

Supply one versioned combined ach-agent image containing both supported native
engines. Reuse the same image/digest for all three containers in distributed mode.
Role-specific build images exist, but selecting several different images is not
required of the operator for this contract.

The image entrypoint is already:

```text
/usr/bin/tini -- python -m ach_agent.main
```

Do not override `command`. Render args as follows:

| Container | Args |
| --- | --- |
| Standalone `agent` | Omit args, or `[]` |
| Distributed `channels` | `["--role", "channels"]` |
| Distributed `harness` | `["--role", "harness"]` |
| Distributed `engine` | `["--role", "engine"]` |

`tini` is a small PID 1 process already in the image. It forwards termination
signals and reaps orphan children. It is not a Kubernetes init container, and ACH
does not install or configure it separately.

In standalone mode channels and harness run together and the launcher starts the
mini-harness as a child process. One container does not mean one OS process. There
is no container boundary protecting harness files or credentials from the native
engine in this mode. Do not automatically select a mode based on whether a secret
appears in a config; placement is the operator/user's explicit choice.

## Configuration and environment

Mount the full configuration at `/etc/ach-agent/config.yaml` only in standalone
`agent` or distributed `harness`. The combined image already uses that path.
If the existing operator uses another path, retain `ACH_CONFIG_PATH` there.
Use a Secret-backed volume when the rendered configuration contains secret values.

| Container | Environment supplied by ACH |
| --- | --- |
| Standalone `agent` | Existing combined channel/source and harness environment |
| Distributed `channels` | Source-side credentials and settings: webhook authentication, queue connection/authentication, inbound A2A settings, as required by configured sources |
| Distributed `harness` | Existing `ACH_BASE_URL`, `ACH_TOKEN`, hook/MCP/memory credentials and settings, plus explicitly selected values for native `engine.forwardEnv` |
| Distributed `engine` | No operator-injected `env` or `envFrom` by default |

This does not remove the image's own PATH or other image defaults. It means ACH
does not copy the common agent secret environment into the engine container.
Common non-secret metadata may be placed on C/H when needed; E requires none for
bootstrap. If one credential is explicitly required by both a source and a harness
hook, provide it to those two roles, never indiscriminately to all containers.

Preserve the existing environment/Secret-reference inputs that ACH already owns.
The table defines their destinations, not three newly required CR environment
blocks. The renderer need not understand Git scripts or engine-native protocols.

`engine.forwardEnv` continues to select names. H resolves the eligible values and
sends them to the mini-harness; E applies them to native children. For example,
`DEBUG` selected for the native engine is supplied to H and forwarded, not copied
by the operator into E. Managed credentials remain excluded by the existing
forwarding policy. Explicitly forwarded custom secrets are deliberate exposure.

H hydrates configuration, starts proxies and provides source-only inputs to C and
public launch inputs to E. ACH must not generate `channels.json`, `engine.json`,
`opencode.json`, bootstrap files, HMAC keys or internal control URLs. Default
socket paths/listeners are application responsibilities, not required EnvVars.

## Distributed mounts

Use two separate IPC directory volumes (`emptyDir` is sufficient):

| Volume directory | H mount | C mount | E mount |
| --- | --- | --- | --- |
| `/run/ach-agent/channels` containing `channel.sock` | Read/write, socket owner | Read-only | Absent |
| `/run/ach-agent/engine` containing `agent.sock` | Read-only | Absent | Read/write, socket owner |

Mount directories, not individual socket files: the owning process creates and
replaces the socket. Use UID/GID 10001 and pod `fsGroup: 10001`; socket mode is
0600. Do not combine both directories into a volume visible to every role.

Keep the existing logical state/home/workspace paths, with these mount owners:

| Data | Mount owners |
| --- | --- |
| Full configuration, harness state/dedup/session metadata | H only |
| Engine HOME, native conversation map and native tool state | E only |
| Workspace | H and E, read/write at the same absolute path |
| Public hydrated context | H read/write, E read-only |
| `/tmp` | Separate writable private volume for each container |

C needs only its IPC directory and private temporary storage. Do not mount the
whole state PVC into all containers. Separate subpaths on the existing PVC are
sufficient; separate PVCs are not required.

For the concrete persistent layout used by the agent examples:

| Absolute path | Owner | PVC subpath |
| --- | --- | --- |
| `/var/lib/ach-agent/state` | H | `state` |
| `/var/lib/ach-agent/home` | E | `home` |
| `/var/lib/ach-agent/workspace` | H + E | `workspace` |
| `/var/lib/ach-agent/public-context` | H write, E read | `public-context` |
| `/var/lib/ach-agent/state` | E | `engine-codemem` |

The two `/state` paths have different backing subpaths. Preserve custom configured
paths; in particular a workspace nested beneath engine HOME is still a separate
H/E mount, not permission to mount the entire HOME into H. ACH must ensure PVC
subdirectories exist through its existing volume provisioning mechanism.

For nonpersistent deployments, provide the same sharing/ownership with `emptyDir`.
Existing application defaults are H state `/tmp/ach-harness-state`, public context
`/tmp/ach-public-context`, E home `/tmp/ach-home`, and workspace beneath E home
unless `engine.workDir` overrides it. Mount shared workspace/context explicitly
at the same paths on H/E; do not share all of `/tmp`.

Standalone mounts the required existing storage into its single container. It
needs writable temporary storage but no cross-container IPC volumes. The launcher
owns its local socket directory and child lifecycle.

## Networking, probes and lifecycle

All distributed containers share the normal pod network namespace. Existing model,
MCP and OpenCode loopback HTTP connections continue unchanged. No internal Service,
control TCP port, network bridge, RBAC or service-account token is needed.

Expose public ingress through one Kubernetes Service:

- Standalone: the `agent` container, existing `health.host`/`health.port`, default
  `0.0.0.0:8080`.
- Distributed: `channels`, default `0.0.0.0:8080`.

Use HTTP `/healthz` for startup/liveness and `/readyz` for readiness on that public
endpoint. H and E use exec probes over their respective Unix sockets. The probe
program below is for H readiness; substitute E's socket path for E or `/healthz`
for startup/liveness:

```python
import httpx
with httpx.Client(
    transport=httpx.HTTPTransport(uds="/run/ach-agent/channels/channel.sock"),
    base_url="http://ach-internal", timeout=2,
) as client:
    client.get("/readyz").raise_for_status()
```

Allow hydration startup time (the examples allow up to 300 seconds); exec probe
timeout must exceed its two-second client timeout, e.g. `timeoutSeconds: 3`.
Containers may start concurrently and retry while peers initialize. An unavailable
native Pi/OpenCode process can fail one invocation; an unhealthy engine container
makes the pod unavailable. Preserve an appropriate graceful termination allowance
for configured hooks and native shutdown.

Set `automountServiceAccountToken: false`, do not share PID namespaces, and retain
non-root/restricted security contexts, private writable `/tmp`, and curated mounts.
No privileged container or runtime socket is required. Shared/open egress remains
the declared profile; this contract does not add per-engine egress filtering.

## Workspace and upgrade behavior

Prepare and cleanup execute in H, with a temporary H-private HOME. They retain
access to the shared workspace, existing cwd and teardown timing. ACH does not
interpret their commands, inspect Git configuration or perform artifact handoff.
The engine's workspace remains untrusted input to credential-bearing scripts.

Retain existing state and native files when changing placement. The agent imports
legacy conversation mappings from H's `state/state.db` to E's native map; ACH does
not implement that migration or reset sessions. If an existing codemem database
shares H's physical state directory, stop the old agent and coherently relocate
that database and any present WAL/SHM files into the E-only backing directory
before using the distributed mount layout. Do not silently start an empty database.

## Acceptance for the ACH renderer

- Omitted mode/`standalone`: one container, combined image, no role args, existing
  config/env/storage and public probes.
- `distributed`: exactly three containers in one pod, same image, correct role
  args, C/H env placement, and no E `env`/`envFrom` or private config mount.
- Two correctly scoped socket mounts, shared workspace, private H state/E home,
  and H-write/E-read public context. Service reaches only public ingress.
- Switch modes by replacing the pod without two active replicas; preserve data.
- Run a real configured channel event and second turn, including session reuse,
  a selected forwarded env value, H-side prepare and shutdown.

Queue-backed separate Deployments, scale-to-zero, autoscaling, S3 session artifacts
and further proxy content protection remain separate work. Neither mode needs them.
