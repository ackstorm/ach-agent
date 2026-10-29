# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [unreleased]

### Changed

- License changed from Apache-2.0 to MIT.

### Fixed

- A failed Harness lifecycle tick logs its redacted cause (`consecutive_failures=N cause=<Type>: <message>`, plus traceback frames on the first failure of a streak) and raises the anomaly kind `lifecycle_tick_failed` instead of `run_outcome_unknown`. Alerts keyed on `run_outcome_unknown` no longer fire for it.
- A failing tick backs off along the 1 s, 2 s, 5 s tick ladder (capped as before) and resets only after a successful tick, instead of retrying at about 1 Hz.

## [0.2.2] - 2026-10-28

### Added

- `workspace.readyTimeoutSeconds` (optional, default 30): the startup budget of an execution unit, previously a fixed 30 s.

### Removed

- Reading the 0.1.x registry key `writer_pod_uid` (added in 0.2.1). Every production registry was migrated to `writer_instance_id` on 2026-10-28; a registry still in the old shape fails restore again.

## [0.2.1] - 2026-10-28

### Fixed

- Restore reads storage registries written by ach-runtime <= 0.1.22 (`writer_pod_uid`) instead of failing with `RestoreFailed: registry object is missing fields: ['writer_instance_id']`. The next writer rewrites the registry with `writer_instance_id`.

## [0.2.0] - 2026-10-28

### Added

- `agent` role (`ach-runtime agent`, Dockerfile target `agent`): one process runs control and the execution unit through the in-process `LocalSource`. For laptops, tests and CI; not for production.
- `examples/hosts/compose/`: reference single-unit Docker Compose host (SeaweedFS, ACH stub, agent).
- `make compose-e2e` and a CI job that runs the compose example, recreates the stack and proves the Workspace is restored from S3.

### Changed (breaking)

- Host-agnostic runtime: the Harness no longer uses a Kubernetes client or needs Harness RBAC. It coordinates Workspaces through a `WorkspaceSource`; the hosted source serves the new `/host/v1` API that a host uses to list wanted Workspaces, register started instances and report losses.
- Config schema is `workspace-v2` (infrastructure fields removed); see `docs/schemas/ach-runtime-contract-v2.md` and its migration note.
- The instance header is `X-ACH-Instance-Id`; each process mints its own instance ID. `ACH_POD_UID` is gone.
- Storage writer identity is renamed: key `writer_instance_id`, metadata `writer-instance-id`.
- `__version__` now comes from installed package metadata (`importlib.metadata`): a source checkout needs `pip install -e .` in a venv.

### Fixed

- Skill hydration nests each skill under `skills/<name>/` (SKILL.md at the archive root or one wrapper directory deep, as ach-cli does). Before, root-level archives were copied straight into `skills/`, so an Environment with two or more skills failed every run with "hydrated skills contain a duplicate path".

## [0.1.22] - 2026-10-16

### Added

- `engine.denyTools` hides native OpenCode tools (for example `question`, `execute`, `websearch`, `webfetch`) from an agent's model: each name becomes a permission deny rule on the managed agent. Control sends it only when set, so warm older execution pods keep accepting `/configure`.

## [0.1.21] - 2026-10-16

### Fixed

- A run with the built-in artifacts MCP server no longer fails before the send with `mcp tools unavailable: artifacts (McpError: Session terminated)`: the pre-send `tools/list` check probes the per-run artifacts URL the engine gets, instead of the bare token URL the facade answers with 404 (regression since 0.1.18).

## [0.1.20] - 2026-10-15

### Fixed

- A workspace whose reactivation timed out before its pod answered `/healthz` (a slow cold start) is scaled back to zero and retried on the next run, instead of failing every later run with the retained `mini-harness health check timed out` error until the control pod restarts.

## [0.1.19] - 2026-10-08

### Fixed

- Control omits `limits.maxStallSeconds` from `/configure` unless the config sets it, so a 0.1.19 control can configure a warm 0.1.17 execution pod during a rollout instead of crash-looping on its 422.

