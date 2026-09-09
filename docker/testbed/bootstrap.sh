#!/usr/bin/env bash
# Prepare the ach-agent testbed: verify the ach-memory stack, mint a user key, write ./.env.
#
# Idempotent — rerunning mints a fresh key and overwrites ./.env. Nothing here starts or
# mutates the memory stack; it only reads it and asks the API for a key.
set -euo pipefail

cd "$(dirname "$0")"

MEMORY_REPO="${MEMORY_REPO:-../../../ach-memory}"
MEMORY_URL="${MEMORY_URL:-http://127.0.0.1:8000}"
MEMORY_NETWORK="${MEMORY_NETWORK:-ach-memory_default}"
WAIT_SECONDS="${WAIT_SECONDS:-60}"

die() { echo "ERROR: $*" >&2; exit 1; }

# --- 1. the master key -------------------------------------------------------------------
# From the environment if already exported, else from the memory repo's .env. Never echoed.
if [[ -z "${MEMORY_MASTER_KEY:-}" ]]; then
  [[ -f "$MEMORY_REPO/.env" ]] || die "no MEMORY_MASTER_KEY in env and no $MEMORY_REPO/.env.
Set MEMORY_REPO=/path/to/ach-memory, or export MEMORY_MASTER_KEY yourself."
  # shellcheck disable=SC1091
  set -a; . "$MEMORY_REPO/.env"; set +a
fi
[[ -n "${MEMORY_MASTER_KEY:-}" ]] || die "MEMORY_MASTER_KEY is empty (see $MEMORY_REPO/.env.example)."

# --- 2. the stack is up ------------------------------------------------------------------
# Bounded: WAIT_SECONDS ceiling, then a failure path. A naked `until curl; do sleep; done`
# would hang forever when the stack is simply not running.
deadline=$((SECONDS + WAIT_SECONDS))
until curl -fsS -o /dev/null "$MEMORY_URL/health" 2>/dev/null; do
  if (( SECONDS >= deadline )); then
    die "ach-memory did not answer $MEMORY_URL/health within ${WAIT_SECONDS}s. Start it with:
  cd $MEMORY_REPO && MEMORY_MCP_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:8000,localhost,localhost:8000,api:8000 docker compose up -d --build"
  fi
  sleep 2
done

# --- 3. the DNS-rebinding allowlist ------------------------------------------------------
# The MCP transport matches the Host header INCLUDING the port. The harness reaches the
# service in-network as `api:8000`, so without that exact string every MCP call comes back
# 421 — long after /health looked fine.
api_container=$(docker ps --filter "name=api" --filter "network=$MEMORY_NETWORK" --format '{{.Names}}' | head -1)
if [[ -n "$api_container" ]]; then
  allowed=$(docker inspect "$api_container" \
    --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^MEMORY_MCP_ALLOWED_HOSTS=//p')
  case "$allowed" in
    *api:8000*) ;;
    *) die "the ach-memory api allows Hosts [$allowed] — 'api:8000' is missing, so every MCP
call from the harness will fail 421. Restart the memory stack with:
  cd $MEMORY_REPO && MEMORY_MCP_ALLOWED_HOSTS=127.0.0.1,127.0.0.1:8000,localhost,localhost:8000,api:8000 docker compose up -d" ;;
  esac
else
  echo "WARN: no api container found on network '$MEMORY_NETWORK' — skipping the Host-allowlist check." >&2
fi

docker network inspect "$MEMORY_NETWORK" >/dev/null 2>&1 \
  || die "docker network '$MEMORY_NETWORK' not found. Bring the memory stack up first, or set MEMORY_NETWORK."

# --- 4. mint a user key ------------------------------------------------------------------
# Via a 0600 curl config file: an `-H "Authorization: Bearer $KEY"` argument is visible to
# every process on the host through `ps aux`.
curl_config=$(mktemp)
chmod 600 "$curl_config"
trap 'rm -f "$curl_config"' EXIT
cat >"$curl_config" <<EOF
header = "Authorization: Bearer $MEMORY_MASTER_KEY"
header = "Content-Type: application/json"
EOF

# Reuse the key already in ./.env when there is one. A project is OWNED by the user whose
# key bootstrapped it, and a second user asking for that slug gets PROJECT_NOT_FOUND — so
# minting a fresh user on every run would silently orphan the existing bank. FRESH=1 to
# deliberately start over (with a new PROJECT_SLUG, or after wiping the memory volumes).
user_key=""
if [[ -z "${FRESH:-}" && -f .env ]]; then
  user_key=$(sed -n 's/^ACH_SECRET_MEMORY_ACHMEMORY=//p' .env)
fi

if [[ -n "$user_key" ]]; then
  user_id="(reused from ./.env)"
else
  user_id=$(curl --config "$curl_config" -fsS -X POST "$MEMORY_URL/v1/users" -d '{}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["user_id"])')
  user_key=$(curl --config "$curl_config" -fsS -X POST "$MEMORY_URL/v1/users/$user_id/keys" -d '{}' \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["key"])')
fi

# --- 5. provision the project bank -------------------------------------------------------
# NOT optional, and nothing else does it: `retain` and `load_context` resolve the project
# with create=False (retention.py, read_context.py), so against a project that was never
# bootstrapped every memory call comes back PROJECT_NOT_FOUND. The harness is fail-open, so
# that surfaces as memory silently never working rather than as an error. `POST /v1/bootstrap`
# is idempotent and also reconciles the two built-in mental models.
#
# Must match what resolve_project() derives in the container: POD_NAMESPACE + agent.name
# from docker-compose.yaml and config.yaml.
project_slug="${PROJECT_SLUG:-testbed-memory-probe}"
user_cfg=$(mktemp)
chmod 600 "$user_cfg"
trap 'rm -f "$curl_config" "$user_cfg"' EXIT
cat >"$user_cfg" <<EOF
header = "Authorization: Bearer $user_key"
header = "Content-Type: application/json"
EOF
status=$(curl --config "$user_cfg" -fsS -X POST "$MEMORY_URL/v1/bootstrap" \
  -d "{\"project_slug\":\"$project_slug\"}" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["project_status"])')
[[ "$status" == "ready" ]] || die "bootstrap returned project_status=$status for '$project_slug'"
echo "project '$project_slug' bootstrapped (status=$status)"

# --- 6. hand it to compose ---------------------------------------------------------------
# ./.env is read automatically by `docker compose` in this directory, and is gitignored.
umask 077
cat > .env <<EOF
# Generated by bootstrap.sh — a minted ach-memory USER key. Not committed (.gitignore).
ACH_SECRET_MEMORY_ACHMEMORY=$user_key
MEMORY_NETWORK=$MEMORY_NETWORK
EOF

echo "ach-memory user $user_id provisioned; key written to docker/testbed/.env"
echo "next: ACH_TOKEN=ek-... docker compose run --rm agent"
