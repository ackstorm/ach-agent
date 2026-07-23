# Phase 1 — Dockerfile `pi-bin` stage + adapter-path fix

**Goal:** the runtime image carries an executable `pi` and the pinned pi-mcp-adapter, and the driver's default adapter path points at the real package root.

**Files:**
- Modify: `Dockerfile` (new `pi-bin` stage after `codemem-bin`; runtime-stage COPY/PATH/smoke)
- Modify: `src/ach_agent/engine/pi/driver.py:39` (default adapter path → package root)
- Modify: `tests/e2e/test_pi_e2e.py:75` (adapter candidate → package root)

**Interfaces:**
- Consumes: SP1's `PiDriver` (`engine/pi/driver.py`), `build_pi_settings(mcp_adapter_path)` (`engine/pi/config.py:17`), `PiEngineBlock.mcp_adapter_path` default `""` (`config/schema.py:72`).
- Produces: in-image paths `/opt/pi/bin/pi` (on `PATH`) and `/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter` (the driver default). Phase 4 greps `PI_VERSION` / `PI_MCP_ADAPTER_VERSION` from the Dockerfile.

**Established facts (probed against the real 2.11.0 tarball):**
`npm install --prefix /opt/pi-mcp-adapter pi-mcp-adapter@2.11.0` yields the package at
`/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter/` (has `package.json`, `cli.js`) with its deps
flattened into `/opt/pi-mcp-adapter/node_modules/` (Node resolves them upward). Pi's
`settings.json` `packages:[...]` wants the **package root**, so the default must be the
`.../node_modules/pi-mcp-adapter` subpath — not the bare prefix.

---

- [ ] **Step 1: Add the `pi-bin` stage to the Dockerfile**

Insert after the `codemem-bin` stage (ends at `Dockerfile:24`, `/opt/codemem/bin/codemem --version`), before `# ── Builder stage`. Pi is installed with `--ignore-scripts` (skip postinstall — supply-chain hygiene, ek never reaches it; SP1 §12):

```dockerfile
# ── pi stage ───────────────────────────────────────────────────────────────
# Pi is the second engine (`pi --mode rpc`, JSONL over stdio). It + the pi-mcp-adapter
# are npm (Node/TS); reuse the same Node 26 the runtime already carries for codemem.
# --ignore-scripts: skip package postinstall (supply-chain hygiene; the ek never reaches Pi).
FROM node:26-bookworm-slim AS pi-bin
ARG PI_VERSION=0.81.1
ARG PI_MCP_ADAPTER_VERSION=2.11.0
RUN npm install -g --ignore-scripts --prefix /opt/pi \
      "@earendil-works/pi-coding-agent@${PI_VERSION}" \
 && npm install --ignore-scripts --prefix /opt/pi-mcp-adapter \
      "pi-mcp-adapter@${PI_MCP_ADAPTER_VERSION}" \
 && test -f /opt/pi/bin/pi \
 && test -f /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter/package.json
```

- [ ] **Step 2: Add the runtime-stage COPY + PATH + smoke**

Insert in the runtime stage right after the codemem block + `RUN codemem --version` (`Dockerfile:76`), so `node` is already on `PATH` when `pi --version` runs:

```dockerfile
# Pi (Node) runtime: the global pi prefix + the vendored adapter tree. `pi` on PATH; its
# shebang resolves `node` (already COPYed for codemem, also on PATH).
COPY --from=pi-bin /opt/pi /opt/pi
COPY --from=pi-bin /opt/pi-mcp-adapter /opt/pi-mcp-adapter
ENV PATH="/opt/pi/bin:${PATH}"
# Runtime smoke: prove pi EXECUTES in the final image (python:3.12-slim libs, not the build
# stage) and the adapter package is present — closes the "present-but-broken" fail-open half,
# same as codemem's `--version` gate above.
RUN pi --version \
 && test -f /opt/pi-mcp-adapter/node_modules/pi-mcp-adapter/package.json
```

- [ ] **Step 3: Fix the driver default adapter path**

`src/ach_agent/engine/pi/driver.py:39` — the default must be the package root:

```python
_DEFAULT_PI_MCP_ADAPTER = "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter"
```

- [ ] **Step 4: Fix the e2e adapter candidate**

`tests/e2e/test_pi_e2e.py:75` — the `/opt/...` fallback candidate must match:

```python
        "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
```

- [ ] **Step 5: Lint the Python changes**

Run: `uv run mypy --strict src/ach_agent/engine/pi/driver.py && uv run ruff check src/ach_agent/engine/pi/driver.py`
Expected: clean.

- [ ] **Step 6: Build the image — the smoke is the packaging authority**

Run: `docker build -t ach-agent:sp2-pi .`
Expected: build succeeds; the `RUN pi --version` layer prints a Pi version and the adapter `test -f` passes. A missing/broken binary fails the build here.

- [ ] **Step 7: Commit**

```bash
git add Dockerfile src/ach_agent/engine/pi/driver.py tests/e2e/test_pi_e2e.py
git commit -m "feat(pi): ship pinned pi + pi-mcp-adapter in the runtime image

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

**Ponytail note:** image gains Pi's npm tree (bundles several provider SDKs); both engines share
the single `node` binary, so the delta is JS packages only — accepted, the version `ARG`s are the
knob. `# ponytail: image size — Pi npm tree; slim via --omit=dev if it bites.`
