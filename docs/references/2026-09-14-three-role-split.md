# Channels, harness and engine: current split contract

Status: implementation reference for `feat/phase1-split`. This document supersedes
the archived split proposals and plans in `docs/superpowers/`. The
[validation report](../reports/unix-split-validation.md) records what was exercised
and on which revisions; it is evidence, not an alternative specification.

## Placement and transport

One pod contains three ordinary containers running the same application, selected by
`--role channels`, `--role harness` and `--role engine`. The image uses tini to
reap orphan processes; this is not a Kubernetes init container. Packaging also
provides images with dependencies tailored to each role. Local operation
keeps one launcher and a child mini-harness, including native TUI support.

Channels normalize source events. The harness owns admission, bounded in-memory
queues, FIFO lanes, prompts, terminal validation, credentials and capability
proxies. The engine mini-harness owns native drivers, the native engine pool,
session mappings, native configuration and process lifecycle.

Channels and harness exchange serializable events, admission decisions and
correlated results over `channel.sock`. Harness and mini-harness exchange launch
inputs, turns and lifecycle events over `agent.sock`. Both use HTTP over Unix
sockets. There is no internal HMAC, bootstrap configuration file or TCP control
port. Each socket directory is mounted only into its two participants; a shared
directory containing both sockets would not provide that separation.

The existing router remains the queue and consumer. Redis source channels retain
their existing acknowledgement policy; accepted work is not durably completed.
Results and retry correlation are retained in memory for a fixed 300 seconds.
There is no public retention setting or new durable broker.

## Configuration and environment

The full agent configuration is private to the harness. Channels receive their
source inputs; the engine receives only explicitly selected public launch inputs,
including proxy/model/MCP settings and workspace/engine parameters.

`engine.forwardEnv` selects environment names whose values the harness resolves
and sends to the mini-harness. This is deliberate exposure to the engine; it does
not copy the whole harness environment. Managed upstream credentials stay in
harness-owned proxies. The mini-harness writes native OpenCode/Pi configuration;
turns carry prepared prompts rather than the complete agent configuration.

## Workspace, HOME and hooks

The existing `session_key` selects the workspace and FIFO lane. Harness and
engine mount the same workspace volume at the same path; no artifact transfer,
checkout copy or workspace reset is introduced.

The engine has its own HOME for native state. Prepare, cleanup and script-only
hooks use a harness-private HOME, separate from that engine HOME and from the
workspace. This is the deliberate compatibility change from the original
`HOME=ACH_WORKSPACE` hook behavior: scripts producing work for the engine must
use `ACH_WORKSPACE`, not `HOME`.

Prepare still runs after admission on the lane, before engine acquisition, with
`cwd=ACH_WORKSPACE`. Cleanup retains its existing teardown timing and parent
working directory. Warm reuse does not automatically run cleanup after every
response. Hook environment selection, event values, timeouts and output handling
remain in the harness. Script-only channels do not acquire a native engine.

ACH executes operator scripts; it does not inspect Git commands, rewrite hooks,
sanitize checkout configuration, require private clones or manage repository
publication. The workspace remains engine-writable. A credential-bearing script
that loads configuration or code from that workspace is the operator's
responsibility. A private hook HOME does not make the shared checkout trusted,
and local processes under one UID do not gain container isolation.

## Sessions, results and lifecycle

Lane identity and conversation reuse identity remain distinct. Preserve
`session: none|auto|custom`, warm idle-TTL reuse, multi-turn invocations,
terminal repair, discard/compact/rotation, usage and native session statistics.
Conversation locking prevents two lanes from concurrently using the same
native conversation.

The native map lives under engine home. The bounded legacy import preserves
conversation mappings from the original harness `state/state.db` on upgrade;
it is not merely compatibility with an earlier split deployment.

The controller connection scopes ownership and rejects stale requests. Native
stop must be confirmed before cleanup or unsafe reuse; loss and failure paths
remain bounded. Engine-side deadlines and output caps bound execution time and
buffered output independently of a slow or disconnected harness.

The session-ready acknowledgement remains: it orders harness trace correlation
before model traffic, including OpenCode's recreation of a stale session after
a 404. Resolving the session initially does not cover that later transition.

## Scope

This cleanup retains the implemented API, controller, native pool and completion
registry. It removes a public retention knob, clarifies names and examples, and
separates hook HOME. It does not introduce a replacement RemoteDriver API.

Further proxy content protection and credential-free tool integrations are a
separate phase. Separate deployments, durable work handoff, scale-to-zero,
autoscaling and S3 session artifacts remain future work.

See `docker/split/README.md` in the repository for mounts, networking and
commands, and the root `README.md` for the lifecycle diagram.
