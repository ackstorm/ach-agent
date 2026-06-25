# ── opencode binary stage ────────────────────────────────────────────────────
# The harness shells out to `opencode serve`, so the runtime image must carry the
# opencode binary. Fetch the pinned release (anomalyco/opencode, glibc linux-x64).
FROM debian:12-slim AS opencode-bin
ARG OPENCODE_VERSION=1.16.0
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends curl ca-certificates \
 && curl -fsSL "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-linux-x64.tar.gz" -o /tmp/oc.tgz \
 && tar -xzf /tmp/oc.tgz -C /usr/local/bin opencode \
 && chmod 755 /usr/local/bin/opencode \
 && rm -rf /tmp/oc.tgz /var/lib/apt/lists/*

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

RUN uv pip install --system --no-cache-dir --target=/app/deps .

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# PYTHONPATH points at the install target so deps are version-agnostic.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/deps

COPY --from=builder /app/deps /app/deps
COPY --from=opencode-bin /usr/local/bin/opencode /usr/local/bin/opencode
COPY src/ ./src/

EXPOSE 8080

# non-root: uid 10001 (numeric USER so kubelet runAsNonRoot can verify without /etc/passwd).
# Pre-create writable work/state dirs the harness + opencode use (the pool's ephemeral
# homes go under TMPDIR=/tmp, which is world-writable).
RUN useradd -u 10001 -m appuser \
 && mkdir -p /tmp/workspace /tmp/ach-state \
 && chown -R 10001 /tmp/workspace /tmp/ach-state
USER 10001

# invoke via `python -m` so we don't depend on console-script shebangs or PATH.
# Override with `--tui` (console REPL) via compose `command:` or `docker run ... --tui`.
CMD ["python", "-m", "ach_agent.main"]
