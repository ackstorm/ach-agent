# ── opencode binary stage ────────────────────────────────────────────────────
# The harness shells out to `opencode serve`, so the runtime image must carry the
# opencode binary. Fetch the pinned release (anomalyco/opencode, glibc linux-x64).
FROM debian:13-slim AS opencode-bin
ARG OPENCODE_VERSION=1.17.11
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" -o /tmp/oc.tgz \
 && tar -xzf /tmp/oc.tgz -C /usr/local/bin opencode \
 && chmod 755 /usr/local/bin/opencode \
 && rm -rf /tmp/oc.tgz /var/lib/apt/lists/*

# ── codemem stage ────────────────────────────────────────────────────────────
# codemem is a Node.js CLI (npm). opencode spawns it as a stdio MCP child:
# `codemem mcp --db-path <db>`. Install into an isolated prefix so one COPY brings
# the package + its (native) deps into the runtime. bookworm matches runtime glibc.
FROM node:26-bookworm-slim AS codemem-bin
ARG CODEMEM_VERSION=0.37.1
# better-sqlite3 (codemem dep) has no prebuilt binary for Node 26 yet; node-gyp
# needs python3+make+g++ to compile it from source.
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends python3 make g++ \
 && npm install -g --prefix /opt/codemem "codemem@${CODEMEM_VERSION}" \
 && /opt/codemem/bin/codemem --version

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

# ── Builder stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /app

# uv as a prebuilt static binary from the official image (pip-installing uv is
# fragile on slim images without a C toolchain).
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

# hatchling needs the package source + README/LICENSE (referenced by pyproject)
# present to build the wheel. Explicit COPY paths only — no wildcards (CLAUDE.md).
COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
COPY README.md ./README.md
COPY LICENSE ./LICENSE
COPY src/ ./src/

RUN uv pip install --system --no-cache-dir --target=/app/deps . \
 # Lipo: drop bytecode caches, type stubs and bundled test suites from the deps —
 # none are needed at runtime (we run `python -m`, never import package test modules).
 && find /app/deps -type d -name "__pycache__" -prune -exec rm -rf {} + \
 && find /app/deps -type d \( -name tests -o -name test \) -prune -exec rm -rf {} + \
 && find /app/deps -name "*.pyi" -delete

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# PYTHONPATH points at the install target so deps are version-agnostic.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/deps

# ripgrep on PATH: opencode's grep/glob tools shell out to `rg`. Without it opencode
# fetches a pinned musl build from GitHub on first use — slow, and re-fetched every
# invocation (each opencode runs under a fresh ephemeral HOME). Baking it avoids the
# download (and works offline). Calendar-only agents rarely hit it, but code agents do.
# libatomic1: Node 26 (codemem-bin) links against it; python:3.12-slim doesn't ship it.
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends ripgrep libatomic1 \
 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /app/deps /app/deps
COPY --from=opencode-bin /usr/local/bin/opencode /usr/local/bin/opencode
# codemem (Node) runtime: the node binary + the isolated codemem prefix. PATH prepend
# puts `codemem` on PATH; its shebang resolves `node` from /usr/local/bin (also on PATH).
COPY --from=codemem-bin /usr/local/bin/node /usr/local/bin/node
COPY --from=codemem-bin /opt/codemem /opt/codemem
ENV PATH="/opt/codemem/bin:${PATH}"
# Runtime smoke: prove codemem actually EXECUTES in the final image (python:3.12-slim libs),
# not just that the binary is present. prepare_codemem probes PATH only (shutil.which), so a
# present-but-broken codemem would pass the probe and crash opencode's stdio child at runtime.
# Failing the build here closes the "broken (not missing)" half of the fail-open invariant.
RUN codemem --version

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

# Bake a minimal default contract so a collaborator can run the image with only an EK
# and an ACH endpoint (no mounted files):
#   docker run -it -e ACH_TOKEN=ek-... -e ACH_BASE_URL=https://your-ach-host \
#       ghcr.io/ackstorm/ach-agent:latest --tui
# The baked config carries NO host — ACH_BASE_URL supplies it at runtime; the EK scopes
# which environment/tools are hydrated. Override the whole contract by mounting your own
# YAML/JSON and setting ACH_CONFIG_PATH (helm does this in prod).
COPY docker/sample-config.yaml /etc/ach-agent/config.yaml
ENV ACH_CONFIG_PATH=/etc/ach-agent/config.yaml

EXPOSE 8080

# non-root: uid 10001 (numeric USER so kubelet runAsNonRoot can verify without /etc/passwd).
# Pre-create the engine home owned by uid 10001 so a volume mounted there inherits a
# non-root-writable mountpoint. workDir (<home>/workspace) and .ach-state (<home>/.ach-state)
# live UNDER home, so the harness creates them — no top-level scratch dirs needed.
RUN useradd -u 10001 -m appuser \
 && mkdir -p /tmp/ach-home \
 && chown -R 10001 /tmp/ach-home
USER 10001

# ENTRYPOINT (not CMD) so launch modifiers append cleanly:
#   docker run IMAGE              → channels mode (prod default; helm sets no command/args)
#   docker run -it IMAGE --tui    → interactive console REPL
#   docker run -i  IMAGE --prompt "hello"  → one-shot, print reply, exit
# invoke via `python -m` so we don't depend on console-script shebangs or PATH.
ENTRYPOINT ["python", "-m", "ach_agent.main"]
