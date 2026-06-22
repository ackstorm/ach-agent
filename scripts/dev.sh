#!/usr/bin/env bash
# Run any command inside the content-addressed devtools container.
# No host pip/venv — this is the single entrypoint for all tooling.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

OWNER="${GITHUB_REPOSITORY_OWNER:-ackstorm}"
HASH="$(sha256sum docker/devtools/Dockerfile | cut -c1-12)"
IMAGE="ghcr.io/${OWNER}/ach-agent-devtools:${HASH}"

# Pull the published image; fall back to a local build if unavailable.
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  if ! docker pull "$IMAGE" >/dev/null 2>&1; then
    echo "dev.sh: building devtools image $IMAGE locally..." >&2
    docker build -t "$IMAGE" -f docker/devtools/Dockerfile .
  fi
fi

# Conditional TTY: appends -t only when stdin is a real TTY (avoids CI "not a TTY" errors).
# Host UID/GID: prevents root-owned files on the mounted source tree.
# HOME + UV_CACHE_DIR point at host-owned, in-repo paths so a non-root host UID can
# write them (a named volume at /root/.cache is root-owned → "Permission denied").
# .uv-cache/ persists the uv download cache across runs and is git/docker-ignored.
exec docker run --rm -i $( [ -t 0 ] && echo "-t" ) \
  -u "$(id -u):$(id -g)" \
  -e IN_DEVTOOLS=1 \
  -e HOME=/tmp \
  -e UV_CACHE_DIR=/app/.uv-cache \
  -e GITHUB_REPOSITORY_OWNER="$OWNER" \
  -v "$REPO_ROOT:/app" \
  -w /app \
  "$IMAGE" "$@"
