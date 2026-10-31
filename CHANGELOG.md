# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Sandbox pods carry the Agent's name: `default--k8s-doctor-9f0456d5` instead of `default--ws-9f0456d5c4515e7e`
  (up to 10 characters of the name plus 8 of hash, within OpenShell's 19-character limit). Sandboxes created by
  earlier versions are still found and removed under their old name.

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
