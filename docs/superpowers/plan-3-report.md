# Plan 3 report: chart, e2e, release

Branch `feat/release` (from `feat/bootstrap` 28cb5c4), version 0.19.0. Nothing pushed, tagged or released.

## Commits

| sha | subject |
|---|---|
| 4510a6a | feat(chart): operator chart with leases Role and Secret admission guard |
| 6dbade0 | test(e2e): kind cluster with Istio ambient, OpenShell and stub upstreams |
| 6ef2332 | test(e2e): standalone, ach, policy, gateway outage, rotation and deletion scenarios |
| 7aa75ab | build: release workflow publishing images and the OCI chart |

## Gate results (final tree)

| gate | result |
|---|---|
| `make chart-test` (sync, drift check, `helm lint`, render test) | `EXIT=0`, `PASS` |
| `make release-check VERSION=0.19.0` | `EXIT=0` |
| `rhysd/actionlint:1.7.7` over `.github` | `EXIT=0` |
| `python -m pytest test/e2e/stubs` (4 tests) | pass |
| `make e2e-up` | `EXIT=0` (operator rolled out, OpenShell gateway Ready) |
| `make e2e-run` after `cluster.sh load` of the final images | `EXIT=0`, 8/8 PASS (278 s) |

Scenarios: standalone, sandbox-egress, ach-mode, rejected-policy, gateway-restart, gateway-down-control-restart, secret-rotation, deletion: all PASS. The whole suite also passed once earlier (279 s); two earlier runs failed (see Product findings).

## Known gaps

- **Sandbox resources ignored.** The gateway driver creates the Sandbox with `resources: {}` although the profile sets `{cpu: "1", memory: 1Gi}`. The scenario logs it and does not fail. `{requests, limits}` was not tried (Plan 2 issue 1 / 7 stay open: also `ephemeral-storage`).
- **OpenShell 0.1.5 can drop sandboxes on a gateway restart or outage.** After a restart the gateway logs `unknown_identity` for the old supervisor and the Sandbox CR disappears; the runtime marks the workspace `pod_unhealthy`, retires it, and the next webhook creates a new sandbox under the same deterministic name (`default--ws-<hash>`) with a new pod UID. It happened in most runs for the outage scenario and in one of the restart runs. A webhook that lands on the lost sandbox is dropped, so the gateway scenarios post every 45 s until the stub sees a model call. The "same name" assertion therefore holds even when the pod was replaced; the UID change is logged as `KNOWN GAP`. The spec's "no sandbox replaced" is not guaranteed by the gateway.
- Sandbox egress denial is asserted with BusyBox `wget` (the image has no curl); the failure there is `bad address` (DNS), not an explicit policy log line.
- The ACH stub serves the model path itself (`/v1/chat/completions` on 8092).

## Deviations

- The e2e module has no client-go and no `replace` on the operator: it shells out to `kubectl`.
- Sandbox pods carry no Agent label (only `openshell.ai/*`); a sandbox is identified by its per-sandbox ExternalName Service, which has the pod's name and the `runtime.ach-agent.ackstorm.ai/agent-uid` label.
- `hack/chart-sync.py` (run by `make chart-sync`) generates `chart/crds` and `chart/templates/clusterrole.yaml` (minus leases; `role.yaml` already contained leases, they moved to the namespaced Role). Chart names are fixed (`ach-agent-operator`) because the mesh policy names the ServiceAccount.
- `docs/mesh/openshell-mesh.yaml` is the single mesh source; `cluster.sh` renders it with `<AGENT_NAMESPACE>`.
- Extra egress: no Agent policy rule reaches the stubs (the operator rejects in-cluster hosts); they are reached only through the facades. `rejected-policy` uses the reserved `ach-agent-x` rule name.
- Header entries use `secretKeyRef` in the CRD (rendered as `{"env": ...}`).
- `operator/Dockerfile` now cross-compiles on `$BUILDPLATFORM`; `operator.yml` got `workflow_call`; runtime and operator workflows have distinct concurrency groups per caller so the release run cannot cancel a push run.
- `cluster.sh up` puts the caller's kubectl current-context back after `kind create` (it was unset, so I unset it again; every call passes `--context kind-ach-agent-e2e`).
- The stub image records `raw_headers` (duplicates visible) in addition to the plan's recorded headers.
- Release publishes `linux/amd64,linux/arm64` like ach-runtime; this was not exercised locally (needs a push to main).

## Product findings

No operator or runtime code changed. The two failing early runs were test-design problems (the deletion check was vacuous because it listed sandboxes through Services that were already gone; the gateway scenarios assumed sandboxes survive a gateway restart), plus the OpenShell behaviour above.

## Open issues

1. Carried from Plan 2: restrict `pods: delete`; finalizer vs a record already gone while its pod terminates (not exercised).
2. HTTPRoute apply path is still untested against an apiserver with the Gateway API CRDs.
3. `docs/install.md` Agent example is checked against the CRDs only through the e2e fixtures, not applied verbatim.
4. The release workflow and the OCI chart push are untested until a real `chore(release): v0.19.0` commit on `main`; `release-bump` was tested on a scratch copy.
5. `docs/superpowers/plan-1-report.md` is untracked (pre-existing).

## Cluster

`kind` cluster `ach-agent-e2e` (context `kind-ach-agent-e2e`) is left running. Delete it with `make e2e-down` (or `kind delete cluster --name ach-agent-e2e`). `ach-e2e` was not touched by this work (no `kind delete` was ever run; `down` was never invoked). Its `kind-ach-e2e` kubeconfig context is still present but its containers no longer exist in Docker: they had disappeared by the time of the final check (they were already absent from `docker ps` at about 10:02), so something else on the shared host removed them.
