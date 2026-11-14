# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

### Added

- `ach-agent promote --init` writes a `deploy.yaml` skeleton beside `agent.yaml`: `baseUrl` from the
  manifest's ach-cli profile, an `openai` channel, commented examples of the other channel types, and
  `CHANGE_ME` for each decision left. `promote` refuses a `deploy.yaml` that still holds a `CHANGE_ME`,
  and a missing `deploy.yaml` names `--init`.

## [0.7.11] - 2026-11-14

### Changed

- The MCP tools check gives each server 10 s to list its tools instead of 4 s. Its probes run
  while the engine connects to the same servers, and 43% of them took over 4 s (up to 7 s),
  so a launch retried after a 2 s wait: 4 of 8 local launches, about 4 s each. A server that
  never answers fails the run after 120 s instead of 84 s.

## [0.7.10] - 2026-11-13

### Changed

- The MCP tools check (every MCP server lists at least one tool) runs when the engine launches a
  process, beside its boot, instead of before every run. A message to a running engine skips it,
  since that process passed it at launch: measured at 1.0–2.3 s of every message to a running
  engine, all of its variation. A failed check still fails the run before its prompt, with the
  same `mcp tools unavailable` reason, and the next run checks again.
  opencode now creates or resumes the session only after it is ready and the check has passed.

## [0.7.9] - 2026-11-13

### Changed

- An Agent's control pod joins its Service sooner after it starts: the readiness probe runs
  every 2 s instead of every 10 s (15 failures, so the 30 s tolerance stays). A pod whose first
  readiness check missed took 21 s to take messages instead of 10–11 s.

## [0.7.8] - 2026-11-13

### Changed

- An ACH Agent takes its model's context and output limits from `/v2/model/info`, which resolves
  LiteLLM model-group aliases; the limits in the `/platform/hydrate` response are ignored.
  `capability.ach.model.limits` still wins over model info.

### Fixed

- A run whose environment context (ACH skills, prompts, artifacts) is already installed in the
  session keeps opencode running: the context is sent before every run, and installing it
  stopped opencode, so every message of an agent with ACH context paid an engine boot and MCP
  reconnect (~3.5 s). A changed context still installs and restarts the engine.

## [0.7.7] - 2026-11-13

### Added

- `ach-agent run` builds a `Dockerfile` beside `agent.yaml` on top of the runtime image, passed
  as the `ACH_AGENT_IMAGE` build argument, and runs the result: an agent's own tools work locally.

### Changed

- `ach-agent run` opens the full opencode TUI on the session instead of `opencode mini`.

### Fixed

- An A2A reply the model wrote without the `a2a_reply` JSON object is delivered as text and the
  task completes, instead of failing with "invalid terminal output". An object that is present
  but invalid still fails the task.

## [0.7.6] - 2026-11-13

### Added

- The control log writes a `run started` line per run with `duration_ms` from the event's
  admission to the engine accepting its prompt: the wait before the model starts, including
  sandbox start, engine boot and MCP connect.

### Fixed

- A restored opencode session that has not run yet can be suspended, archived and closed:
  with no process started there is nothing to stop.
- A restored session id starts a fresh session only when opencode answers 404 for it; any other
  answer, or no answer, fails the run and the next run checks the same id again, so a store
  still opening no longer loses the restored conversation.

## [0.7.5] - 2026-11-12

### Changed

- `ach-agent run` starts the `opencode` binary by default instead of `opencode2`; `OPENCODE_BIN`
  still overrides it. The images set it to their bundled binary and are unchanged.

### Fixed

- Resuming a session whose opencode had stopped (kept HOME, S3 restore or volume) boots opencode
  once instead of twice: the restore installs the HOME and records the session id, and the run's
  start launches opencode with its full config and checks that id. A session id opencode no
  longer knows starts a fresh session in the same HOME instead of failing the restore.

## [0.7.4] - 2026-11-12

### Added

- An `openai` channel type answers the OpenAI Chat Completions API (`/v1/chat/completions`,
  `/v1/models`, streaming or not) so any OpenAI client can use the Agent as a model. A conversation
  continues its session by matching the history the client resends, kept in the Agent's S3 store
  when sessions are persisted; attached files and images become Workspace artifacts.

### Fixed

- A session's engine keeps running between its runs instead of restarting for each one, which
  cost about 3 s per reply (opencode boot and MCP reconnect). The progress and artifacts MCP
  servers are addressed per session (`/sessions/{session_ref}/`) instead of per run, so a
  session's engine config no longer changes from one run to the next.

## [0.7.3] - 2026-11-11

### Fixed

- The live-session bound of 0.7.2 reads the pod's sessions instead of the lifecycle's last idle
  check, so it no longer misses sessions started since that check, or counts sessions of a lost
  pod. It also counts sessions whose engine is still starting, skips sessions with a run still
  active on the pod or a failed save waiting for its retry, and closes through the same path as
  the idle timeout.

## [0.7.2] - 2026-11-11

### Added

