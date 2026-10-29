# Plan 2 report: ach-agent operator

Branch `feat/operator` (worktree `ach-agent-operator`), based on `feat/bootstrap` 014a289. Nothing pushed.

## Commits

| sha | subject |
|---|---|
| 57f3757 | chore(operator): import the ACHAgent operator from ach v0.14.2 |
| d8d2209 | feat(operator): Agent/AgentProfile in ach-agent.ackstorm.ai rendering workspace-v3 (Tasks 2 and 3) |
| 08e7f1e | feat(operator): merged sandbox policy with reserved rules, resources |
| a9a8076 | feat(operator): gateway availability, client rebuild and a finalizer that waits |
| 138c64a | build(operator): image and CI |
| 1d952b5 | build(operator): mark grpc and protobuf as direct dependencies |

## Gates (final, run from `operator/`)

| gate | result |
|---|---|
| `make verify` (gen drift, `go vet ./...`, `go test -race` on api/host/openshell/render/cmd) | `EXIT=0`; `ok` host, openshell, render; no failures |
| `make test-envtest` (controller package, apiserver 1.36 binaries) | `EXIT=0`; `ok internal/controller` |
| `docker build -t ach-agent-operator:dev operator` | `EXIT=0` |
| `grep -rnE "ach.ackstorm.ai\|ACHAgent\|workspace-v2\|achagent-" --include='*.go' --include='*.yaml'` excluding `third_party` | `clean` |

The `go.mod` tidy in 1d952b5 came after the last gate run; `go build ./...` passed after it and it only drops two `// indirect` markers.
No kind cluster was touched; envtest only.

## Deviations from the plan

