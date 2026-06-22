# ── Builder stage ────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder
WORKDIR /app

# git is required for the pinned `hermes-agent @ git+https://...` dependency:
# uv shells out to git to clone it. Only the builder needs it — the runtime
# stage stays slim (hermes is installed as a built artifact into /app/deps).
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

# uv as a prebuilt static binary from the official image. `pip install uv` is
# fragile on slim images: when uv has no cp312 wheel yet, pip falls back to the
# sdist and tries to compile it with cargo, which fails on the slim image
# (no C toolchain). The static binary sidesteps both.
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

# hatchling needs the package source present to install it.
# Explicit COPY paths only — no wildcards (CLAUDE.md Docker rules).
# README.md + LICENSE are referenced by [project] readme / license-files in
# pyproject.toml, so hatchling needs them present to build the wheel.
COPY pyproject.toml ./pyproject.toml
COPY uv.lock ./uv.lock
COPY README.md ./README.md
COPY LICENSE ./LICENSE
COPY src/ ./src/

RUN uv pip install --system --no-cache-dir --target=/app/deps .

# ── Runtime stage ─────────────────────────────────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# PYTHONPATH points at the install target so deps are version-agnostic:
# a python base bump needs no path edit here.
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/app/deps

COPY --from=builder /app/deps /app/deps
COPY src/ ./src/

EXPOSE 8080

# non-root: uid 10001 (numeric USER so kubelet runAsNonRoot can verify without /etc/passwd)
RUN useradd -u 10001 -m appuser
USER 10001

# invoke via `python -m` so we don't depend on console-script shebangs or PATH
CMD ["python", "-m", "ach_agent.main"]
