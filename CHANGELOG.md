# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

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
