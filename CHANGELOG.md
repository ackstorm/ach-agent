# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
