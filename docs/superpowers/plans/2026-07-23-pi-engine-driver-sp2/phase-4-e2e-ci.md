# Phase 4 — CI e2e job: run the real-Pi turn against the pinned binary

**Goal:** the real-Pi e2e (`tests/e2e/test_pi_e2e.py`) runs in CI instead of skipping. This is
the D-4 authority: a real `pi --mode rpc` turn with ek-hygiene assertions, against the same
pinned versions the image ships.

**Files:**
- Modify: `.github/workflows/ci.yml` (new `e2e-pi` job)

**Interfaces:**
- Consumes: Phase 1's Dockerfile `ARG PI_VERSION` / `ARG PI_MCP_ADAPTER_VERSION` (grep'd — single
  source, no drift); `tests/e2e/test_pi_e2e.py` (skips unless `pi` on PATH; reads
  `PI_MCP_ADAPTER_PATH`, `:74`; asserts `ACH_API_KEY` absent from the Pi subprocess, `:120`).
- Produces: a CI gate that fails if a Pi turn breaks or the ek leaks.

**Design note (ponytail):** no bespoke `docker run` harness and no separate durability e2e. The
Dockerfile build-time `pi --version` smoke (Phase 1) is the in-image packaging authority; this job
is the behaviour authority. A Pi-specific durability e2e is **skipped** — SP1's driver unit tests
+ this real-turn e2e cover the path; add one only when a Pi session-continuity bug appears.

---

- [ ] **Step 1: Add the `e2e-pi` job to `ci.yml`**

Append under `jobs:` in `.github/workflows/ci.yml` (after the `conformance` job):

```yaml
  e2e-pi:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '26'
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install pinned Pi + adapter (versions read from the Dockerfile)
        run: |
          set -euo pipefail
          PI_VERSION=$(grep -oP 'ARG PI_VERSION=\K\S+' Dockerfile)
          PI_MCP_ADAPTER_VERSION=$(grep -oP 'ARG PI_MCP_ADAPTER_VERSION=\K\S+' Dockerfile)
          npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"
          npm install --ignore-scripts --prefix "$HOME/pi-adapter" \
            "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}"
          echo "PI_MCP_ADAPTER_PATH=$HOME/pi-adapter/node_modules/pi-mcp-adapter" >> "$GITHUB_ENV"
          pi --version
      - name: Install uv
        run: pip install uv
      - run: uv sync --frozen
      - run: uv run pytest tests/e2e/test_pi_e2e.py -v
```

- [ ] **Step 2: Sanity-check the YAML**

Run: `python -c "import yaml,sys; yaml.safe_load(open('.github/workflows/ci.yml')); print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Reproduce the job locally (proves the e2e no longer skips)**

Run (mirrors the job — installs the pinned Pi, then runs the e2e):

```bash
PI_VERSION=$(grep -oP 'ARG PI_VERSION=\K\S+' Dockerfile)
PI_MCP_ADAPTER_VERSION=$(grep -oP 'ARG PI_MCP_ADAPTER_VERSION=\K\S+' Dockerfile)
npm install -g --ignore-scripts "@earendil-works/pi-coding-agent@${PI_VERSION}"
npm install --ignore-scripts --prefix "$HOME/pi-adapter" "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}"
PI_MCP_ADAPTER_PATH="$HOME/pi-adapter/node_modules/pi-mcp-adapter" \
  uv run pytest tests/e2e/test_pi_e2e.py -v
```

Expected: the test **runs** (no "pi binary not installed" skip) and **PASSES** — a real Pi turn
replies and `ACH_API_KEY` is absent from the subprocess env.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci(pi): run the real-Pi e2e against the pinned binary

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```