- **Tasks 2 and 3 are one commit.** The reconciler consumes the new API, so neither builds alone.
- **Package split.** `internal/host` owns the `Backend` interface, `UnitState` and the wire types; `internal/openshell` implements it. The plan's file split (backend in openshell, runner in host) was an import cycle. `Runner` takes a `NewBackend` factory instead of an `*OpenShell`.
- **Gateway outage marker.** `host.ErrGatewayUnavailable`; the openshell backend wraps every unavailable gateway error with it (`openshell.IsUnavailable` classifies SDK/gRPC/transport errors). The plan's `h.backend.(*OpenShellBackend).Sandboxes` host test is replaced by an in-memory backend returning that error.
- **Host tests** run against an in-memory `Backend`; the StatefulSet-backed ones were deleted with the backend.
- **Header-name rules** (reserved `host`, `content-length`, `transfer-encoding`, `connection`, `x-ach-*`; case-insensitive duplicates) are enforced in `render`, not CEL: a CEL rule over map keys exceeds the apiserver cost budget. `maxProperties`/`maxItems` bound the maps.
- **Policy document field names** are the proto ones (`filesystem`, not `filesystem_policy`). Strict decoding rejects the plan's spelling; the plan's rejection tests would have passed for the wrong reason, so the tests now assert the rejection reason.
- **Vendored converter** also needs `copy.go` and `time.go`.
- **Refused-sandbox condition** is kept by `finish` when the condition's `observedGeneration` equals the Agent's generation (the host loop stamps it), not `status.observedGeneration`. A successful create clears it (Ready=Unknown/SandboxCreated).
- **Ready reason**: first False required condition, else first Unknown (the plan's order showed `NotEvaluated` for an early `InvalidGracePeriod`).
- **`handoff.scope`**: the runtime fixture carries `scope: event`, which the schema defaults; the renderer leaves it out and the fixture comparison drops it.
- **SDK client** is dialled without TLS (mesh only); `ACH_AGENT_OPENSHELL_TLS_DIR` does not exist. An unset gateway fails the start.
- **controller-gen v0.20.0** matches controller-runtime v0.24 (verified by running it); gateway-api v1.6.3 matches k8s 0.36.
- **Suite manager** caches `ach-system` only so the isolated-namespace storage tests drive their own reconciler without racing it.
- Old v0.14.2 envtest/render cases tied to removed concepts (identity, image, podTemplate, NetworkPolicy, gateway URL, StatefulSet backend, examples directory) were deleted; cases for unchanged sub-types were ported where they exercised changed code.

## Open issues

1. **Sandbox template resources layout is unverified.** `Resources()` emits `{cpu, memory}` as the plan says; the SDK only exposes `map[string]any` and the gateway's reading cannot be checked offline. A gateway `InvalidArgument` mentioning "resource" becomes `SandboxResourcesUnsupported`; a silent ignore would not be caught. Verify in the Plan 3 cluster e2e (also try `{requests,limits}` if ignored).
2. **HTTPRoute is unit-tested only.** The envtest apiserver has no Gateway API CRDs, so `HTTPRouteAvailable` is false there; `buildHTTPRoute`, `reconcileRoute` branches and `copySpec` are covered by unit/fake tests, the apply path is not exercised against an apiserver.
3. ~~Reserved header list~~ now mirrors the runtime (see Review fixes).
4. **Envtest coverage of unchanged sub-types** (routing, handoff/session-concurrency CEL, env admission) is thinner than v0.14.2's: those CEL rules are unchanged in the types but their dedicated envtest cases were not ported.
5. **Default control grace** stays 120 s (v0.14.2). With the usual `workspace.shutdownTimeoutSeconds: 300` a profile must set `control.terminationGracePeriodSeconds >= 315` or Agents report `InvalidGracePeriod`.
6. No Helm chart, RBAC ClusterRole template or e2e fixtures here (Plan 3); `config/rbac/role.yaml` is generated from the markers.
7. The final review pass of the plan was not run as a separate step; the checks above are the test gates only.

## Review fixes

Branch history was rewritten locally (never pushed): `operator/bin` is out of every commit and `operator/bin/` is in `.gitignore`. Gates after the fixes: `make verify` and `make test-envtest` both exit 0.

### Fixed (each with a test that fails without the fix)
- **Policy** (`openshell/policy.go`): reject a rule whose key or `name` starts with `ach-agent-` (case-insensitive) or whose `name` differs from its key; reject any non-reserved endpoint that could match the facade (exact host, host glob, `ports`, or `allowed_ips` on the facade port); same reserved-prefix and facade-selector checks on `network_middlewares`; `read_only` and `read_write` must be absolute; `/work` is read-only and protected, `/usr/local/lib` is protected. `IncludeWorkdir` is no longer forced (`/workspace` is listed explicitly) because the image WORKDIR is `/work`.  The reserved binaries stay `python3.13`, `node` and `opencode.exe` (`.bin/opencode2` symlinks to that native ELF, the engine process that calls the model facade); a test pins the list.
- **Headers** (`render/headers.go`): reserved names, prefixes and the token regex mirror `runtime/.../config/schema.py`. The vendored v3 schema is re-vendored byte-equal to the runtime copy; `baseUrl`/`url` get `^https?://[^/\s]+` on the CRD (model, MCP, ach). The in-repo `runtime/` lacked commit 130379f, so it is cherry-picked (`-x`) to keep the drift test meaningful.
- **Gateway state** (`host/host.go`): `gatewayReported` resets on every new runtime epoch; `GatewayAvailable` is written on the first pass of every Host and whenever the observed state differs, a failed write is retried next pass; `noteGateway` runs only on `Backend.Live` results (a profile/cache load error is no evidence). The refusal clear reads the live Agent and clears any Ready=False `SandboxPolicyRejected`/`SandboxResourcesUnsupported`, so a restarted loop clears it.
- **Stop vs in-flight create**: the gateway `Create` runs on `context.WithoutCancel(ctx)` bounded by `callDeadline`; Stop already waited for the loop, so the finalizer's inventory now sees the sandbox.
- **Minor**: gRPC `Canceled` with a live caller ctx counts as an outage; the finalizer passes the uncached reader so a dropped record with a terminating workload pod keeps the finalizer; `SandboxCleanupFailed` reason for non-gateway cleanup errors; only ownership/empty-key errors are `SecretConflict` (others requeue); prune deletes only objects the Agent controls and `apply` refuses to adopt an existing unowned object; `codemem` removed from the memory enum (type, CEL arm, render branch); `SandboxResources.ephemeralStorage` added (mapped to `ephemeral-storage`) with a quantity pattern on cpu/memory/ephemeralStorage; `--leader-elect` defaults to true.

### Open issues
1. **Plan 3 chart must ship a ValidatingAdmissionPolicy** limiting the Secrets the operator ServiceAccount creates to `type: Opaque` named `ach-agent-key-*` (the operator holds cluster-wide `secrets: create`+`get`, which allows minting ServiceAccount-token Secrets). No code change here.
2. **Leader election needs a namespaced Role** on `coordination.k8s.io/leases` (get/list/watch/create/update/patch) plus `events` create/patch, in the chart; `role.yaml` is cluster-scoped and cannot carry it.
3. **`pods: delete` is cluster-wide** (used by `replaceStaleControlPod`); the spec says pods read-only. Restrict with a VAP to pods labelled `ach-agent.ackstorm.ai/component=control`, or record it as a spec deviation.
4. **`make test` skips `internal/controller`** (the package is envtest-only through its `TestMain`). Splitting the pure tests out needs a build tag or a package move; until then `make verify` must be followed by `make test-envtest`.
5. **Finalizer vs terminating pod** is covered only when the record is listed and then not found; a record already gone from the gateway list with a pod still terminating is not seen. Needs a pod-by-label inventory (design change).
6. **Old objects**: the pre-rewrite commits (with the 170 MB binaries) remain in the reflog until it expires; `refs/original` was removed.
7. `ephemeral-storage` as a template resource key is unverified against the gateway (same check as issue 1).
