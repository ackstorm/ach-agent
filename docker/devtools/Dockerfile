# Dev/CI toolchain image — every Makefile target runs inside this.
# Content-addressed by its own sha256 (see scripts/dev.sh). No host pip/venv.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_SYSTEM_PYTHON=1 \
    UV_LINK_MODE=copy

# OS tools: make (target runner inside container), git (version/changelog ops),
# docker CLI (for the secrets/e2e targets that shell out to docker).
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends make git ca-certificates curl \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
    https://download.docker.com/linux/debian bookworm stable" \
    > /etc/apt/sources.list.d/docker.list \
 && apt-get update -qq \
 && apt-get install -y --no-install-recommends docker-ce-cli \
 && apt-get purge -y curl \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

# uv as a prebuilt static binary (avoids cargo compile on slim)
COPY --from=ghcr.io/astral-sh/uv:0.11.21 /uv /usr/local/bin/uv

WORKDIR /app

# Copy pyproject.toml and uv.lock for dep installation
COPY pyproject.toml uv.lock ./

# Install dependencies (incl. the dev group: lint/test/docs tools) from the
# lockfile. --no-install-project: the ach-agent package source is NOT in this
# stage (only pyproject.toml + uv.lock are); it is bind-mounted at runtime by
# scripts/dev.sh, where `uv run` installs it editable against the live tree.
RUN uv sync --frozen --no-install-project

WORKDIR /app
CMD ["bash"]
