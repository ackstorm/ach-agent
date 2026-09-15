#!/usr/bin/env bash
set -euo pipefail

# Bounded real-engine acceptance for one persistent standalone -> distributed handoff.
# Run with: rtk proxy bash scripts/test-pvc-transition.sh
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${ACH_IMAGE:-ach-agent:pvc-stable-review}"
ENGINE="${ACH_ENGINE_TARGET:-engine-opencode}"
PROJECT="ach-pvc-transition-${USER:-user}-$$-${RANDOM}"
PVC_DIR="$(mktemp -d "${TMPDIR:-/tmp}/ach-pvc-transition.XXXXXX")"
STANDALONE=(docker compose -p "${PROJECT}-standalone" -f "$ROOT_DIR/docker/split/compose-pvc-transition-standalone.yaml")
DISTRIBUTED=(docker compose -p "${PROJECT}-distributed" -f "$ROOT_DIR/docker/split/compose-pvc-transition-distributed.yaml")
export ACH_IMAGE="$IMAGE" PVC_DIR
export ACH_CONFIG_FILE="$ROOT_DIR/tests/integration/fixtures/pvc-transition-config.yaml"
[ "$ENGINE" = engine-pi ] && export ACH_CONFIG_FILE="$ROOT_DIR/tests/integration/fixtures/pvc-transition-config-pi.yaml"

cleanup() {
  "${STANDALONE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  "${DISTRIBUTED[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  docker run --rm --user 0 --entrypoint python -v "$PVC_DIR:/data" "$IMAGE" -c \
    'from pathlib import Path; import shutil; root=Path("/data"); [shutil.rmtree(p) if p.is_dir() and not p.is_symlink() else p.unlink() for p in root.iterdir()]' \
    >/dev/null 2>&1 || true
  rm -rf "$PVC_DIR"
}
trap cleanup EXIT INT TERM

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "image $IMAGE is unavailable; build it first with docker dev tooling" >&2
  exit 2
fi

echo "PVC transition acceptance: engine=$ENGINE image=$IMAGE data=$PVC_DIR"
mkdir -p "$PVC_DIR/state" "$PVC_DIR/home/workspace"
chmod 0777 "$PVC_DIR" "$PVC_DIR/state" "$PVC_DIR/home" "$PVC_DIR/home/workspace"

compose_config() {
  "$@" config --quiet
}

wait_ingress() {
  local mode=$1 service=$2
  local file="$ROOT_DIR/docker/split/compose-pvc-transition-${mode}.yaml"
  local port=""
  for _ in $(seq 1 60); do
    port="$(docker compose -p "${PROJECT}-${mode}" -f "$file" port "$service" 8080 2>/dev/null | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1 || true)"
    if [ -n "$port" ] && curl -fsS "http://127.0.0.1:${port}/readyz" >/dev/null 2>&1; then
      INGRESS_PORT="$port"
      return 0
    fi
    sleep 1
  done
  echo "$service ingress did not become ready" >&2
  return 1
}

submit() {
  local id=$1 reply status
  reply="$(curl -fsS -w $'\n%{http_code}' -X POST "http://127.0.0.1:${INGRESS_PORT}/channels/acceptance/events" \
    -H 'content-type: application/json' -H "idempotency-key: $id" \
    --data '{"repository":{"full_name":"pvc/acceptance"},"number":7}')"
  status="${reply##*$'\n'}"
  [ "$status" = 202 ] || { echo "event $id was not admitted: $reply" >&2; return 1; }
  printf '%s' "${reply%$'\n'*}" | sed -n 's/.*"task_id":"\([^"]*\)".*/\1/p'
}

wait_completion() {
  local mode=$1 service=$2 id=$3
  local compose_name="${PROJECT}-${mode}"
  local file="$ROOT_DIR/docker/split/compose-pvc-transition-${mode}.yaml"
  if [ "$mode" = standalone ]; then
    for _ in $(seq 1 60); do
      logs="$(docker compose -p "$compose_name" -f "$file" logs --no-color standalone 2>/dev/null || true)"
      if grep -q 'engine: response.*standalone-marker' <<<"$logs" && \
         grep -q 'engine: summary' <<<"$logs"; then
        echo "standalone harness terminal response observed: active-marker=standalone-marker"
        return 0
      fi
      sleep 1
    done
    echo "standalone model turn did not reach the fixture" >&2
    return 1
  fi
  docker compose -p "$compose_name" -f "$file" exec -T "$service" python - "$id" "$mode" <<'PY'
import asyncio, sys
from ach_agent.channels.client import ChannelsClient
from ach_agent.channels.envelopes import EventRef

async def main() -> None:
    event_id, mode = sys.argv[1:]
    client = ChannelsClient("/run/ach-agent/channels/channel.sock", poll_interval=0.2, wait_timeout=25)
    try:
        client.agent = (await client.fetch_config()).agent_name
        result = await client.wait(EventRef(agent=client.agent, channel_name="acceptance", idempotency_key=event_id))
        print(result.model_dump_json())
        if result.state != "completed":
            raise SystemExit(f"event ended in {result.state}: {result.error}")
    finally:
        await client.close()
asyncio.run(main())
PY
}

native_session() {
  local project=$1 compose_file=$2 service=$3
  docker compose -p "$project" -f "$compose_file" exec -T "$service" python - <<'PY'
import sqlite3
with sqlite3.connect("/base/home/.ach-execution/sessions.db") as db:
    rows = db.execute("SELECT key, oc_session_id FROM oc_sessions WHERE key LIKE '%pvc-transition-conversation' ORDER BY last_used DESC").fetchall()
if len(rows) != 1:
    raise SystemExit(f"expected exactly one native session row, got {rows!r}")
print(f"{rows[0][0]}={rows[0][1]}")
PY
}

assert_model_evidence() {
  local project=$1 file=$2
  local stats
  stats="$(docker compose -p "$project" -f "$file" exec -T pvc-upstream python - <<'PY'
import json, urllib.request
with urllib.request.urlopen("http://127.0.0.1:9080/health") as response:
    print(response.read().decode())
PY
)"
  echo "upstream stats: $stats"
  docker compose -p "$project" -f "$file" exec -T pvc-upstream python - "$stats" <<'PY'
import json, sys
s = json.loads(sys.argv[1])
assert s["counts"]["model"] >= 2, s
assert s["counts"]["tool_turn"] >= 1, s
assert s["counts"]["prior_history_seen"] >= 1, s
assert "distributed-marker" in s["marker_results"], s
assert max(s["message_sizes"]) > min(s["message_sizes"]), s
PY
}