- `ACHAgentRunsFailing`: 3 or more failed runs of an agent in an hour and none completed. Runs that
  start and then fail (a model or MCP call refused for budget or auth, an upstream down) record no
  anomaly, so nothing alerted on them.

### Changed

- `workspace.maxConcurrentSessions` bounds live sessions, not only running ones: before a session
  starts its engine in a full Workspace, the longest-idle session is closed (saved, as on its idle
  timeout). Idle sessions kept their engine for the session idle timeout without counting, so a
  user's third conversation could get a 500 MiB sandbox OOM-killed mid-start.

### Fixed

- `ACHAgentStartFailing` is no longer silenced by failed runs: only a completed run in the hour
  clears it.
- An A2A reply block with a raw line break inside a string value (a model wrote `thoughts` across
  lines) was rejected as invalid JSON, so a completed turn reached the caller as `failed` /
  `invalid terminal output` and the answer was lost. The block is now parsed leniently.
- `run step` times a model call from when the request goes out (input delivered, previous step or
  tool ended), not from the engine's step start event, which both engines emit only with the first
  streamed chunk. `ttft_ms` was 0 and `duration_ms` left out the time to first token.

## [0.7.1] - 2026-11-11

### Added

- The sandbox logs one `run step` line per model call (duration, time to first token, tokens, finish
  reason) and one `run tool` line per tool call (tool, outcome, duration), keyed by `invocation_id`.

### Changed

- OpenShell 0.1.8: the e2e cluster, `docs/install.md`, and the operator's Go SDK, which moved to
  `github.com/ai-openshell/sdk-go` (v0.1.8). With 0.1.8 a sandbox delete on Kubernetes no longer
  waits out the pod's 30 s termination grace period (NVIDIA/OpenShell#5279).
- `inform_user`'s description carries the update rules for every agent: describe the work, never the
  tool, skill, script, API or command behind it; write in the language of the request; send one only
  when something changed, at most about one every 30 seconds.

### Fixed

- A2A channels also accept the A2A 0.3 method names (`tasks/get`, `tasks/cancel`, ...). LiteLLM's A2A
  gateway forwards `GetTask` and `CancelTask` under those names, so both failed with `Method not found`
  and a cancel through the gateway never reached the run.
- An A2A cancel ends the caller's open `SendMessage` or `SendStreamingMessage` request on the task
  with `canceled`; before, that request never returned.
- A run's model and MCP calls reach the gateway under one session: both engines send
  `baggage: session.id=<session_ref>` on every model and MCP request, and the facades drop the
  engine's own `x-<vendor>-session-id`, which would outrank it. MCP tool calls were each their own
  session, and pi's model calls had none. opencode model calls move from its `ses_` id to the
  `session_ref`.
- An A2A `tasks/cancel` stops the run in its sandbox, like a run past its deadline, and frees its
  session for the next request; a cancelled request that has not started yet never starts.
- A run adopted by the control after a restart is logged at INFO and no longer counts as a
  `start_outcome_unknown` anomaly, so a normal restart with a run in flight no longer fires
  `ACHStartOutcomeUnknown`.

## [0.7.0] - 2026-11-10

### Added

- `ach-agent run --event <text|@file>` runs one request without the TUI and prints the reply on stdout.
- `ach-agent promote` prints the Agent CR for an `agent.yaml` and its `deploy.yaml`.
- `agent.yaml` accepts `prompt.compose`.

### Changed

- **Breaking:** `agent.yaml` names its fields as the Agent CR does: `model.type` is `model.api` (default
  `openai`), `engine: <name>` is `engine.type`, `denyTools` is `engine.denyTools`, `exclude` is
  `filter.exclude`, and `prompt.file` / `prompt.text` are `prompt.system` (`type: file|text|ach`). The old
  forms are rejected.
- `ach-agent run` reads `./agent.yaml` when `--config` is not given.
- A2A progress: "Still working..." is sent after 120 s of silence instead of 60 s, and the
  start notice reads "Starting the agent, this can take a few seconds...".

## [0.6.14] - 2026-11-09

### Fixed

- A2A task recovery after a control restart completes the task again. Listing stored tasks sent
  S3 `ContinuationToken=None`, which boto3 rejects, so recovery failed before reading any task
  and a request whose run outlived the restart stayed `working` for good. A recovery that cannot
  read its tasks now logs a warning.
- A2A task records live under `a2a/<agent>/<channel>/`. The storage prefix is shared by every
  Agent in the namespace, so with `a2a/<channel>/` one Agent's recovery read the other Agents'
  tasks and would fail their running requests. Records written under the old path are no longer
  read or pruned.

## [0.6.13] - 2026-11-09

### Removed

- The 1 s grace-period re-delete of a terminating sandbox pod. The kubelet keeps the 30 s stop it
  already started, so a closed sandbox's pod still takes 30 s to go until the OpenShell gateway
  runs a release with NVIDIA/OpenShell#5279.

## [0.6.12] - 2026-11-09

### Fixed

- The operator no longer deletes a sandbox that is still starting. The gateway reports a sandbox
  Ready about a second before its pod shows Running, and a host pass in that window took the
  sandbox for lost, deleted it and created another, adding about 15 s to that start.
