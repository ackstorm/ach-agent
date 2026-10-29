# Contract v2 migration note (draft)

Status: draft, 2026-10-26, a companion to [ach-runtime-contract-v3.md](ach-runtime-contract-v2.md). Nothing here is implemented.

This note maps every infrastructure responsibility that contract v1 gives the runtime onto its owner under v2. Unlike the contract, it names the current host (the ACH operator on Kubernetes), because that is where the work moves.

Sources:

- `ach-runtime` at `e5376ff`;
- `ach` at `6f3c7580`;
- the OpenShell PoC in `spikes/openshell-poc/`.

## 1. What leaves the runtime

| Responsibility today | Runtime code | New owner |
|---|---|---|
| Kubernetes REST client: service-account token and CA, `statefulsets`, `services` and `pods` calls, `wait_ready`, `wait_deleted` | `harness/kubernetes.py` (188 lines) | Removed. |
| Execution StatefulSet and headless Service bodies, including: OnDelete strategy, probes, uid 10001, emptyDirs, ConfigMap mount, downward-API env, grace `max(infra, shutdownTimeout+15)` | `harness/workspaces.py` `_service_body`, `_statefulset_body` | Host. Built from the launch spec (v2 §5.5) plus the host's own agent definition. |
| Labels and annotations: `runtime.ach.ackstorm.ai/{agent-uid,workspace-name,workspace-ref,template-hash}`, plus `ach.ackstorm.ai/agent` | `workspaces.py:50-56` | Host. |
| ACHAgent `ownerReference` and owner checks: `_check_owned`, `_check_pod_owner` | `workspaces.py:189-270` | Host. Today Kubernetes garbage collection of that owner reference is the only cleanup on ACHAgent delete. |
| Template-hash rollout: delete and recreate an idle StatefulSet when its template changes | `workspaces.py:67`, `:592-600` | Host. |
| Naming legacy (`ach-ws-…`) conflict handling | `refs.py:49`, `workspaces.py:62`, `_hold_legacy` | Host, or dropped. |
| Restart rediscovery: `discover_owned`, `_run_bootstrap`, `_discover_*` | `workspaces.py:902-1100` | Split. Discovery moves to the host's inventory (v2 §5.3–5.4). Adoption itself (probe, reconnect, run recovery) stays in the runtime, fed by that inventory. |
| Recovery: delete the pod, confirm termination, recreate | `workspaces.py` `_do_recover`, `_wait_recovery_pod_absent` | Split. The runtime lists the Workspace `released`, the host stops the unit and confirms with `DELETE`, then the runtime lists it `running` again. |
| Facade ownership through the Kubernetes API: GET the StatefulSet and pod `-0`, compare the pod UID | `harness/control.py:214-229` `_owned_live` | Runtime, without Kubernetes: the Workspace has a serving instance in the Harness's own state. |
| Pod UID as execution identity: `ACH_POD_UID` from the downward API, `X-ACH-Pod-UID` | `execution/boot.py:21-43`, `execution/auth.py`, `delivery.py`, `transfer.py` | Runtime. The execution process mints the instance ID at start (v2 §5.6). |
| Storage writer identity `writer_pod_uid` | `storage/fencing.py`, `storage/s3.py:639`, `lifecycle.py` | Runtime, using the instance ID. Same field semantics: a new writer still raises the epoch. |
| `ACH_POD_NAMESPACE` | injected at `workspaces.py:373`; never read | Removed. |
| Config rule `infrastructure.execution.serviceAccount == ach-execution-<uid>`, and the other infrastructure fields | `config/schema.py:833-898` | Removed from the schema (v2 §7). |

What stays in `workspaces.py`, without Kubernetes:

- slot accounting (`_reserve`, `_release`, `_wait_for_slot`, `limits.maxActiveWorkspaces`);
- coalescing of concurrent `ensure` calls;
- receipt validation;
- the `WorkspaceRef` and `WorkspaceEndpoint` types.

The seam is the existing `WorkspaceManager` interface:

- `ensure(ref, deadline_at)`
- `close(ref)`
- `recover(ref)`
- `discover_owned()`

Note that `lifecycle.py` and `control.py` reach into `_active`, `_deleted_pods`, `_restore`, `_commit`, `_reconnect` and `_wait_mini_health`. Those private accesses become part of the seam, or get replaced.

Two Workspace sources implement it:

- **local:** in-process Workspaces, for the `agent` role;
- **hosted:** v2 §5 desired state plus registrations, for the `control` role.

Size estimate from the PoC review:

- Removable from the runtime: about 945 lines, namely `kubernetes.py` plus the Kubernetes-only and overridden parts of `workspaces.py`.
- New hosted source: of the order of the PoC backend (277 lines), minus all gateway calls, plus the host API routes.

## 2. What the ACH operator takes on