compose_config "${STANDALONE[@]}"
"${STANDALONE[@]}" up -d
wait_ingress standalone standalone
first_id="pvc-standalone-${ENGINE}"
submit "$first_id" >/dev/null
wait_completion standalone standalone "$first_id"
first_session="$(native_session "${PROJECT}-standalone" "$ROOT_DIR/docker/split/compose-pvc-transition-standalone.yaml" standalone)"
echo "standalone native session: $first_session"

"${STANDALONE[@]}" exec -T standalone python - <<'PY'
from pathlib import Path
matches = list(Path("/base/home/workspace").glob("*/active-marker"))
if len(matches) != 1:
    raise SystemExit(f"expected one active marker, got {matches!r}")
p = matches[0]
assert p.read_text() == "standalone-marker"
print(f"workspace marker: {p}")
PY
"${STANDALONE[@]}" down --remove-orphans

if [ "$ENGINE" = engine-pi ]; then
  export ACH_CONFIG_PATH=/etc/ach-agent/config.yaml
fi
compose_config "${DISTRIBUTED[@]}"
"${DISTRIBUTED[@]}" up -d
wait_ingress distributed harness
"${DISTRIBUTED[@]}" exec -T harness python - <<'PY'
from pathlib import Path
matches = list(Path("/base/home/workspace").glob("*/active-marker"))
if len(matches) != 1:
    raise SystemExit(f"expected one retained active marker, got {matches!r}")
matches[0].write_text("distributed-marker")
print(f"harness mutated marker: {matches[0]}")
PY
second_id="pvc-distributed-${ENGINE}"
submit "$second_id" >/dev/null
wait_completion distributed harness "$second_id"
second_session="$(native_session "${PROJECT}-distributed" "$ROOT_DIR/docker/split/compose-pvc-transition-distributed.yaml" engine)"
echo "distributed native session: $second_session"
[ "$first_session" = "$second_session" ] || { echo "native session changed: $first_session -> $second_session" >&2; exit 1; }

"${DISTRIBUTED[@]}" exec -T harness python - <<'PY'
from pathlib import Path
assert list(Path("/base/home").glob(".ach-execution/*")) == []
assert not Path("/base/home/.ach-execution/sessions.db").exists()
PY
assert_model_evidence "${PROJECT}-distributed" "$ROOT_DIR/docker/split/compose-pvc-transition-distributed.yaml"
echo "PVC transition acceptance passed: standalone -> distributed, engine=$ENGINE"