- The operator deletes a terminating workload pod again with a 1 s grace period, to work around
  OpenShell v0.1.7's supervisor hanging after SIGTERM (NVIDIA/OpenShell#5025). This has no
  effect and is removed in 0.6.13.

## [0.6.11] - 2026-11-09

### Changed

- `workspace.session.idleTimeoutSeconds` is optional. Unset, a session takes the default of the
  channel its last run came from: `0` for `cron` and `webhook-script`, whose sessions never get a
  follow-up, and `60` for `a2a`, `webhook` and `queue`. A set value applies to every channel, so a
  profile that sets it keeps today's behaviour; remove it to get the channel defaults.
- With `persistence: s3`, a session whose idle timeout is above `0` keeps its HOME in the sandbox
  after its close upload: the next message of that conversation resumes it in place instead of
  restoring it from S3. A `0` timeout still deletes the HOME.
- The runtime images upgrade their Alpine packages, which clears the HIGH CVEs of the pinned
  Python base (expat, musl, openssh, zlib).
- The Grafana dashboards no longer refresh on their own.

### Fixed

- `workspace.idleTimeoutSeconds: 0` works: the CRD accepted it but the control role refused it at
  boot.
- A control restart takes seconds instead of `shutdownTimeoutSeconds` when the host owns the Workspaces: the run drain is skipped because the next control adopts the runs.
- An A2A caller whose stream is cut by a restart gets a `working` status that says the run continues (or `failed` when the run had not started), instead of a silent end.
- A run no longer fails while the ACH platform restarts. Hydration and the skill, prompt and
  artifact downloads retry connection errors and 502/503/504 answers for 60 s, and the check
  that every MCP server lists its tools now retries for 60 s instead of 8 s.

### Added

- `workspace.idleTimeoutSeconds: 0` closes a Workspace on the next lifecycle tick once it holds no
  work and no open session, including right after a `sessionStart` gate skips the run; `-1` never
  closes it for idleness.
- A2A callers get queued, starting and still-working statuses from the runtime.
- A2A results survive a control restart: tasks are stored in the Agent's storage, recovered runs write their outcome into their task for `tasks/get`, and a run the restart broke is resumed once in the same session.

## [0.6.10] - 2026-11-07

### Removed

- The sandbox NetworkPolicy enforcement check from 0.6.9 and its chart value
  `openshell.insecureWithoutNetworkPolicy` (now ignored if set). The boot probe of the Kubernetes API
  could not tell a cluster without enforcement from a sandbox policy that lets internal addresses
  through the OpenShell supervisor, and refused intermittently on a cluster that enforces
  NetworkPolicy. NetworkPolicy enforcement stays an install prerequisite.

## [0.6.9] - 2026-11-07

### Changed

- **Breaking:** a sandbox refuses to start on a cluster that does not enforce NetworkPolicy. Its egress
  lockdown is the OpenShell driver's deny-all NetworkPolicy, so without enforcement it reaches any IP.
  At boot the execution role tries a TCP connect and TLS handshake with the Kubernetes API Service for
  up to about 30 s and exits if every attempt completes. Installs on such clusters must set
  `openshell.insecureWithoutNetworkPolicy=true`.
- Runtime logs are JSON throughout. Library INFO lines (MCP and A2A per-request messages) are no longer
  printed, and the MCP SDK's `ClosedResourceError` traceback logged on every normal session close is
  dropped. Error records carry a redacted `traceback` field.
- The reference mesh policy (`docs/mesh/openshell-mesh.yaml`) lets kubelet probes through `allow-hbone`:
  under Istio ambient they come from a fixed link-local address, and an enforcing cluster otherwise
  fails the gateway and supervisor probes.

## [0.6.8] - 2026-11-06

### Fixed

- The operator accepts open-egress hosts such as `**.*.*` again. v0.6.7 refused any host whose
  top-level label was a wildcard or not a domain label, so Agents with such a rule got no sandbox and
  their runs failed with `WorkspaceUnhosted`. Hosts like `127.1`, `0x7f.0x1` and
  `kubernetes.default.svc.*` pass the operator again too, as they did before v0.6.7: internal
  destinations of these hosts are left to the sandbox proxy. Literal internal IPs, dotless hosts and
  `.svc`, `.cluster.local` and `.local` hosts are still rejected.

## [0.6.7] - 2026-11-06

### Added

- `engine.type: pi` runs an agent on PiG, the Go port of pi, driven over pi's JSONL RPC, as a lighter
  alternative to opencode: 44-46 MB RSS against 302-305 MB for `opencode serve` on the same agent and
  prompts. `opencode` stays the default. See `docs/local-agent.md#engines` for how the two differ.
- `ghcr.io/ackstorm/ach-agent-execution-pi`, the sandbox image for `engine.type: pi` (PiG at
  `/opt/pi/pig`), 181 MB against 368 MB for `execution`. The sandbox policy allows that binary.
- `ach-agent run` supports `engine: pi`, and the `agent` image carries both engines.

