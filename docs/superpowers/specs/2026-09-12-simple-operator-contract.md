> Archived design history. Superseded by the [current split contract](../../references/2026-09-14-three-role-split.md). Do not use as implementation instructions.

# ACH operator handoff: three-role agent

This is the Phase 1 packaging contract implemented in the local `ach-agent`
branch. Compatible images still need publication before external rollout.
The operator creates one Deployment with one replica, one pod and three
ordinary containers. No operator-generated role projections or internal
authentication settings are required.

## Image and role selection

Use the compatible image target for each role. Every role owns the same image
entrypoint:

```dockerfile
ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ach_agent.main"]
```

Select roles with Kubernetes `args`; do not set `command`, because that would
replace the image entrypoint and bypass tini:

```yaml
args: ["--role", "channels"]
args: ["--role", "harness"]
args: ["--role", "engine"]
```

Tini is the image's small PID 1 process: it forwards signals and reaps orphaned
children. It is neither another container nor a Kubernetes initContainer.
The same compatible combined image can run all three roles; separate image
targets are optional (`channels`, `harness`, `engine-opencode`, `engine-pi`).
The combined image remains the local default and preserves `--tui`, `--debug`,
and `--prompt` launchers. Image publication is separate from this contract;
do not assume those target names are available registry tags.

## Configuration and environment

Mount the existing full agent configuration only into H at
`/etc/ach-agent/config.yaml`. The image already sets `ACH_CONFIG_PATH` to that
path. Another config location can use the existing `ACH_CONFIG_PATH` override.
Continue supplying existing integration values and Secret references to H,
such as `ACH_BASE_URL`, `ACH_TOKEN`, and required metadata such as
`POD_NAMESPACE` where used for memory identity.

ACH's existing environment merge rules remain in force. A
`valueFrom.secretKeyRef` stays a Kubernetes reference and is resolved in the
role that owns it. Source credentials belong to C; preparation and capability
credentials belong to H. E receives names selected by `engine.forwardEnv` and
the existing passthrough MCP environment references it needs. The mini-harness
resolves their values from E's own environment. Explicit custom secrets are
supported; managed ACH credentials are excluded. `prepare.forwardEnv` remains
independent. Do not clone the full environment into every role or introduce
new `channelsEnv` / `engineEnv` fields.

C and E do not need `ACH_CHANNELS_CONFIG_PATH`, `ACH_ENGINE_CONFIG_PATH`,
`ACH_HARNESS_URL`, `ACH_ENGINE_URL`, internal host/port variables,
`ACH_AGENT_NAME`, or an operator-generated HMAC key. Identity and internal
connection details come from H's generated bootstrap files. `engine.forwardEnv`
continues to select names; the selected values are supplied in E's own
container environment and resolved there. Managed credentials and
preparation-only secret names remain excluded.

## Mount contract

H writes both bootstrap files atomically. C and E consume only their permitted
file through read-only mounts:

| Path | Owner | Access | Purpose |
| --- | --- | --- | --- |
| `/run/ach-agent/channels/bootstrap.json` | H | H read/write, C read-only | source channels, agent identity, H URL, stable C/H key |
| `/run/ach-agent/engine/bootstrap.json` | H | H read/write, E read-only | credential-free `PublicEngineConfig` |
| `/var/lib/ach-agent/state` | H | H read/write | dedup and harness state |
| `/var/lib/ach-agent/home` | E | E read/write | native home and sessions |
| `/var/lib/ach-agent/state` | E | E read/write | E-owned codemem directory |
| `/var/lib/ach-agent/workspace` | H/E | H/E read/write | prepared workspaces |
| `/var/lib/ach-agent/public-context` | H/E | H read/write, E read-only | hydrated public context |
| `/tmp/ach-private` | H | H read/write only | private credential-bearing preparation scratch |
| `/tmp` | Each role | separate per-container writable mount | temporary runtime files |

Mount the two bootstrap **directories**, each backed by a separate `emptyDir`:
`/run/ach-agent/channels` and `/run/ach-agent/engine`. Do not mount individual
bootstrap files with `subPath`: atomic replacement must remain visible to
the readers. Neither bootstrap volume is the shared workspace.

The two logical `/var/lib/ach-agent/state` mounts must use different physical
PVC subdirectories. Preserve the existing codemem SQLite database, including
present `-wal` and `-shm` siblings, as one offline migration before rollout.
Preserve existing harness state and engine home/session data; native mapping
import is owned by ach-agent, not the operator. Provision the narrow PVC
subdirectories with UID/GID 10001 access before rollout. C has no full config,
state, workspace, home, or public context mount.

