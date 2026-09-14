# Split simplification: queues, Unix sockets and private configuration

Status: implemented on `feat/phase1-split`, verified locally on 2026-09-14.
See the [validation report](../../reports/unix-split-validation.md) for exact revisions,
test results and the boundary between local evidence and an external rollout.
Implementation starts from `d855fa5` on `feat/phase1-split`; **functional behavior
is defined by the original agent v0.16.1 (`462912f`), before the split**.
The split changes placement and transport, not the original agent's features or
policies. Remove mechanisms introduced by our split that changed those behaviors.
Use baseline characterization tests to settle details, not new design preferences.

Execution plan: [preserve behavior and simplify the split](../plans/2026-09-14-preserve-behavior-simplify-split.md).

## Intended system

```text
channels -- channel.sock --> admission / bounded RAM queues / consumers in harness
                                                                  |
                                                             agent.sock
                                                                  |
                                                           mini-harness
                                                                  |
                                                             Pi / OpenCode
```

One pod, three ordinary containers, one active replica. The full configuration
is private to H. C and E receive only the inputs they need through their socket.
Managed ACH credentials remain H-side. Deliberately selected engine environment
values cross the execution socket. No new public CR fields.

## Queue boundary

The existing `Router` and `Lane` already implement bounded RAM queues and their
consumers. Keep them as the queue implementation; do not insert another inbox,
scheduler, queue-capacity setting, broker class hierarchy or worker pool.
`ChannelsClient.handle/submit` sends an event; the existing completion registry
and router admit it. Admission is not completion. Preserve dedup, backpressure,
FIFO, separate script concurrency, result correlation and bounded retention.

Future Redis may replace the handoff, but this increment promises no durable
acceptance. Existing Redis **source channels** remain supported; they are not
the future internal broker. Their existing ACK behavior is unchanged.

## Two sockets

| Endpoint | Socket owner | Directory mounts |
| --- | --- | --- |
| `/run/ach-agent/channels/channel.sock` | H | H writable, C read-only, absent E |
| `/run/ach-agent/engine/agent.sock` | E | E writable, H read-only, absent C |

Use two distinct `emptyDir` volumes, outside workspace and engine home. Mount
directories, not individual sockets. UID/GID/fsGroup 10001; sockets 0600.
Mounted directory roots may be owned by root with fsGroup write access; do
not require the non-root process to chmod/chown them. Locally created private
runtime directories use 0700. A read-only directory mount permits connecting to a socket
but prevents replacing its filesystem entry. Verify this in actual containers.
Server restart may remove its own stale socket, never a live listener, regular
file or symlink. No recursive cleanup of the mount or unrelated contents.

HTTP with existing JSON/NDJSON runs over Unix sockets using the installed
httpx/Uvicorn stack. A socket pathname is one endpoint, not a single connection:
retain independent connections for streams and cancellation. No bespoke framing,
HTTP/SSE tunnel, TCP fallback, TLS or internal HMAC. External webhook/A2A source
authentication remains intact. Engine-container access to its own endpoint is
accepted under the existing agent-wide trust scope.

Only the control endpoints lose TCP. Channels ingress, model/MCP/A2A proxies
and native OpenCode HTTP remain where required by their existing clients.
Proxy URLs are supplied by H automatically; the operator does not configure
internal control ports or protocol details.

## Configuration over the existing connection

H alone reads `ACH_CONFIG_PATH`, hydrates and starts proxies. It constructs
allowlisted public inputs; it never serializes the whole config and then tries
to scrub it. No generated C/E bootstrap JSON files or bootstrap key remain.

C obtains `{agentName, channels}` from `GET /internal/v1/config` on channel.sock
before starting its source adapters. `channels` uses the existing
`ChannelSourceConfig` projection; no prepare/cleanup scripts or secret values.
C resolves source credentials from its own environment. H can expose this
only after its source projection is available; C waits boundedly for H.

E starts its socket without a config or native process. H supplies the existing
`PublicEngineConfig` in its existing controller-open request. This selects the
adapter, prepares E-owned paths and opens E's native session store; it does not
launch Pi/OpenCode. The existing `AcquireRequest.config` supplies per-execution
settings when work arrives. Keep this existing DTO rather than add a parallel
start protocol. Do not echo config in the controller response.

