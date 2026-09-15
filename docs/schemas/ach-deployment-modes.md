# ACH operator handoff: distributed placement

This supersedes the previous storage proposal. ACH renders placement, environment
and generic storage. ACH Agent owns the contents, native tool paths and compatibility.
Do not add renderer branches for codemem, OpenCode, Pi, skills or hydration artifacts.

Runtime version for this storage contract: `ghcr.io/ackstorm/ach-agent:v0.16.5`.
Use the matching operator mounts below. The corrected existing-PVC transition
still needs joint Kubernetes e2e; local evidence is recorded in the
[stable workspace validation report](../reports/2026-09-15-stable-workspace-validation.md).

## Placement and image

Placement is operator configuration: `AgentProfile.spec.achagent.placement` and
`ACHAgent.spec.placement`, with values `standalone | distributed` and default
`standalone`, as reported by the ACH operator team. The operator owns resolution
between the two fields. Never render placement into Agent's `config.json`.

- Standalone: existing Deployment with one container and no role arguments.
- Distributed: one Deployment, one replica, three ordinary containers in one pod.
- Same combined image for all containers. Preserve the image entrypoint (tini is
  already included); set only `args: ["--role", "channels"]` (or `harness`, `engine`).
- Keep Recreate, the existing scheduling/resource settings and public Service.

## Environment and configuration

For this increment Channels and Harness receive the same existing operator-supplied
environment. Both are trusted components. Engine receives only the environment
entries explicitly selected by `engine.forwardEnv`, resolved by the operator from
that same environment. Do not copy unrestricted `envFrom` into Engine.

Mount the complete configuration only in Harness and retain
`ACH_CONFIG_PATH=/etc/ach-agent/config.json` there when using that path.
Channels obtains its source configuration from Harness. Engine obtains only its
public launch inputs.

Keep the existing `engine.forwardEnv` configuration field. The mini-harness passes
the selected environment to native children. ACH does not generate native
configuration or bootstrap files.

## Distributed data mounts

Resolve one base path:

```text
persistent:     base = persistence.mountPath
nonpersistent:  base = /tmp/ach-agent
```

Mount exactly these generic data directories:

| Directory | Harness | Engine | Channels |
| --- | --- | --- | --- |
| `base/state` | Read/write | Absent | Absent |
| `base/home` | Absent | Read/write | Absent |
| `base/home/workspace` | Read/write | Read/write through `base/home` | Absent |

Use these exact subpaths on the existing PVC when persistent: `state`, `home`, and
`home/workspace`. Harness mounts `home/workspace`; Engine mounts `home`, with the
same `home/workspace` subpath visible at its historical absolute path. For
temporary deployments, `emptyDir` backing with the same three-directory ownership
is sufficient.
Do not expose the entire base volume to every container. Each container also gets
its own writable `/tmp`.

These directories are opaque to ACH. ACH Agent places native HOME, sessions, tool
databases and workspaces within them. The renderer must not derive
extra mounts from `engine.home`, `engine.workDir`, memory settings or the selected
engine. Standalone keeps its existing storage/configuration behavior.

Additionally mount one separate `emptyDir` at `/run/ach-agent/transfer`, read/write
in Harness and Engine, absent in Channels. This is temporary startup handoff
storage, including when persistence is enabled. ACH Agent creates and removes its
own download batches there; the operator does not manage their contents.

## IPC and networking

Keep two separate IPC directories:

| Mount | Harness | Engine | Channels |
| --- | --- | --- | --- |
| `/run/ach-agent/channels` | Read/write | Absent | Read-only |
| `/run/ach-agent/engine` | Read-only | Read/write | Absent |

Mount directories, not individual socket files. Use UID/GID 10001 and
`fsGroup: 10001`; verify fresh volume/subpath writability in e2e.

The pod shares its normal network namespace. Public ingress/health port 8080 lives
on Channels in distributed mode. No internal Services, HMAC keys,
service-account token or extra RBAC.
Retain restricted security contexts and separate PID namespaces.

Recommended pod setting: `enableServiceLinks: false`, to suppress automatic
Service environment variables. Explicit environment and DNS-based Service access
remain the intended discovery mechanisms; this is not a network isolation control.
See [Kubernetes Service discovery](https://kubernetes.io/docs/concepts/services-networking/service/#environment-variables).

## Probes

Use ordinary Kubernetes `httpGet` probes for every container. No exec probe,
image healthcheck command or Unix-socket client is required.

| Role | HTTP port |
| --- | --- |
| Channels | 8080 |
| Harness | 8090 |
| Engine | 8081 |

ACH Agent binds the health listeners to `0.0.0.0`. The new Harness/Engine TCP
listeners expose health endpoints only; application traffic keeps using Unix
sockets. No additional Service or operator-injected environment is needed.

Use `/readyz` for startup/readiness and `/healthz` for liveness:

| Probe | Initial delay | Period | Timeout | Failure threshold |
| --- | --- | --- | --- | --- |
| Startup | 15s | 5s | 3s | 6 |
| Readiness | 0s | 10s | 3s | 3 |
| Liveness | 0s | 20s | 3s | 3 |

Standalone retains its existing configured HTTP port and the same endpoints.
Readiness has the same semantics in both placements: initialization must finish,
including hydration copy, native configuration and directory preparation, before
readiness succeeds. This happens at startup, without waiting for an event. Harness
and Channels readiness reflects required downstream readiness, including engine
loss after startup. ACH Agent owns this logic; the operator only renders probes.
Profile resources apply to each container; document the resulting total pod requests.

## Responsibility and acceptance

ACH Agent is responsible for internal path resolution and retaining existing
native data during supported upgrades. Do not implement tool-specific migration,
Git preparation or file copying in the operator.

The stable workspace path requires a coordinated image and mount rollout.
`v0.16.4` used the old distributed default (`base/workspace`) and is not
compatible with the corrected persistent mount contract. Roll out an image that
uses `base/home/workspace` together with the operator's `state`, `home`, and
`home/workspace` subpaths; do not pair the corrected mounts with an older image.
The original placement failure and its evidence remain in the
[live transition report](../reports/2026-09-15-pvc-placement-transition.md).

Validate standalone output unchanged; distributed container/env/mount shape;
persistent and temporary startup; one real channel turn with Harness-side prepare;
a second turn reusing its session; and Engine unable to read Harness-private files.

Track deployment rendering, startup readiness and real invocation evidence
separately. `WorkloadApplied=True` proves neither successful hydration nor Pod
readiness. Kind can exercise runtime startup and channel/session behavior when
configured with reachable ACH and model endpoints; a live-model cluster run
remains a separate integration check. Existing Compose evidence does not replace
operator-rendered Kubernetes coverage.
Separate Deployments, new queues and autoscaling are not part of this renderer change.
