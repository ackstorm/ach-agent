#!/usr/bin/env bash
set -euo pipefail

# Real three-role acceptance.  Run this file through `rtk proxy bash scripts/test-split.sh`.
# All Python below executes in the already-built role container; the host only runs Docker,
# curl and small POSIX utilities.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROJECT="ach-phase1-split-${USER:-user}-$$-${RANDOM}"
COMPOSE=(docker compose -p "$PROJECT" -f "$ROOT_DIR/docker/split/compose.yaml" -f "$ROOT_DIR/docker/split/compose-acceptance.yaml")

export ACH_HARNESS_CONFIG_FILE=config-acceptance.yaml
export ACH_BASE_URL=http://mock-upstream:9080
export ACH_TOKEN=split-acceptance-key

cleanup() {
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT INT TERM

echo "split acceptance project: $PROJECT"
run_engine_acceptance() {
  local target="$1"
  export ACH_ENGINE_TARGET="$target"
  if [ "$target" = engine-pi ]; then
    export ACH_HARNESS_CONFIG_FILE=config-acceptance-pi.yaml
  else
    export ACH_HARNESS_CONFIG_FILE=config-acceptance.yaml
  fi
  "${COMPOSE[@]}" down --volumes --remove-orphans >/dev/null 2>&1 || true
  "${COMPOSE[@]}" config --quiet
  "${COMPOSE[@]}" build harness channels engine
  startup_started="$(date +%s)"
  "${COMPOSE[@]}" up -d

  INGRESS_PORT=""
  for _ in $(seq 1 60); do
    INGRESS_PORT="$(${COMPOSE[@]} port harness 8080 2>/dev/null | sed -n 's/.*:\([0-9][0-9]*\)$/\1/p' | head -1)"
    if [ -n "$INGRESS_PORT" ] && curl -fsS "http://127.0.0.1:${INGRESS_PORT}/healthz" >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
  [ -n "$INGRESS_PORT" ] || { echo "channels ingress did not become reachable" >&2; return 1; }

  wait_harness_ready
  echo "$target startup seconds: $(( $(date +%s) - startup_started ))"

  first="split-${target}-one"
  second="split-${target}-two"
  first_task="$(submit "$first")"
  [ -n "$first_task" ] || { echo "first admission had no task id" >&2; return 1; }
  first_result="$(wait_completion acceptance "$first")"
  echo "$target first completion: $first_result"
  echo "$first_result" | grep -q '"state":"completed"'
  first_session="$(native_session_ref)"
  [ -n "$first_session" ] || { echo "first native session was not recorded" >&2; return 1; }
  echo "$target native session after first event: $first_session"
  assert_engine_forwarded_env

  second_task="$(submit "$second")"
  [ -n "$second_task" ] || { echo "second admission had no task id" >&2; return 1; }
  second_result="$(wait_completion acceptance "$second")"
  echo "$target second completion: $second_result"
  echo "$second_result" | grep -q '"state":"completed"'
  second_session="$(native_session_ref)"
  [ "$second_session" = "$first_session" ] || {
    echo "native session was not reused: $first_session -> $second_session" >&2
    return 1
  }
  echo "$target native session reused: $second_session"

  stats="$(${COMPOSE[@]} exec -T harness python - <<'PY'
import urllib.request

with urllib.request.urlopen("http://mock-upstream:9080/health") as response:
    print(response.read().decode())
PY
)"
  echo "$target upstream stats: $stats"
  "${COMPOSE[@]}" exec -T harness python - "$stats" <<'PY'
import json
import sys

stats = json.loads(sys.argv[1])
counts = stats["counts"]
assert counts.get("hydrate", 0) >= 1, counts
assert counts.get("model", 0) >= 2, counts
assert counts.get("authorized", 0) >= counts["model"], counts
assert counts.get("unauthorized", 0) == 0, counts
sizes = stats["message_sizes"]
assert len(sizes) >= 2 and max(sizes) > min(sizes), sizes
PY

  # Start a deliberately long model turn, then drop E. The controller must finish
  # the admitted event as a failure and tear down the owned native process.
  cancel_id="split-${target}-cancel"
  cancel_task="$(submit_channel cancel "$cancel_id")"
  [ -n "$cancel_task" ] || { echo "cancel admission had no task id" >&2; return 1; }
  wait_cancel_started
  cancel_started="$(date +%s)"
  "${COMPOSE[@]}" kill engine >/dev/null
  cancel_result="$(wait_result cancel "$cancel_id")"
  echo "$target cancellation seconds: $(( $(date +%s) - cancel_started ))"
  echo "$target canceled completion: $cancel_result"
  echo "$cancel_result" | grep -q '"state":"failed"'

  # E failure is observed by H's readiness monitor after the positive evidence.
  for _ in $(seq 1 30); do
    if "${COMPOSE[@]}" exec -T harness python - <<'PY' >/dev/null 2>&1
import urllib.error
import urllib.request

try:
    response = urllib.request.urlopen("http://127.0.0.1:8090/readyz")
except urllib.error.HTTPError as exc:
    raise SystemExit(0 if exc.code == 503 else 1)
raise SystemExit(0 if response.status == 503 else 1)
PY
    then
      echo "$target engine failure: harness readiness became 503"
      return 0
    fi
    sleep 1
  done
  echo "$target harness did not observe engine failure" >&2
  return 1
}

