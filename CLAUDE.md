# ach-agent

## Layout
- `operator/` Go module: Agent/AgentProfile CRDs, controller, render, OpenShell backend.
- `runtime/` Python package `ach_runtime` (control and execution roles), distribution `ach-agent-runtime`.
- `docs/superpowers/` specs and plans.

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
# full suite, as CI runs it
make -C runtime test-unit
```