When persistence is disabled, use separate `emptyDir` volumes and the matching
ephemeral paths: H state `/tmp/ach-harness-state`, E home `/tmp/ach-home`,
E codemem `/tmp/ach-home/state`, shared workspace `/tmp/ach-home/workspace`,
and public context `/tmp/ach-public-context`. Bootstrap paths stay unchanged.
Explicit configured home/workspace paths require matching narrow mounts.

The image pre-creates `/run/ach-agent/channels` and
`/run/ach-agent/engine` for UID 10001 for Docker volume initialization.
On Kubernetes, set `runAsUser`, `runAsGroup`, and `fsGroup` to 10001 so mounted
`emptyDir` directories are writable. No hydration init container is needed. H's
bootstrap key is stable across a harness process restart when its bootstrap
volume persists. Restarting the whole pod is the normal rollout boundary and
regenerates the ephemeral bootstrap files.

## Ports and probes

The defaults are H `127.0.0.1:8090`, E `127.0.0.1:8081`, and C
`0.0.0.0:8080`. Publish only C's ingress. Compose places C and E in H's
network namespace; Kubernetes uses one pod. Configure probes as follows:

| Role | Probe mechanism | Readiness | Startup and liveness |
| --- | --- | --- | --- |
| H | exec HTTP request to `127.0.0.1:8090` | `/readyz` | `/healthz` |
| C | Kubernetes httpGet on pod port `8080` | `/readyz` | `/healthz` |
| E | exec HTTP request to `127.0.0.1:8081` | `/readyz` | `/healthz` |

All H/E probes use exec because those listeners bind loopback. For example:
`["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8081/healthz', timeout=2)"]`.
Give each startup probe a 300 second budget (`periodSeconds: 5`,
`failureThreshold: 60`). Compose healthchecks use `start_period: 300s`.

Use restricted containers: non-root UID 10001, read-only root filesystems,
all capabilities dropped, no host network/PID/IPC, no shared process namespace,
no runtime socket, and no automatic service-account token. Keep one active
execution instance during updates to preserve session ordering; use a
non-overlapping update strategy, not the default rolling surge. Keep the
existing public Service identity and point it at C port 8080. H/E need no
Service. Configuration/image changes roll the whole pod. ACH owns resources,
scheduling, termination grace and workload status, not internal protocols.

## Status and evidence

Implemented in the ach-agent Phase 1 split worktree. Final packaged acceptance
ran from commit `929397a`; the complete log is
`/tmp/simple-bootstrap-compose-929397a.log`:

- Harness-owned bootstrap publication, bounded reads, atomic permissions,
  stable key reuse, role defaults, and fail-closed startup.
- tini-owned image entrypoints, UID 10001 runtime directories, persistent and
  ephemeral Compose examples, and the Kubernetes three-container example.
- Real synthetic Compose acceptance passed for both OpenCode and Pi, including
  two completed events with one native session, engine failure cleanup and 503
  harness readiness, and native `DEBUG`/custom token forwarding. The run built
  H `sha256:16710e2644c20ba7b4e2b6d8e219fd14f8b7493a4d5710801388967039e253cb`,
  C `sha256:1a996eca06998064f6d4278f6439a09a089c5554f8550459167698253b4eb8a5`,
  E/OpenCode `sha256:7f1a52ed8e3601480bc518f52822ea0cbe52ebfc12159255602d19c956b4008f`,
  and E/Pi `sha256:4924e157cb5e42dc55a262d441399e5ddfc5fa02ab3b0446329355387e5292c8`.
- Independent harness restart continuity evidence is recorded in
  `docs/reports/simple-bootstrap-root-validation.md`.
- Root's unchanged repository gate passed at `929397a`: 1,224 tests passed,
  3 skipped; 18 conformance tests; lint, type checking and secret scan passed.
  Root also validated actual native Pi/OpenCode TUIs with two turns, resize
  and clean exit from the newly built combined image. The final Kubernetes
  manifest has contract-test coverage but was not reapplied to a cluster in
  this increment.

Deferred to later phases: two-Deployment on-demand execution, durable queueing,
autoscaling/KEDA, S3 persistence, per-container egress isolation, and renderer
changes in the separate `ach` repository. These examples do not mutate a cluster.

The public schema remains version `1`. The only relevant Phase 1 addition is
the optional `limits.resultRetentionSeconds` integer, default `300`, with
exclusive minimum `0` and maximum `86400`; it is not required.
Synchronize the operator schema fixture; no new CR setting is needed just to
use that default. Existing `env` and `engine.forwardEnv` remain the public API.

Future direction only: keep one logical ACHAgent, with a channels Deployment
at one replica and an execution Deployment containing H+E at zero or one.
This requires a durable handoff and scaling policy later; setting today's H/E
replicas to zero does not implement it. No new CR deployment mode is requested
in this increment.