## [0.1.18] - 2026-10-08

### Changed

- Each non-prerelease release also moves `latest` on `ach-runtime-control` and `ach-runtime-execution` to its images, pushed together with the version tag. Deploy by version tag; `latest` is a convenience.

### Added

- `limits.maxStallSeconds` (optional, default 300): a running invocation with no engine progress (no new message part or tool-state change) for that long fails with reason `stalled`, freeing its invocation slot instead of holding it until `maxInvocationSeconds`.

### Fixed

- Before the send, execution probes `tools/list` on every configured MCP server and retries a server that lists no tools or cannot be reached (3 tries, 2s then 6s apart, at most ~20s). If one still lists none, the run fails at once with `mcp tools unavailable: <server> (<reason>)` and never reaches the engine. Before, OpenCode listed tools once at connect, so a server still starting left the whole run silently without its tools.
- A headless run no longer hangs on a file tool call outside the workspace. OpenCode's default `external_directory` permission is `ask`, and nobody answers it in a headless run, so a `read` of e.g. the session's `opencode.config.json` stayed `running` until the invocation deadline. The managed agent now denies `external_directory` (`read`, `glob`, `grep`, `edit`...) except OpenCode's own `tool-output` and `shell` output files; the call fails at once and the model carries on. The `shell` tool is not covered by this permission.

## [0.1.17] - 2026-10-07

### Changed

- `capability.filter.exclude.tools` now also hides the excluded MCP tools from `tools/list` (JSON and SSE replies), so the model never sees them; calls stay denied as before. One MCP server can now serve a narrowed toolset to one agent, instead of a second server instance with its own OAuth store.

## [0.1.16] - 2026-10-07

### Added

- `routing.workspaceKey: session` gives each session its own workspace (key `[<default workspace>, <session key>]`).
- A2A request headers are template data: `event.headers.<lowercase-name>`, minus the channel's auth header, `authorization`, `cookie` and `proxy-authorization`. A caller-scoped A2A session is now declared, e.g. `sessionKey: "{{ event.headers.x-litellm-user-id }}:{{ event.a2a.contextId }}"`.
- `ACH_WORKSPACE_KEY` and `ACH_SESSION_KEY` in the `sessionStart` hook environment.

### Changed

- A generic webhook or queue channel without `routing.sessionKey` no longer loads.
- Generic webhook default workspace is `webhook:<channel>` (was one workspace per `eventId`).
- A full `limits.maxActiveWorkspaces` makes activation wait for a free slot until the invocation deadline instead of failing at once.
- `StartRequest` gains required `workspaceKey` and `sessionKey`: control and execution pods must run the same version.

### Fixed

- Control restores a workspace pod that Kubernetes recreated while control was down (e.g. node loss): at discovery it compares the S3 registry's `writer_pod_uid` with the live pod and runs the normal restore (archive + `workspaceStart`) when they differ. Before, the new pod kept an empty `/workspace` and `workspaceStart` never ran.

### Removed

- `handoff.scope: session` is rejected; only `event` is accepted.
- The legacy `ACH_SESSION_KEY` (`<projectId>:<iid>`) from `webhook-script`; `ACH_EVENT_*` carries project and MR/issue.

## [0.1.15] - 2026-10-06

### Changed

- Workspace StatefulSets, Services and pods are named `agent-<agent name>-<20 hex>` instead of `ach-ws-<agent name>-<20 hex>`, so they sort next to the agent's control pod `agent-<agent name>-0`. Agent names up to 25 characters (was 24) are kept whole. An existing `ach-ws-*` StatefulSet is not adopted: its Workspace stays failed with a logged conflict until the StatefulSet is deleted, then it is recreated under the new name. Requires an ACH release whose ACHAgent CRD rejects agent names ending in `-<20 hex>`.

## [0.1.14] - 2026-10-06

### Fixed

- Control omits the hook keys added in 0.1.13 (`workspaceStart`, `workspaceStop`) from `/configure` while they are unset. A warm 0.1.12 execution pod rejected them (422), so a 0.1.13 control could not reconnect to it and crash-looped at startup.