| Today in the operator | Under v2 |
|---|---|
| Role and RoleBinding `ach-harness-<uid>`: `statefulsets` get/list/create/patch/delete, `pods` get/list/delete, `services` get/create (`achagent_workload.go:150-173`) | Removed. The control SA needs no Kubernetes API access, so token automount can be turned off. The operator itself now holds those verbs for execution units. |
| ConfigMap `ach-execution-<uid>` with `bootstrap.json` (`achagent_workload.go:231-246`) | Replaced by `ACH_BOOTSTRAP_JSON` in each Workspace's `launch.env`, which the operator sets unchanged on the unit it creates. |
| Execution SA `ach-execution-<uid>` (no RBAC, no token) | Kept as an operator choice. It is no longer named in the runtime config. |
| `infrastructure.execution.*` rendered from `AgentProfile.spec.execution` (`render2.go:219-273`) | The operator uses the same profile fields directly on the execution units it creates. Only `infrastructure.facadeEndpoint` is rendered, and it should be the FQDN form (`ach-operator-fqdn.diff`). |
| Nothing creates execution pods (`achagent_workload.go:349-356`) | New: a Workspace reconciler per ACHAgent. It long-polls the control unit's `/host/v1/workspaces` with the HMAC host token derived from `ach-key-<uid>`. It then creates, replaces and deletes execution units, and `PUT`s or `DELETE`s registrations. The backend is a StatefulSet today and could be OpenShell or Agent Sandbox later; that choice is the operator's. |
| No finalizer; cleanup relies on garbage collection of owner references, which the Harness sets today | Execution units get the ACHAgent owner reference from the operator (Kubernetes backend). Backends that cannot be owned, such as OpenShell sandboxes, need a finalizer that selects by agent UID and tolerates retries and gateway outages (PoC evidence `13-agent-delete.txt`). |
| `WorkloadReady` read from control pod status only | Unchanged. Optionally also surfaces execution units' states from `/host/v1/workspaces`. |
| Config rendering and the vendored-schema drift test (`schema_test.go:42`) | Rendered for `workspace-v3` (decision D1). The fixture `testdata/pr-review-runtime.json` and the render tests change together with it. |

Operator behaviour that is unchanged:

- control StatefulSet, the 8080 event Service, readiness and liveness probes and env wiring (the 8081 Service gains `publishNotReadyAddresses`; the startup probe moves to `/healthz`);
- the `ACH_SECRET_*` aliases;
- `ACH_STORAGE_S3_*` and AWS SecretKeyRefs;
- `ACH_SANDBOX_KEY` generation;
- the NetworkPolicy for control;
- the config hash rollout.

## 3. Runtime work under v2 (not started)

1. **`agent` role:** Channels, Harness and facades plus the in-process mini-harness, using the local Workspace source.
2. **Hosted Workspace source and `/host/v1` routes:** desired state, registrations, `runtimeEpoch`, adoption through registrations.
3. **Self-minted instance ID:** `X-ACH-Instance-Id`, and the writer identity in storage.
4. **Facade ownership check:** based on Harness state.
5. **Bootstrap from `ACH_BOOTSTRAP_JSON`:** accepted as an alternative to the file.
6. **Schema `workspace-v3`:** infrastructure fields removed, `infrastructure.facadeEndpoint` added.
7. **Removals:** `harness/kubernetes.py` and the Kubernetes paths in `workspaces.py` and `control.py`.
8. **Reference single-unit host:** a container-stack example under `examples/hosts/`, outside the contract. It contains:
   - the runtime in the `agent` role;
   - SeaweedFS for S3, as in the root `docker-compose.yml` and the e2e;
   - a small ACH-compatible stub implementing v2 §3.3.1, in front of a configurable OpenAI-compatible model server.

   The stub generalises the e2e's in-process hydration fake (`tests/e2e/conftest.py` `catalog`). The example documents only what v2 §3 asks for.

Test seam: the e2e suite already runs execution apps in-process behind a fake cluster (`tests/e2e/conftest.py` `E2EStack`, `PodTransport`). The fake cluster becomes a fake host that drives `/host/v1`.

## 4. Found while mapping

- **The PoC put the gateway backend inside the Harness.** That contradicts v2 §1. The PoC remains valid as proof of the execution side only.
- **Cleanup differs by backend.** On today's StatefulSet path, cleanup on ACHAgent delete works only because the Harness sets the owner reference. With an OpenShell backend nothing removes the sandbox (PoC evidence `13`).
- **`controlEndpoint` is carried but never called** by the execution side.
- **`ACH_POD_NAMESPACE` is set but never read.**
- **No credential isolation in `agent`.** The engine shares the unit's user and filesystem with the Harness. That is why the contract makes isolation explicitly a host property (v2 §2, D5).
