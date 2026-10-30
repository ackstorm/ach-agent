# ach-agent

## Layout
- `operator/` Go module: Agent/AgentProfile CRDs, controller, render, OpenShell backend.
- `runtime/` Python package `ach_runtime` (control and execution roles), distribution `ach-agent-runtime`.
- `chart/` Helm chart (operator, CRDs, RBAC, Secret admission policy); `make chart-test`.
- `test/e2e/` kind cluster `ach-agent-e2e` (Istio ambient + OpenShell + stub upstreams) and the Go e2e suite; `make e2e-up|e2e-run|e2e-down`. Every kubectl/helm call uses `--context kind-ach-agent-e2e`; never touch the shared `ach-e2e`.
- `docs/install.md`, `docs/mesh/` install guide and reference mesh policy.
- `docs/superpowers/` specs and plans.
- Release: `make release-bump VERSION=X.Y.Z`, then a `chore(release): vX.Y.Z` commit on `main` (see `.github/workflows/release.yml`); `make release-check` verifies runtime, chart and CHANGELOG agree.

## Invariants
- The runtime never calls the Kubernetes API or the OpenShell gateway API.
- Use `/usr/bin/git` for every git command.

## Runtime tests (from `runtime/`)
```bash
# one-time
uv venv .venv --python 3.13 && uv pip install --python .venv/bin/python -e ".[dev]"
# per file
PYTHONPATH=src:tests/storage:tests/engine:tests/facades:tests/harness \
  .venv/bin/pytest --import-mode=importlib tests/<path>.py -q -p no:cacheprovider
# full suite, as CI runs it: starts the S3 fixture and runs in the devtools image (opencode inside).
# The host venv has neither, so tests/e2e, tests/storage S3 and tests/engine fail there.
make -C runtime test-unit
```
