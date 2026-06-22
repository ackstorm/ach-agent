#!/usr/bin/env bash
# scripts/e2e.sh — Walking-skeleton end-to-end test with real opencode + mock-model.
#
# Brings up docker/mock-model, boots ach-agent with ACH_CONFIG_PATH pointing
# at the cron fixture, asserts a log-only invocation fires, and tears down.
#
# Prerequisites: docker compose v1 or v2, opencode binary on PATH.
#
# Usage:
#   make e2e
#   bash scripts/e2e.sh
#
# Exit codes:
#   0  — all assertions passed
#   1  — FAIL: <assertion description> (named exit)
#
# CLAUDE.md compliance: no naked polling loops.
# Allowed patterns: timeout+grep -m1, bounded for loop with explicit FAIL,
# docker wait (blocking), kubectl wait --timeout.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FIXTURE="${REPO_ROOT}/tests/config/fixtures/config_cron.json"
HARNESS_LOG="${REPO_ROOT}/.e2e-harness.log"
HARNESS_PID_FILE="${REPO_ROOT}/.e2e-harness.pid"
COMPOSE_FILE="${REPO_ROOT}/docker/mock-model/docker-compose.yml"
TIMEOUT_SECONDS=60

# ---------------------------------------------------------------------------
# compose v1/v2 fallback helper
# ---------------------------------------------------------------------------
dc() {
    if command -v docker-compose &>/dev/null; then
        docker-compose -f "${COMPOSE_FILE}" "$@"
    else
        docker compose -f "${COMPOSE_FILE}" "$@"
    fi
}

# ---------------------------------------------------------------------------
# Cleanup: always tear down on exit
# ---------------------------------------------------------------------------
cleanup() {
    echo "e2e: cleanup — tearing down compose stack"
    if [ -f "${HARNESS_PID_FILE}" ]; then
        HARNESS_PID=$(cat "${HARNESS_PID_FILE}" 2>/dev/null || true)
        if [ -n "${HARNESS_PID}" ]; then
            kill "${HARNESS_PID}" 2>/dev/null || true
            wait "${HARNESS_PID}" 2>/dev/null || true
        fi
        rm -f "${HARNESS_PID_FILE}"
    fi
    dc down --volumes --remove-orphans 2>/dev/null || true
    rm -f "${HARNESS_LOG}"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Pre-flight: verify prerequisites
# ---------------------------------------------------------------------------
if [ ! -f "${FIXTURE}" ]; then
    echo "FAIL: cron fixture not found: ${FIXTURE}" >&2
    exit 1
fi

if [ ! -f "${COMPOSE_FILE}" ]; then
    echo "FAIL: docker-compose file not found: ${COMPOSE_FILE}" >&2
    exit 1
fi

if ! command -v opencode &>/dev/null; then
    echo "FAIL: opencode binary not found on PATH" >&2
    exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: Tear down any leftover stack from a previous run
# ---------------------------------------------------------------------------
echo "e2e: pre-up teardown"
dc down --volumes --remove-orphans 2>/dev/null || true

# ---------------------------------------------------------------------------
# Step 2: Start the mock-model compose stack
# ---------------------------------------------------------------------------
echo "e2e: starting mock-model stack"
dc up -d

# Wait for mock-model readiness with bounded retry (no naked polling loop)
MOCK_READY=0
for i in $(seq 1 30); do
    if dc ps | grep -q "running\|Up"; then
        MOCK_READY=1
        break
    fi
    sleep 2
done
if [ "${MOCK_READY}" -eq 0 ]; then
    echo "FAIL: mock-model stack did not become ready within 60s" >&2
    exit 1
fi
echo "e2e: mock-model ready (attempt ${i})"

# ---------------------------------------------------------------------------
# Step 3: Start ach-agent harness in the background
# ---------------------------------------------------------------------------
echo "e2e: starting ach-agent harness"
ACH_CONFIG_PATH="${FIXTURE}" \
    uv run python -m ach_agent.main \
    >"${HARNESS_LOG}" 2>&1 &
HARNESS_PID=$!
echo "${HARNESS_PID}" >"${HARNESS_PID_FILE}"

# Wait for the harness to start (bounded retry — no naked polling loop)
HARNESS_READY=0
for i in $(seq 1 15); do
    if kill -0 "${HARNESS_PID}" 2>/dev/null; then
        HARNESS_READY=1
        break
    fi
    sleep 1
done
if [ "${HARNESS_READY}" -eq 0 ]; then
    echo "FAIL: ach-agent harness did not start within 15s" >&2
    cat "${HARNESS_LOG}" >&2
    exit 1
fi
echo "e2e: harness started (PID=${HARNESS_PID})"

# ---------------------------------------------------------------------------
# Step 4: Assert a log-only invocation fires within TIMEOUT_SECONDS
# (bounded: timeout + grep -m1 — CLAUDE.md compliant)
# ---------------------------------------------------------------------------
echo "e2e: waiting for delivery log line (timeout=${TIMEOUT_SECONDS}s)"
if ! timeout "${TIMEOUT_SECONDS}" grep -m1 "delivery: reply action" "${HARNESS_LOG}" 2>/dev/null; then
    # Give it one more check in case timeout was too fast
    if ! grep -q "delivery: reply action" "${HARNESS_LOG}" 2>/dev/null; then
        echo "FAIL: no 'delivery: reply action' log line within ${TIMEOUT_SECONDS}s" >&2
        echo "--- harness log ---" >&2
        cat "${HARNESS_LOG}" >&2
        exit 1
    fi
fi
echo "e2e: PASS — delivery log line found"

# ---------------------------------------------------------------------------
# Step 5: Assert ek_ never appears in harness log output (SEC-01)
# ---------------------------------------------------------------------------
echo "e2e: asserting ek_ not in log output (SEC-01)"
if grep -q "ek_" "${HARNESS_LOG}" 2>/dev/null; then
    echo "FAIL: ek_ token found in harness log output (SEC-01 violated)" >&2
    grep "ek_" "${HARNESS_LOG}" | head -5 >&2
    exit 1
fi
echo "e2e: PASS — no ek_ in log output (SEC-01)"

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo "e2e: all assertions passed"
exit 0