pi takes OpenAI-type models only: Gemini and Anthropic types are rejected at config load. Its
`maxSteps` is soft and a run can overshoot by a turn or two.

### Changed

- **Breaking:** the chart refuses to render without `openshell.tls.secret` unless
  `openshell.insecurePlaintext=true` acknowledges the plaintext gateway. Existing plaintext installs
  must add that flag on upgrade.
- The release workflow claims the version tag before publishing anything, so a second run for the
  same version from a different commit fails instead of overwriting the registry. `rc/**` branches
  can only publish pre-release versions.

### Fixed

- The session idle timer resumes after a control-pod restart: `/sessions/{ref}/info` reports
  `idleSeconds`, so a restart no longer gives every open session a fresh timeout and keeps its memory
  alive. Control and execution images must be the same release for this to apply.
- The operator derives the reserved facade egress host from the Agent identity, not from the endpoint
  the control pod reports, and rejects extra egress hosts ending in `.local` or with a wildcard or
  non-letter top-level label.
- Engine config a tool planted in the session HOME no longer reaches later turns: opencode's global
  `opencode.json(c)` and pi's `SYSTEM.md` and `APPEND_SYSTEM.md` are removed before each launch.

## [0.6.6] - 2026-11-06

### Added

- A2A channels advertise `capabilities.streaming: true` and serve `SendStreamingMessage` (SSE).
- A2A runs get an `inform_user(message)` MCP tool (`progress` facade, mounted per run at
  `/facades/progress/runs/{invocationId}/`): each call reaches the A2A caller as a `WORKING` status
  update with the message, before the final reply. Other channel types never see the tool; exclude it
  with `capability.filter.exclude.tools: [inform_user]`.

### Changed

- OpenCode's `execute` tool (Code Mode) is denied whatever `engine.denyTools` lists, so MCP tools
  are called directly. To keep it, set `OPENCODE_ENABLE_CODE_MODE` to `1` or `true` in `spec.env`
  and list it in `engine.forwardEnv`. `ach-agent run` manifests no longer default `denyTools` to
  `[execute]`.
- `StartRequest` carries `informUser`; control and execution images must be the same release.
- A2A prompts append a `<progress_updates>` paragraph telling the agent to use `inform_user` for
  milestones before slow work.

## [0.6.5] - 2026-11-06

### Fixed

