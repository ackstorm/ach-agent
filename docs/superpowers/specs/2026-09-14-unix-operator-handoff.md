# Unix split operator handoff

Status: implemented and locally validated on `feat/phase1-split`, 2026-09-14.
Image publication and external operator rollout remain separate. This document is
self-contained for an operator rendering the runtime outside this repository.

## Deployment shape

Run one pod with three ordinary containers and one active replica:

| Role | Entry point | Responsibility |
| --- | --- | --- |
| C, channels | `tini -- python -m ach_agent.main --role channels` | Public ingress, source adapters, event submission and result delivery |
| H, harness | `tini -- python -m ach_agent.main --role harness` | Full config, hydration, queues, hooks, prompts, proxies and lifecycle coordination |
| E, engine | `tini -- python -m ach_agent.main --role engine` | Native adapter, session store, engine home and process lifecycle |

H is the only role that reads the full rendered runtime config and managed credentials.
C receives source-only channel inputs. E receives the existing `PublicEngineConfig` through
the existing controller request. No bootstrap JSON, bootstrap key, internal HMAC, duplicated
managed secret, or operator control URL is required.

The image already includes `tini` in its entrypoint. Set container `args` to
`["--role", "channels"]`, `["--role", "harness"]` or `["--role", "engine"]`;
do not replace the entrypoint or add an init container. `tini` forwards termination
signals and reaps orphaned processes. The mini-harness still owns engine launch and stop.

## IPC mounts and probes

Create two distinct IPC directory volumes. Mount the directories at the same paths:

| Directory and socket | Owner | Mounts |
| --- | --- | --- |
| `/run/ach-agent/channels/channel.sock` | H | H read/write; C read-only; absent E |
| `/run/ach-agent/engine/agent.sock` | E | E read/write; H read-only; absent C |

Mount directories rather than individual socket files. Use the existing UID/GID and fsGroup
10001. Socket files are mode 0600. H and E health/readiness probes make HTTP requests over
their Unix socket; C's public probe and ingress use HTTP on port 8080. There are no internal
control TCP ports. Model, MCP and native OpenCode HTTP endpoints remain available wherever
their existing clients require them.

Use `/healthz` for startup/liveness and `/readyz` for readiness. For example, H's
exec probe is `python -c` with the following program; E uses the engine socket path:

```python
import httpx
with httpx.Client(
    transport=httpx.HTTPTransport(uds="/run/ach-agent/channels/channel.sock"),
    base_url="http://ach-internal", timeout=2,
) as client:
    client.get("/readyz").raise_for_status()
```

Allow bounded startup time for hydration (the examples use 300 seconds). All three
containers share the pod network namespace, so native model/MCP HTTP can continue
using loopback. Only C's ingress needs a Kubernetes Service port, normally 8080.

The C-to-H request on `channel.sock` is `GET /internal/v1/config`, returning source-only
`ChannelInputs` before C starts source adapters. H-to-E initialization uses the existing
controller-open request on `agent.sock` with `PublicEngineConfig`; the controller response
does not echo private configuration. Turns, streams, cancellation, stop confirmation and
result correlation continue through the existing HTTP request/event API.

## Configuration and environment

H hydrates the full config and starts the existing model/MCP/A2A proxies. H keeps managed
ACH, model, MCP and preparation credentials. The existing `engine.forwardEnv` remains a
name selector; H resolves the selected values and sends the explicit `engineEnv` mapping to
E. E applies that mapping to native children without requiring the operator to duplicate
those variables in E and without mutating E's global environment. Only deliberately selected
values cross this boundary.

Mount the full YAML only into H at `/etc/ach-agent/config.yaml`, or set the existing
`ACH_CONFIG_PATH` override there. Supply H's existing ACH and hook credentials to H.
Source authentication references in C's projection resolve from C's own environment;
provide the relevant webhook/queue/A2A source credentials to C. E needs neither the
full YAML nor copies of H's selected `forwardEnv` values. Internal socket paths and
role defaults are application responsibilities, not additional operator EnvVars.

This Unix-socket simplification adds no CR or public runtime field. Relative to original
v0.16.1, the earlier split already added optional `limits.resultRetentionSeconds`
(default 300, positive integer up to 86400) for in-memory result retention. Existing
configs remain valid; the operator need not set it. The typed C/E projections are
internal API models, not new CR fields. Do not add a scheduler, internal broker,
lease, heartbeat, generic transport layer or new cleanup service.

## Workspace and hooks

H and E mount the existing workspace volume at the same logical path. Preserve the exact
`workspace_dir(work_dir, session_key)` mapping, session reuse, warm idle TTL and native
conversation behavior. H executes every configured `prepare` and `cleanup` hook, with or
without `secretEnv`, using the original shared-workspace contract:

- prepare runs after lane admission and before native acquisition, with cwd and `HOME` equal
  to `ACH_WORKSPACE`;
- cleanup runs best-effort at the original teardown point, after confirmed native stop when
  an engine was acquired, with cwd at the workspace parent and `HOME` still the workspace;
- selected env, event variables, shell/stdin behavior, timeout, output bounds, redaction and
  failure policy remain unchanged;
- `webhook-script` remains H-side, uses its original temporary-workspace and newline-terminated
  payload behavior, and does not acquire a native engine.

There is no mandatory private clone, bundle export/import, workspace reset, Git inspection or
credential-dependent hook path. This preserves the original shared-checkout trust model.
The native engine home remains E-private. H state remains H-private. The existing shared
public context is H-write/E-read.

## Operator boundaries and deferred topology

Expose only C's public ingress from this target pod. Use ordinary volume permissions so C can
connect through its read-only channels directory but cannot replace H's socket; E can write
its engine socket while H connects read-only. Keep runtime configuration and H state out of C
and E mounts, and keep E's native home out of H mounts.

This handoff covers one pod, one active replica and the two-socket split. A future topology
with separate channel deployments and separate H+E replicas is deferred. Image publication,
cluster rollout, dynamic PVC/ConfigMap/Secret rendering and external operator changes require
separate authorization and validation.
