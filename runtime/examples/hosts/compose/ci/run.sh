#!/usr/bin/env bash
# Single-unit acceptance test (spec 12): run a task, recreate the stack, run a second task on the
# same session key, and prove the Workspace was restored from S3.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
export AGENT_PORT="${AGENT_PORT:-18080}"
DC=(docker compose -p ach-compose-ci -f "$HERE/../compose.yaml" -f "$HERE/compose.ci.yaml")
URL="http://127.0.0.1:${AGENT_PORT}/channels/tasks/events"

fail() { echo "FAIL: $*" >&2; "${DC[@]}" logs --no-color --tail 200 >&2 || true; exit 1; }
cleanup() { "${DC[@]}" down -v --remove-orphans >/dev/null 2>&1 || true; }
trap cleanup EXIT

send() {  # $1 = delivery id
  local code
  code="$(curl -sS --max-time 30 -o /dev/null -w '%{http_code}' -X POST "$URL" \
    -H 'x-webhook-token: dev-webhook-token' -H "x-request-id: $1" -H 'content-type: application/json' \
    -d '{"session": "ci", "task": "Say pong"}')" || fail "webhook $1 unreachable"
  [ "$code" = "202" ] || fail "webhook $1 answered $code, expected 202"
}

wait_completed() {  # bounded: 90 x 2 s
  local n
  for i in $(seq 1 90); do
    n="$("${DC[@]}" logs --no-color agent 2>&1 | grep -c '"outcome": "completed"' || true)"
    [ "${n:-0}" -ge 1 ] && return 0
    sleep 2
  done
  fail "no completed run within 180 s"
}

s3_keys() {
  "${DC[@]}" run --rm --no-deps --entrypoint python s3-init -c '
import boto3
s3 = boto3.client("s3", endpoint_url="http://s3:8333", region_name="us-east-1",
                  aws_access_key_id="ach-test-key", aws_secret_access_key="ach-test-secret")
print(s3.list_objects_v2(Bucket="ach").get("KeyCount", 0))'
}

timeout 900 "${DC[@]}" up --build -d --wait --wait-timeout 300 || fail "stack did not become healthy"

send ci-1
wait_completed
# The idle timeouts in config.json make the runtime snapshot the Workspace on its own; the
# bucket then holds more than the registry object. Bounded: 45 x 2 s.
keys=0
for i in $(seq 1 45); do
  keys="$(s3_keys | tail -n 1)" || keys=0
  [ "${keys:-0}" -ge 2 ] && break
  sleep 2
done
[ "${keys:-0}" -ge 2 ] || fail "no Workspace snapshot in S3 after the first run (objects: ${keys:-0})"
echo "first run completed; S3 objects: $keys"

# Recreate the containers, keep the named S3 volume.
"${DC[@]}" down --remove-orphans || fail "down failed"
timeout 900 "${DC[@]}" up -d --wait --wait-timeout 300 || fail "stack did not come back healthy"

send ci-2
wait_completed
max_turns="$("${DC[@]}" logs --no-color echo 2>&1 | grep -o 'echo turns=[0-9]*' | cut -d= -f2 | sort -n | tail -n 1)" || true
[ "${max_turns:-0}" -ge 2 ] || fail "second run did not see the restored conversation (max turns=${max_turns:-0})"
echo "PASS: restored after recreate (model saw $max_turns user turns)"