- The facade listener keeps idle connections open for 360 s (uvicorn's default is 5 s), longer than
  opencode's Bun connection pool keeps an idle socket (more than 240 s, less than 330 s). The OpenShell
  supervisor reuses a sandbox's pooled connection onto the facade connection and, when the facade had
  already closed it, drops the sandbox side without a response: parallel MCP tool calls after a few
  seconds idle failed with `ECONNRESET: The socket connection was closed unexpectedly`.

## [0.6.4] - 2026-11-06

### Fixed

- opencode in an OpenShell sandbox no longer caches DNS answers (`BUN_CONFIG_DNS_TIME_TO_LIVE_SECONDS=0`).
  Bun kept a policy-DNS synthetic IP for 30 s after its mapping expired (the record TTL, 5 s for cluster
  names), and the supervisor refused the connect: MCP tool calls failed with
  `EACCES: Was there a typo in the url or port?`.

## [0.6.3] - 2026-11-05

### Added

- `model.limits` (wire `capability.ach.model.limits`, Agent `spec.ach.model.limits`, `ach-agent run`
  manifest `model.limits`) works with an ACH environment too, and wins over the limits ACH hydrate and
  the gateway's model info report. For a model whose real window differs from what ACH reports.

### Changed

- A new sandbox on another runtime release than its control runs with a warning instead of failing
  the turn. A sandbox of another release found at control start is still retired.

### Fixed

- A sandbox the gateway no longer knows while its pod still runs is removed. A create that
  failed half-way left the Sandbox object and its pod behind, and the operator waited for that
  pod to go forever, so every run failed with `WorkspaceUnhosted`. Needs `delete` on
  `agents.x-k8s.io` `sandboxes` (in the chart's ClusterRole).
- `workspace.persistence.type: volume`: a session whose opencode died is suspended, so its next sandbox
  resumes the conversation instead of starting fresh, and its `sessionSuspend` hook runs.
- `workspace.persistence.type: volume`: a session manifest of another Agent or Workspace starts the
  session fresh instead of failing the turn.

## [0.6.2] - 2026-11-05

### Security

- The facade token moves from the URL path to the `x-ach-facade-token` request header. The
  OpenShell supervisor logs every request path, so the token reached pod logs and anything
  that collects them, where it worked as a bearer credential with no expiry. Rotate
  `ACH_SANDBOX_KEY` after upgrading to invalidate tokens already logged.

### Changed

- Facade URLs have no `/t/<token>` segment, and control and execution images must be of a
  release with this change: an older execution image rejects the catalog at `/configure`.
  Hook scripts get `ACH_MCP_HEADERS` next to `ACH_MCP_URLS`; `ach-mcp` reads both.

### Fixed

- AgentProfile `sandbox.openShell.resources` reach the sandbox pod. They were sent as a flat
  `{cpu, memory}` map, which the gateway does not read as limits, so the pod ran with no
  requests or limits. They are now sent as `limits`; the requests default to the same values.
- An extra egress endpoint with a `ports` list and no `port` is accepted when the list leaves
  out the facade port. The unset `port` was read as "any port" and every such rule was
  rejected as reaching the facade.

## [0.6.1] - 2026-11-05

### Fixed

- A managed agent can read its own skills. The file tools were denied everything outside the
  workspace except OpenCode's output files, and skills are hydrated under
  `$XDG_CONFIG_HOME/opencode/skills`, so every `read` of a skill's references or scripts failed
  with `external_directory` and the agent searched the filesystem or copied the skill instead.

## [0.6.0] - 2026-11-05

### Added

- `ach-agent run` manifests take `limits`, merged over the run defaults (`maxSteps: 50`,
  `maxInvocationSeconds: 600`). A long task hit 50 steps, where opencode disables tools and the turn
  ends silently.
- `ach-agent run` manifests take `denyTools`, and `execute` is denied by default.

### Changed

- OpenCode's built-in `opencode.browser` and `opencode.tools` (session, worktree and model-search
  tools) plugins are off; the MCP resource tools stay. To keep one, set
  `OPENCODE_ENABLE_BROWSER_TOOL` or `OPENCODE_ENABLE_MANAGEMENT_TOOLS` to `1` or `true` in `spec.env`
  and list it in `engine.forwardEnv`.
- OpenCode's built-in `opencode.tool.question` plugin is always off: nobody answers a managed turn.

### Fixed

- Web search uses Exa by default, so the first `websearch` call no longer waits a minute on an
  unanswered provider question and fails.

## [0.5.0] - 2026-11-05

### Added

- `workspace.persistence.type: volume` keeps a Workspace's files and session HOMEs on one PVC per
  Workspace (`volume: {size, storageClassName, accessMode}`), created by the operator in the sandbox
  namespace and mounted at `/sandbox/ach` by every sandbox of that Workspace. An idle session stops
  opencode and keeps its HOME; the next sandbox resumes the conversation from it, with no S3 transfer.
  A PVC with no sandbox for `retentionDays` is deleted, session HOMEs unused for
  `session.retentionDays` are deleted at Workspace start, and deleting the Agent deletes its PVCs.
  Requires the gateway mTLS (`openshell.tls.secret`; otherwise `WorkloadApplied=False`,
  `VolumeRequiresGatewayTLS`), `server.drivers.kubernetes.allowDriverConfig: true` on the OpenShell
  gateway, and a StorageClass with `reclaimPolicy: Delete` writable by uid/gid 10001.
- Opt-in OpenShell gateway mTLS. With `openshell.tls.secret` (the Secret of the operator's
  `OU=operator` client certificate) the operator reads that Secret at start, calls an `https://`
  gateway with it, and control pods verify the sandbox service path against its CA only
  (`ACH_AGENT_EXECUTION_CA_PEM`); the gateway refuses every other API caller. The chart grants
  `get` on that one Secret and renders no cert-manager objects. `docs/mesh/openshell-pki.yaml`
  is the reference PKI, all in `openshell` with namespaced Issuers (every certificate 262800h).
  Without the value nothing changes. Sandboxes created before the switch answer 412 until control
  recreates them.

### Fixed

- An ACH Agent on a LiteLLM model-group alias (such as `ackstorm.smart`) takes its context and
  output limits from the `/platform/hydrate` response (ACH v1.1.3 or later), where the alias is
  resolved. `/v2/model/info` does not resolve aliases, so such an Agent ran on opencode's default
  context window and compacted early.
- `ach-agent run` with an `ach-cli login` profile no longer fails at boot with `400 missing_environment`:
  skill, prompt and artifact downloads send `x-ach-environment`, which ACH requires for a login (`pk_`)
  credential.
- `ach-agent run` reports why the warm-up run failed (for example `mcp tools unavailable: <server> (…403…)`)
  as soon as it fails, instead of `timed out waiting for the TUI session to start` after three minutes.

## [0.4.0] - 2026-11-04

### Added

- `spec.ach.mcpServers` (wire `capability.ach.mcpServers`) adds MCP servers to the ones an ACH Environment
  serves, with the same shape as `standalone.mcpServers`. They authenticate with their own headers and are
  never sent the ACH credential; a name the Environment already serves fails the control pod's start.
- `ach-agent run` runs standalone agents: a manifest without `ach:` names its model endpoint (`baseUrl`,
  `headers`, `limits`) and its `mcpServers`, with no ACH or ach-cli. Header secrets are `{env: NAME}`,
  passed into the container by name. With `ach:`, `model` also takes `params` and `thinking`, and
  `mcpServers` adds servers to the environment's. A model on the host is reachable as
  `host.docker.internal`.

### Changed

- **Breaking:** `workspace.persistence` is `{type: none | s3, retentionDays}` on Agent and AgentProfile
  and on the `workspace-v3` wire; one `type` covers the Workspace files and the session HOMEs. It
  replaces `workspace.persistence.enabled` and `workspace.session.persistence`: the runtime rejects
  both; on the CRDs `persistence.enabled` without `type` is rejected, while `session.persistence` is
  an unknown field that strict clients (kubectl's default) reject and lax ones drop.
  `none` is the default; `retentionDays` is required with `s3` and forbidden with `none`.
  `workspace.session.retentionDays` (optional, at most `persistence.retentionDays`, default equal to
  it) is the session snapshot retention. An Agent's `persistence` block replaces the profile's whole.
  `type: volume` is rejected as not implemented yet. Migrate `{enabled: true, retentionDays: N}` to
  `{type: s3, retentionDays: N}` and `{enabled: false}` to `{type: none}`.
- **Breaking:** sandboxes keep the Workspace in `/sandbox/ach/workspace` and the session HOMEs in
  `/sandbox/ach/sessions` (were `/workspace` and `/var/lib/ach-runtime/sessions`), and the sandbox
  policy grants read-write on `/sandbox/ach` in their place. S3 session snapshots saved under
  `/workspace` are not resumed: each such conversation starts fresh once, and its next save replaces
  the snapshot. Workspace file archives restore unchanged. Every Agent's `configVersion` changes, so
  control pods restart once. Upgrade the operator and every AgentProfile's `sandbox.openshell.image`
  to this release together: the sandbox read-write paths come from the operator's policy, and either
  one alone leaves sandboxes unable to write their Workspace.
- OpenShell 0.1.7: the e2e cluster, `docs/install.md` and the operator's Go SDK (`sdk/go/v0.1.7`). Since
  OpenShell 0.1.6 a sandbox opens the control connection to its supervisor; under Istio ambient that
  needs the new `allow-hbone-egress-to-supervisor` NetworkPolicy in `docs/mesh/openshell-mesh.yaml`
  (egress TCP 15008 from sandbox workloads to supervisors). Apply it before upgrading the gateway, or no
  supervisor becomes ready. OpenShell 0.1.6 also refuses to start without authentication unless
  `server.auth.allowUnauthenticatedUsers` is set, as the reference values do.

## [0.3.3] - 2026-11-03

### Added

- `channels[].rateLimit` caps the invocations a channel admits per sliding hour: `maxPerHour` for the whole
  channel and `perKey` entries for a rendered key template (e.g. per ticket or per sender). Webhooks over a
  limit get HTTP 429 naming it, before any model call; the rejection is an `ACH operational anomaly` of kind
  `limit_reached`. Protects agents from loops such as an e-mail auto-responder replying to itself.
- `ach-agent run --config agent.yaml` runs an agent locally from a short YAML manifest (environment, model,
  prompt, excluded tools) in one container, with the current directory as its workspace, and attaches the
  opencode TUI to its session. Published as `ghcr.io/ackstorm/ach-agent-local`. See `docs/local-agent.md`.

### Changed

- The engine is OpenCode `@opencode/cli@2.2.5`. A session snapshot restores whatever OpenCode release saved it:
  the engine version is recorded in the snapshot manifest but no longer has to match, so sessions saved by
  2.0.20 keep working after the upgrade.
- Dashboards. Cost panels read the billed spend from LiteLLM (`litellm_spend_metric_total`) by the agent's
  environment; the engine estimate, which counts gateway model aliases as 0, stays as one comparison stat.
  Counts are over the selected range instead of per-second rates, and "ACH — Agents" opens on a per-agent
  table (runs, failure rate, p95, spend, LLM errors, anomalies, last run).
- Every dashboard filters by agent: `$agent` is multi-select with All by default, listed from the agents with
  a series in the time range. The sandbox pod start, storage server and operator panels are not per agent.
- "ACH — Agent Detail" adds a Health row (credential expiry and refresh failures, unpriced invocations, LLM
  gateway errors by status, webhook-script failures, control pod CPU, memory and restarts) and a Logs row
  (invocations, anomalies, errors) from a Loki datasource.
- "ACH — Start-up" adds the storage server (unsynced files, last S3 reconcile, volume, bucket size, S3
  traffic) and the operator (reconciles, reconcile p95, work queue depth).

## [0.3.2] - 2026-11-02

### Added

- Start-up metrics. The control pod exports `ach_agent_workspace_start_seconds{phase}`: `place`, `probe`,
  `restore`, `boot` (the execution unit's own start, which it reports on `/healthz` as `bootSeconds`), `ready`
  (from admission until the unit takes the run) and `total`. The operator exports
  `ach_agent_sandbox_start_seconds{phase}` from the workload pod's conditions: `volume`, `init` and
  `container`.
- With `metrics.podMonitor.enabled`, PodMonitor `ach-agent-operator`; with `metrics.dashboards.enabled`, the
  Grafana dashboard "ACH — Start-up".

### Changed

- The execution image is 322 MB instead of 1.1 GB: it ships only opencode's native binary, without node or
  opencode's other platform builds, and only the execution unit's Python dependencies. The control pod's
  dependencies are the `control` extra of `ach-agent-runtime`. The sandbox network policy no longer lists
  `node` as a runtime binary.

### Fixed

- The storage server's reconcile container rewrites each imported `registry.json` through the S3 gateway after
  a fresh `remote.mount` (a new volume or a `MOUNT_PATH` change). SeaweedFS 4.51 otherwise compares a PUT's
  `If-Match` with the cached chunk's hash, so every compare-and-swap of that registry failed and the
  Workspace never activated.
- Writer acquisition fails at once, naming the storage inconsistency, when storage rejects a registry write
  whose ETag is unchanged on re-read, instead of retrying twenty times and reporting contention.

## [0.3.1] - 2026-11-02

### Changed

- The execution unit starts in about half the time: the image ships compiled bytecode, and start-up
  no longer reads package metadata or imports the `mcp` package. Importing the unit takes 2.3 s instead
  of 4.9 s (local measurement); the image grows 20 MB.

### Fixed

- The operator removes a sandbox's ExternalName Service once its workload pod is gone; the Service of a
  Workspace no longer listed used to outlive its sandbox.

## [0.3.0] - 2026-11-01

### Added

- The control pod exports `ach_agent_storage_persist_seconds` and `ach_agent_storage_restore_seconds`
  histograms, labelled `domain` (`workspace` | `session`) and `outcome` (`ok` | `error`, and `empty` for a
  restore with nothing stored). A save includes the archive export and its retries; a restore includes
  streaming the archive into the sandbox.
- Storage server: with a bucket configured and `storage.enabled` (the default), the chart runs a SeaweedFS 4.51
  storage server, `ach-agent-storage`, in each namespace of `storage.namespaces`. Control pods save to it and it
  replicates to the bucket in the background, so a save no longer waits for the bucket. The bucket is AWS S3
  (`storage.remote.type: s3`, `runtime.storage.s3.*`) or Google Cloud Storage (`gcs`,
  `storage.remote.gcs.*`, Workload Identity or a key Secret). Only the storage server holds cloud credentials.
  `make storage-smoke` checks a SeaweedFS version in docker against a stand-in S3 and a fake GCS. See
  `docs/install.md`.
- Storage server observability: with `metrics.podMonitor.enabled`, PodMonitor `ach-agent-storage`; alerts
  `ACHAgentStorageDown` (needs kube-state-metrics), `ACHAgentStorageDiskHigh` and `ACHAgentStorageSyncLagging`.

### Changed

- The runtime uses path-style S3 addressing whenever `runtime.storage.s3.endpointUrl` is set.
- Workspace and session restores extract in one streaming pass, and snapshots are not re-read after packing.
  Snapshots compress at gzip level 1 instead of 9. On prod (312 MB Workspace) a save takes about 15.5 s
  instead of 18.3 s and a restore about 5.5 s instead of 6.2 s; source code compresses three times faster
  for slightly larger snapshots.
- **Breaking:** with a bucket configured, the chart requires `storage.namespaces` (the Agent namespaces) and
  `runtime.storage.s3.region`, or `storage.enabled=false` to keep control pods on S3 directly. The bucket's
  Pod Identity association moves from the control ServiceAccount to `<namespace>/ach-agent-storage`.
- Each storage server mounts only its namespace's folder of the bucket (`{prefix}/{namespace}`).
- The release workflow also publishes from `rc/**` branches, and a version with a suffix (`0.3.0-rc.1`)
  becomes a GitHub pre-release.

### Fixed

- A read timeout from S3 (botocore `ReadTimeoutError`) is a transient failure that the close retries; it used
  to fail the save with `AttributeError: 'NoneType' object has no attribute 'get'`.

## [0.2.1] - 2026-10-31

### Changed

- Sandbox pods carry the Agent's name: `default--k8s-doctor-9f0456d5` instead of `default--ws-9f0456d5c4515e7e`
  (up to 10 characters of the name plus 8 of hash, within OpenShell's 19-character limit). Sandboxes created by
  earlier versions are still found and removed under their old name.
- The operator creates its configured OpenShell workspace (`openshell.workspace`) on the first sandbox create; a
  workspace the gateway does not know holds no sandboxes.
- The e2e cluster runs sandboxes in the OpenShell workspace `ach-agent`, so pods read `ach-agent--<agent>-<hash>`.
  Changing `openshell.workspace` on a running install leaves sandboxes of the previous workspace unseen: delete
  them once (`<old-workspace>--*` pods in the sandbox namespace).

### Fixed

- opencode no longer checks for updates inside the sandbox (`OPENCODE_DISABLE_AUTOUPDATE=1`); the sandbox allows
  egress only to the facades, so the check was a denied `connect`.

## [0.2.0] - 2026-10-30

### Changed

- **Breaking:** `Agent.spec.ach.keySecretRef` is `spec.ach.keyFrom.secretKeyRef`. Rename it in every ACH Agent
  when upgrading. Until it is renamed the operator reports `WorkloadApplied=False` (`RenderFailed`) for that Agent
  and leaves its running pod untouched.
- `make -C runtime test-unit` starts the S3 fixture the storage and e2e tests need, so the full suite runs
  locally as it does in CI.

### Added

- `spec.ach.keyFrom.tokenExchange.identity` (needs ACH 1.1.0 and a `KeyGrant` naming the control ServiceAccount
  as `<namespace>/<serviceaccount>`): the control pod keeps its usual ServiceAccount, exchanges a projected token
  of it (audience `spec.ach.baseUrl`) at `POST /platform/keys/exchange` for a short-lived ACH credential and
  renews it at 80% of its lifetime with no failed request; a credential ACH rejects is re-exchanged once and the
  request retried. `environment` and an `https://` `baseUrl` are required. No Secret is referenced, so rotation
  never restarts the pod. Every pod running a namespace's control ServiceAccount can use every grant that names it.
- Metrics `ach_agent_key_refresh_failures_total`, `ach_agent_key_expiry_timestamp_seconds`,
  `ach_agent_key_lifetime_seconds` and the alert `ACHAgentKeyRefreshFailing`.

### Fixed

- After a control restart, a unit's facade call that arrived before adoption published the unit was refused
  with 403 and failed its run; it now waits up to 30 seconds for adoption.

### Security

- The control process scrubs ACH keys (`ek-`/`ek_`), compact JWS (ACH credentials, ServiceAccount tokens) and
  provider keys from every log record, tracebacks included, and from A2A tool errors returned to the engine.

## [0.1.5] - 2026-10-30

### Fixed

- A stream still open when the facade listener's shutdown grace expires (an engine's MCP stream)
  ends its response instead of logging an `Exception in ASGI application` traceback.

### Added

- `make chart-test` unit-tests the chart's alert rules with `promtool test rules`.

## [0.1.4] - 2026-10-30

### Added

- The chart ships the agent Grafana dashboards (`metrics.dashboards`), a PodMonitor for the control
  pods (`metrics.podMonitor`) and the agent alert rules (`metrics.prometheusRule`), all off by default.
  The dashboards list every agent from `ach_agent_info`, so an agent with no event since its pod
  started is still listed.

### Fixed

- MCP tools reach the model without the gateway's `<server>.` prefix: opencode adds its own server
  prefix, so models saw `mcp-x_mcp-x_tool` and kept calling names that do not exist. A call by the
  bare name is mapped back to the upstream name; `filter.exclude.tools` keeps the `<server>.<tool>`
  form and also blocks a bare call to an excluded tool.
- The A2A agent card is spec-compliant: named after the agent, one skill per channel and a supported
  interface at the URL the caller reached, so the A2A SDK no longer warns on every card fetch.
- A terminating control pod no longer waits up to `shutdownTimeoutSeconds` for the engines' MCP
  streams: once every admitted run has settled, the facade listener closes within 5 seconds.

## [0.1.3] - 2026-10-30

### Fixed

- Adopting an execution unit of another runtime release after an upgrade retires it with an INFO log
  instead of an ERROR and a `pod_unhealthy` anomaly; a freshly placed unit of another release is still
  an anomaly.
- Workspace adoption and activation failures log their redacted reason, not only the error type.

## [0.1.2] - 2026-10-30

### Fixed

- A failed handoff hook reports the last lines of its stderr (secrets redacted) and the start-failure
  log names the channel and event id.
- The control process no longer logs every internal HTTP request (httpx INFO).

## [0.1.1] - 2026-10-30

### Changed

- `expose.route.path` is stripped before the control pod (HTTPRoute `URLRewrite ReplacePrefixMatch: /`), so
  several Agents can share one hostname under different path prefixes.

## [0.1.0] - 2026-10-29

ach-agent is now one project: CRDs, operator and runtime in one repository, released together under one
version. The runtime has its own history in `runtime/CHANGELOG.md`.

### Added

- `Agent` and `AgentProfile` custom resources (`ach-agent.ackstorm.ai/v1alpha1`) and the operator that renders
  them into a control unit (a one-replica StatefulSet) plus OpenShell sandboxes, one per execution unit.
- Standalone mode: model and MCP servers configured directly on the Agent, with per-endpoint headers from
  Secrets or literals. ACH is an optional capability server (`capabilityServer: ach`).
- Helm chart `oci://ghcr.io/ackstorm/charts/ach-agent` for the operator: CRDs, ClusterRole, a namespaced leases
  Role and a ValidatingAdmissionPolicy that limits the operator to Opaque Secrets named `ach-agent-key-*`. The
  chart refuses to install without `openshell.gateway` and `openshell.mesh: istio-ambient`.
- Images `ach-agent-operator`, `ach-agent-control` and `ach-agent-execution`.
- A kind-based end-to-end suite (`make e2e`) with Istio ambient, a real OpenShell gateway and stub upstreams.
- `docs/install.md` and a reference mesh policy for the OpenShell gateway.

### Changed

- The runtime config schema is `workspace-v3` (a `capability` block replaces the root `model` and `ach`).
- OpenShell is the only execution sandbox; an Istio ambient mesh is required.
