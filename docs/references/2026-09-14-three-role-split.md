# Channels, harness and engine: current split contract

Status: the original split was merged into local `main` on 2026-09-14. The startup
hydration and operator-storage update below is implemented, locally validated and
merged into `main` for v0.16.2. This document supersedes
the archived split proposals and plans in `docs/superpowers/`. The
[validation report](../reports/unix-split-validation.md) records what was exercised
and on which revisions; it is evidence, not an alternative specification.

## Placement and transport

The ACH operator proposal offers `standalone` (one combined container) and
`distributed` (the three-container boundary below), with one Deployment and one
active pod in either case. This is operator placement, not a new agent config field
or CLI flag. The existing launcher without `--role` serves standalone; explicit
role args serve distributed. See the self-contained
[operator handoff](../schemas/ach-deployment-modes.md).

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

`engine.forwardEnv` selects environment names. The operator supplies those selected
values to the Engine container; the standalone parent supplies them to its child.
The execution API carries names only, and the mini-harness resolves them from its
own environment. This is deliberate exposure to the engine; it does not copy the
whole harness environment. Managed upstream credentials stay in
harness-owned proxies. The mini-harness writes native OpenCode/Pi configuration;
turns carry prepared prompts rather than the complete agent configuration.

## Startup hydration

Harness downloads using its credentials into a unique batch under the shared
temporary `/run/ach-agent/transfer` mount. It resolves its prompt text before
handing the batch path to the mini-harness through the existing controller-open
request. The mini-harness installs real files in its own home, checks native boot
configuration, and removes the batch before reporting startup success. Initialization
does not wait for an event or launch a native conversation. Initialization failure
makes the engine role unhealthy and terminates it; a later native launch failure
remains an invocation failure.

Distributed storage has three generic data roots: Harness-only `base/state`,
Engine-only `base/home`, and shared `base/workspace`. `base` is the configured
persistence mount or `/tmp/ach-agent`. Transfer storage is always temporary and
separate. Native sessions and tool files stay inside Engine-owned storage; the
operator does not render native-specific mounts. Channel hooks still use the
workspace and have no responsibility for removing hydration downloads.

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

The prior preserve-behavior plan's five tasks are complete: characterization,
selected environment forwarding, H-side hooks, Unix-socket configuration delivery,
and packaging/real acceptance. The subsequent bounded cleanup is complete as well.
Post-merge tests on `main`: 1,178 passed, 3 skipped; Ruff and strict mypy passed.
The subsequent startup/storage update passes 1,230 tests (4 skipped), strict lint
and schema checks. Real Pi/OpenCode Compose, native TUI, temporary storage and a
Harness-process restart were exercised; see the validation report for image
revisions and limits. No operator rollout or image publication is claimed.

HTTP health uses Channels 8080, Harness 8090 and Engine 8081, with `/readyz`
for startup/readiness and `/healthz` for liveness. Kubernetes uses `httpGet`;
the Harness/Engine TCP listeners do not expose internal APIs. Standalone retains
its configured public port and shares the same readiness conditions, including
completed hydration installation and downstream engine availability.

Optional cleanup remains: move public projection JSON examples used only
by manifest tests into fixtures. This is not a blocker and does not justify
replacing the execution API or result registry.

This cleanup retains the implemented API, controller, native pool and completion
registry. It removes a public retention knob, clarifies names and examples, and
separates hook HOME. It does not introduce a replacement RemoteDriver API.

Further proxy content protection and credential-free tool integrations are a
separate phase. Separate deployments, durable work handoff, scale-to-zero,
autoscaling and S3 session artifacts remain future work.

See `docker/split/README.md` in the repository for mounts, networking and
commands, and the root `README.md` for the lifecycle diagram.
