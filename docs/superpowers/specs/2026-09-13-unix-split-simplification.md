# Split simplification: queues, Unix sockets and private configuration

Status: design approved in conversation; not implemented. Baseline `8f06991`
on `feat/phase1-split`. This changes the transport/bootstrap portions of the
previous simple-operator contract; existing execution behavior remains binding.

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
Credentials stay in their owning container environment. No new public CR fields.

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
operations. E's instance identity exists before configuration. Reconnect after
confirmed cleanup preserves native mapping behavior; immutable engine/layout
changes require process/pod replacement, not hot reload. Fresh proxy URLs after
H restart are valid per-execution inputs, not layout changes.

`engine.forwardEnv` carries names; the mini-harness reads their values from E's
environment. Keep managed-secret exclusions and existing deliberate passthrough
MCP behavior. Native config files generated by E remain allowed: they are the
engine's own public configuration, never the private agent document.

## Preserve current behavior

No changes to sessions, conversation keys, terminal repair, budgets, workspace
handoff, private credential-bearing prepare/cleanup, migration or proxy policy.
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
role-artifact files. Do not retain two production transports for the unpublished
split. Update manifests, tests and the operator handoff together.

Acceptance: actual read-only socket mounts, C/E without full config, no secret
on either wire, both native engines with session reuse and streaming, overlapping
stream/cancel traffic, controller loss, H restart, script-only work, native TUI,
ordinary Compose without internal env boilerplate, and the unchanged full gate.
Image publication and external ACH rendering remain separately authorized work.
