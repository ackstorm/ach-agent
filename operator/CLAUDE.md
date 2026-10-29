# operator

Go module `github.com/ackstorm/ach-agent/operator`: the `Agent` / `AgentProfile` controller and the
per-Agent sandbox host loops. Group `ach-agent.ackstorm.ai/v1alpha1`, no shortNames. The code is a
port of the ach `v0.14.2` agent operator (read it with `git -C <ach> archive v0.14.2`).

## Layout
- `api/v1alpha1` types and CEL; `config/crd`, `config/rbac` and `zz_generated.deepcopy.go` are generated.
- `internal/render` Agent + AgentProfile -> `workspace-v3` config (`Render`) plus the env bindings
  for header and key Secrets; the vendored schema in `testdata/` must equal
  `runtime/docs/schemas/ach-workspace-config-v3.schema.json` (drift test fails otherwise).
- `internal/controller` Agent reconciler: control StatefulSet, headless Service (8081), `-http`
  Service, `-config` ConfigMap, `-state` PVC, key Secret, HTTPRoute, finalizer.
- `internal/host` leader-elected runner, one host loop per Agent against the runtime's `/host/v1`.
  Owns the `Backend` interface and the wire types.
- `internal/openshell` the only `Backend`: policy merge, sandbox create/delete, per-sandbox
  ExternalName Service, rebuildable gateway client.
- `third_party/openshellconv` vendored Apache-2.0 SDK converter (keeps its NVIDIA headers).
- `cmd/operator` main.

## Make targets (from `operator/`)
`make gen` (deepcopy, CRDs, RBAC), `make test` (vet + unit, no cluster), `make test-envtest`
(controller envtest, downloads apiserver binaries), `make verify` (gen drift + vet + test).
`controller-gen` is v0.20.0. The controller package is envtest-only: even its pure tests need
`KUBEBUILDER_ASSETS`, which the Makefile sets.

## Invariants
- The operator is the only Kubernetes and gateway API client. The runtime is reached only
  through `/host/v1`.
- The finalizer `ach-agent.ackstorm.ai/sandboxes` is removed only after the host loop stopped,
  the gateway inventory by agent UID is empty and no per-sandbox Service is left. An unreachable
  or missing gateway keeps it.
- While the gateway is unavailable the host never removes a unit, confirms a stop or reports a
  unit lost; it reports `PUT /gateway available=false` and the `GatewayAvailable` condition.
- Sandbox policy rule names starting `ach-agent-` are reserved; the runtime binaries and their
  directories are never writable. The policy document uses the proto field names
  (`network_policies`, `filesystem`, `landlock`).
- Header env names: `ACH_AGENT_MODEL_H<i>`, `ACH_AGENT_MCP_<NAME>_H<i>` (index in the
  case-insensitive sorted header names); the ACH key is `ACH_SECRET_IDENTITY`.
- CEL cost: map keys cannot be bounded, so rules over header names live in `render`, not the CRD.
- Operator env: `ACH_AGENT_OPENSHELL_GATEWAY` (required), `ACH_AGENT_OPENSHELL_WORKSPACE`,
  `ACH_AGENT_OPENSHELL_SERVICE_URL`, `ACH_AGENT_OPENSHELL_POD_NAMESPACE`, `ACH_AGENT_NAMESPACE`,
  `ACH_AGENT_STORAGE_S3_*`, `ACH_AGENT_ROUTE_PARENT_{NAME,NAMESPACE,SECTION}`.