Controller initialization precedes existing session-map import and workspace
reservation. E's instance identity exists before configuration. Reconnect after
confirmed cleanup preserves native mapping behavior; immutable engine/layout
changes require process/pod replacement, not hot reload. Fresh proxy URLs after
H restart are valid per-execution inputs, not layout changes.

The public configuration keeps `engine.forwardEnv` as the original name selector.
H resolves eligible selected names and sends their **values**, in internal wire
field `engineEnv: dict[str, str]`. E applies this explicit mapping to the native
process, without requiring duplicate operator env routing to E and without
mutating the mini-harness's global environment. Keep baseline sanitization and
deliberate passthrough MCP behavior. A custom token explicitly forwarded is
deliberate engine exposure; managed ACH/proxy/source/preparation-only credentials
are not automatically forwarded. Never log or echo the environment mapping.

Native config files are generated by E's existing adapters from these launch
inputs. URLs/model/MCP settings are acquisition configuration, not repeated turn
payloads. H builds prompts and interprets channel configuration; E receives no
channel hook scripts or complete private config.

## Existing workspace and hooks: restore, do not redesign

Keep the exact original `workspace_dir(work_dir, session_key)` formula and logical
paths. A workspace belongs to the existing FIFO/session key, not a new invocation
directory. Preserve the separate native-conversation key and original reuse rules.
Do not rename directories, reset sessions or clear repositories on upgrade.

H/E mount the same workspace volume at the same logical path. H executes **all**
prepare and cleanup hooks as originally configured, with or without credentials.
Prepare runs on the admitted lane before acquisition; `cwd` and hook `HOME` are
the workspace. Cleanup runs best-effort at original teardown points, with cwd at
the workspace parent and hook HOME still the workspace. Keep timeouts, shell/stdin
handling, selected env, event-value validation, output limits and redaction.

No automatic private clone, bundle export/import, repository reset, Git inspection
or script policy enforcement is part of this split. The operator-script author
owns safe handling of agent-modified repository configuration. This knowingly
restores the original shared-checkout trust model; containers do not remove that
property. Do not remove existing basic validation while removing the new machinery.

The native engine home remains E-owned. Preserve the workspace's `.ach-state`
view of hydrated public content using the narrow shared public-context mount;
do not expose H state or require H to mount E's private home to run a hook.

Preserve warm idle TTL, cleanup registration and original failure behavior. H must
not run destructive cleanup before E confirms stop, and cleanup must not race a
new event's prepare. Reuse the smallest existing reservation/stop-notification/
acknowledgement mechanism needed to carry the original pool callback across IPC.
Remove script execution and bundle transfer from E; do not create a new workspace
manager or perform cleanup after every response merely to simplify transport.

`webhook-script` remains H-side, uses original temporary-workspace/payload semantics
and its separate script concurrency pool; it never acquires an engine.

## Preserve current behavior

No functional changes to original sessions, conversation keys, terminal repair,
budgets, prompt composition, hook behavior, source results or proxy policy.
Keep existing controller-loss cleanup and explicit stop confirmation. No new
leases, heartbeats, generations, replay or quarantine machinery.

Local RPC execution uses the same Unix endpoint in a private short temporary
directory, with a parent-owned mini-harness child. Local native Pi/OpenCode TUI
still inherits the real terminal. Its public launch configuration travels over
the same controller socket, never stdin, and preserves its existing native-TUI
exception to pooled execution. No remote terminal protocol is introduced.

H/E probes execute HTTP over their Unix socket. E endpoint health/readiness
must allow initial controller configuration without first launching a native
engine; no startup cycle. C public probes and ingress remain unchanged.

## Removal and release gate

Remove internal HMAC signing, nonce caches and key distribution; shared bootstrap
files/readers/writers; ordinary internal TCP host/port/URL configuration; local
role-artifact files; E-side channel hook execution and mandatory private Git/bundle
handoff introduced by the split. Do not retain two production transports for the unpublished
split. Update manifests, tests and the operator handoff together.

Acceptance: baseline characterization parity; actual read-only socket mounts;
C/E without full config; no managed secret leakage and only explicitly selected
engine values on the execution wire; both native engines with session reuse and streaming, overlapping
stream/cancel traffic, controller loss, H restart, script-only work, native TUI,
ordinary Compose without internal env boilerplate, and the unchanged full gate.
Image publication and external ACH rendering remain separately authorized work.