## [0.1.13] - 2026-10-06

### Added

- Lifecycle hooks `workspaceStart` (after the workspace restore on a fresh, reactivated or recovered pod) and `workspaceStop` (on a clean idle close, before the workspace snapshot), next to the session hooks. Both are best-effort: a failure raises an anomaly and never blocks the workspace.
- `sessionStart` is a run gate: exit 78 ends the invocation with the new outcome `skipped` without calling the engine, any other non-zero exit is a recorded `failed` run, and its stdout is appended to the prompt.
- `ach-mcp call <server> <tool> '<json>'` in the execution image: hook scripts call MCP tools as the agent through the pod's facade URLs (`ACH_MCP_URLS`).
- Metric `ach_agent_runs_total{channel,outcome}`; skipped runs do not count as turns. A2A replies to a skipped run with state rejected.

### Fixed

- The facade proxy returns 499 instead of raising when the client disconnects before upstream headers, which logged "Exception in ASGI application" twice per run for agents with MCP servers.

## [0.1.12] - 2026-10-05

### Changed

- MCP servers are registered with OpenCode code mode off (`codemode: false`). The model now sees each MCP tool directly as `<server>_<tool>` and calls it with JSON arguments, instead of writing JavaScript for the `execute` tool and discovering signatures with `search()`. Agents keep their tool lists small, so code mode only added steps and tokens.

## [0.1.11] - 2026-10-05

### Fixed

- Workspace archives are now created in the system temporary directory (`tempfile.gettempdir()`), outside the packed Workspace. Before, they were created in the Workspace's parent directory. With the default `/workspace` that meant `/.ach-archive-*`, which fails for non-root execution.
- A failed Workspace archive export now carries the execution side's error `type` and `message` on the same `HTTPStatusError`. Workspace and session persistence failures log their error type and cause through the existing redaction. Retry policy, HTTP metadata and alarm reasons/counters are unchanged.

## [0.1.10] - 2026-10-04

### Fixed

- A Workspace StatefulSet that the Harness owns but whose governing Service is missing no longer fails every Start with `WorkspaceConflict`. The Service is recreated (one warning with `workspace_ref` and `workspace_name`) from the same body a new Workspace gets, on ensure, recover, startup inventory and reconnect. A StatefulSet that is not provably owned still raises the conflict. Startup inventory used to latch a failure for an owned, podless StatefulSet without Service; it now recreates the Service.
- The start-failure reason keeps a Workspace object name (`ach-ws-<name>-<20 hex>`) instead of printing `[redacted]`, and short `ek-`/`sk-` tokens are now redacted too.

## [0.1.9] - 2026-10-04

### Changed

- Kubernetes pod waits poll every 0.25 s (backing off to 1 s) instead of every 50 ms. The lifecycle tick backs off to 5 s while a workspace is idle (never more than a tenth of the smallest idle timeout) and returns to 1 s on any activity. The run watch polls every 0.25 s for the first 5 s of a run, then backs off to 1 s. Every request is still made and logged.

### Fixed

- The start-failure reason is now part of the `run start failed: <reason>` log message; before it was passed only as a log field the control process never printed. `Workspace recovery failed` and the legacy-name conflict messages now also carry the workspace reference, name, phase, error type and Kubernetes status.

## [0.1.8] - 2026-10-04

### Changed

- Execution containers run as uid/gid 10001 (`runAsNonRoot`, `fsGroup`, no privilege escalation, all capabilities dropped, `seccompProfile: RuntimeDefault`, `enableServiceLinks: false`, no service account token). Workspace pods also carry `ach.ackstorm.ai/agent=<agent name>` when the name is a valid label value.
- Workspace StatefulSets, Services and pods are named `ach-ws-<agent name part>-<20 hex>` (name up to 24 chars as is, longer names shortened with a hash; at most 52 chars) instead of `ach-ws-<uid>-<20 hex>`. **Breaking:** workspaces with the old name are reported as an unresolved conflict at startup and must be deleted by hand.
- An existing workspace StatefulSet is recreated when the rendered pod template changes (`runtime.ach.ackstorm.ai/template-hash` annotation), only when the workspace is idle.
- The control image runs as uid/gid 10001 and every temporary file of the control process lives under `TMPDIR`, so the operator can mount a single writable volume. Handoff and webhook-script children now inherit `TMPDIR`.
- Harness logs the redacted, length-capped failure reason (`reason`) when a run fails to start.