wait_harness_ready() {
  for _ in $(seq 1 60); do
    if "${COMPOSE[@]}" exec -T harness python -c \
      'import urllib.request; raise SystemExit(0 if urllib.request.urlopen("http://127.0.0.1:8090/readyz").status == 200 else 1)' \
      >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "harness did not become ready" >&2
  "${COMPOSE[@]}" logs --no-color harness engine >&2 || true
  return 1
}

wait_cancel_started() {
  for _ in $(seq 1 30); do
    if "${COMPOSE[@]}" exec -T harness python - <<'PY' >/dev/null 2>&1
import json
import urllib.request

with urllib.request.urlopen("http://mock-upstream:9080/health") as response:
    raise SystemExit(0 if json.load(response).get("cancel_started", 0) >= 1 else 1)
PY
    then
      return 0
    fi
    sleep 1
  done
  echo "cancellation fixture did not enter its held inference" >&2
  return 1
}

native_session_ref() {
  "${COMPOSE[@]}" exec -T engine python - <<'PY'
import sqlite3

with sqlite3.connect("/var/lib/ach-agent/home/.ach-execution/sessions.db") as db:
    rows = db.execute(
        "SELECT key, oc_session_id FROM oc_sessions "
        "WHERE key LIKE '%split-acceptance-conversation' ORDER BY last_used DESC"
    ).fetchall()
if len(rows) != 1:
    raise SystemExit(f"expected one native session row, got {rows!r}")
print(f"{rows[0][0]}={rows[0][1]}")
PY
}

assert_engine_forwarded_env() {
  "${COMPOSE[@]}" exec -T engine python - <<'PY'
from pathlib import Path

for proc_dir in Path("/proc").glob("[0-9]*"):
    try:
        command = (proc_dir / "cmdline").read_bytes().replace(b"\0", b" ")
        environment = (proc_dir / "environ").read_bytes().split(b"\0")
    except OSError:
        continue
    if b"opencode" not in command and b"/pi" not in command:
        continue
    if b"DEBUG=engine-value" in environment and b"CUSTOM_TOOL_TOKEN=engine-token" in environment:
        print("native engine received selected forwardEnv names")
        raise SystemExit(0)
raise SystemExit("native engine process did not receive selected forwardEnv values")
PY
}

submit() {
  submit_channel acceptance "$1"
}

submit_channel() {
  local channel="$1" event_id="$2"
  local reply status
  reply="$(curl -fsS -w $'\n%{http_code}' -X POST "http://127.0.0.1:${INGRESS_PORT}/channels/${channel}/events" \
    -H 'content-type: application/json' -H 'idempotency-key: '"$event_id" \
    --data '{"probe":"split"}')"
  status="${reply##*$'\n'}"
  [ "$status" = 202 ] || { echo "event $event_id was not admitted: $reply" >&2; return 1; }
  printf '%s' "${reply%$'\n'*}" | sed -n 's/.*"task_id":"\([^"]*\)".*/\1/p'
}

wait_completion() {
  local channel="$1" event_id="$2"
  "${COMPOSE[@]}" exec -T harness python - "$channel" "$event_id" <<'PY'
import asyncio
import json
import sys

from ach_agent.channels.client import ChannelsClient
from ach_agent.channels.envelopes import EventRef


async def main() -> None:
    channel = sys.argv[1]
    event_id = sys.argv[2]
    with open("/run/ach-agent/channels/bootstrap.json", encoding="utf-8") as stream:
        bundle = json.load(stream)
    harness_url = bundle["harnessUrl"]
    hmac_key = bundle["hmacKey"]
    agent_name = bundle["agentName"]
    client = ChannelsClient(
        harness_url,
        hmac_key.encode(),
        agent=agent_name,
        poll_interval=0.2,
        wait_timeout=25,
    )
    try:
        completion = await client.wait(EventRef(
            agent=agent_name, channel_name=channel, idempotency_key=event_id
        ))
        print(completion.model_dump_json())
        if completion.state != "completed":
            raise SystemExit(f"event {event_id} ended in {completion.state}: {completion.error}")
    finally:
        await client.close()


asyncio.run(main())
PY
}

wait_result() {
  local channel="$1" event_id="$2"
  "${COMPOSE[@]}" exec -T harness python - "$channel" "$event_id" <<'PY'
import asyncio
import json
import sys

from ach_agent.channels.client import ChannelsClient
from ach_agent.channels.envelopes import EventRef


async def main() -> None:
    channel, event_id = sys.argv[1:3]
    with open("/run/ach-agent/channels/bootstrap.json", encoding="utf-8") as stream:
        bundle = json.load(stream)
    harness_url = bundle["harnessUrl"]
    hmac_key = bundle["hmacKey"]
    agent_name = bundle["agentName"]
    client = ChannelsClient(
        harness_url,
        hmac_key.encode(),
        agent=agent_name,
        poll_interval=0.2,
        wait_timeout=25,
    )
    try:
        completion = await client.wait(EventRef(
            agent=agent_name, channel_name=channel, idempotency_key=event_id
        ))
        print(completion.model_dump_json())
    finally:
        await client.close()


asyncio.run(main())
PY
}

for engine_target in engine-opencode engine-pi; do
  run_engine_acceptance "$engine_target"
done