### Fixed

- A workspace whose StatefulSet, Service and pod are deleted while the Harness is running is recreated on the next event instead of failing until the control pod restarts. A run in flight on the deleted workspace ends as `failed` (anomaly `pod_unhealthy`) instead of staying `run_outcome_unknown`.
- An agent with memory tools but no `prompt` block now gets the memory instructions applied to the model. The managed fixture defaults `promptCompose` to `append`, the documented `prompt.compose` default; agents with a `prompt` block are unchanged.
- Spike cancel probe no longer races on an empty pid file.

## [0.1.7] - 2026-10-03

### Fixed

- Unhealthy Workspace recovery waits for owned replacement pods to disappear before restoring a fresh writer. UID-conflicting delete responses no longer permanently block recovery when the observed old pod has already gone; genuine ownership, authentication and restore failures still stop execution. Failure logs include safe phase/type/status diagnostics without exception content.
- Cancellation at or after the original invocation deadline consistently reports timeout failure. Concurrent cancellation callers join the same retained cleanup; an explicit cancellation claimed before expiry keeps its original outcome.

### Tests

- Added deterministic replacement-race coverage and a native OpenCode/S3 pod-loss test for latest-snapshot restoration and no accepted-run replay. Live Kubernetes acceptance is tracked separately.

## [0.1.6] - 2026-10-03

### Changed

- Webhooks return HTTP 202 after Harness admission instead of waiting for Workspace/OpenCode startup. Internal Started and execution outcomes remain owned by the Harness; admission still uses the existing non-durable in-memory queue. Queue, cron and A2A acknowledgement behavior is unchanged.

## [0.1.5] - 2026-10-03

### Fixed

- Memory, artifact and A2A MCP facades use the Harness's cluster-facing host setting instead of FastMCP's loopback default. Internal Kubernetes service hostnames no longer fail with HTTP 421 during native startup; workspace authentication is unchanged.

## [0.1.4] - 2026-10-03

### Fixed

- Native readiness no longer waits for an agent `system` that is not there. Only `prompt.compose: replace` sets it; the default `append` pushes the prompt through a session hook, and an agent without `prompt.system` keeps OpenCode's default prompt. Every such agent failed to start with `native readiness timed out at /api/agent/<agent>`.

## [0.1.3] - 2026-10-03

### Fixed

- `infrastructure.control.serviceAccount` accepts any DNS-1123 name, so an AgentProfile can run every Harness as one stable ServiceAccount (`controlServiceAccountName`). Execution stays `ach-execution-<uid>`. 0.1.2 rejected the stable name at startup.

## [0.1.2] - 2026-10-03

### Fixed

- Workspace StatefulSet names are now `ach-ws-<24 hex of agent uid>-<20 hex of workspace ref>` (52 chars). The 60-char name pushed the controller-revision-hash label past 63 chars, so Kubernetes rejected workspace pods.

## [0.1.1] - 2026-10-03

### Fixed
- Control boot no longer fails with `environment mismatch: requested ''` when the ACHAgent does not set `capability.environment`; an unset environment adopts the one the credential is bound to.

## [0.1.0]

First release of `ach-runtime`, the successor to `ach-agent`.

### Added
- Explicit workspace and session identities, lifecycles and persistence (S3 snapshots, artifacts).
- Channels + Harness control plane separated from per-workspace execution pods
  (one-replica StatefulSets, mini-harness); delivery is ACKed at accepted/Started.
- OpenCode v2 (pinned 2.0.20) initialized through the managed configuration plugin for models and MCPs.
- Control and execution images: `ghcr.io/ackstorm/ach-runtime-{control,execution}`.
