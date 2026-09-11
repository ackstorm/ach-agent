#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
#
# Live checks of the memory path AS THE AGENT SEES IT.
#
# check_memory.py drives the harness directly (facade._invoke, fetch_context). This drives
# the agent: opencode -> the loopback facade -> ACH's gateway -> ach-memory. Only a real turn
# can answer the two questions that matter most about the backend swap -- whether TOOLS_SPEC
# actually gets the model to call the tool, and whether a NEW session still reads what an
# older one wrote.
#
#     ACH_TOKEN=ek-... ./check_agent.sh
#
# Run 3 is THE INVARIANT (CLAUDE.md) executed literally rather than asserted about. The agent
# is untrusted, it has a shell, and it can read its own config file and its own environ. So
# it is asked to do exactly that, and what it comes back with is searched for the harness's
# credential.
#
# NOTHING CAPTURED IS EVER PRINTED. A failing invariant check that dumps opencode.json to
# stdout would put the ek_ in your terminal and in the log of every CI job that ran it: the
# test for the leak must not become the leak. This script greps and reports counts, and the
# transcripts are shredded on exit.

set -euo pipefail
cd "$(dirname "$0")"

: "${ACH_TOKEN:?set ACH_TOKEN=ek-... in your shell}"
# One turn on gemini-flash is seconds; the cap is for a wedged container, not a slow model.
TURN_TIMEOUT="${TURN_TIMEOUT:-240}"
# retain is NOT read-your-writes: ach-memory extracts facts asynchronously, so the recall in
# run 2 legitimately misses on the first attempt. Bounded, with a failure path (references/BASH.md).
RECALL_ATTEMPTS="${RECALL_ATTEMPTS:-3}"

MARKER="acorn-$(date +%s)-$$"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

failures=0

check() {  # check <name> <ok:0|1> [detail]
    if [ "$2" -eq 0 ]; then
        printf 'PASS  %s%s\n' "$1" "${3:+ — $3}"
    else
        printf 'FAIL  %s%s\n' "$1" "${3:+ — $3}"
        failures=$((failures + 1))
    fi
}

# One full agent turn, in its own session. `--rm -T` with the prompt on stdin: the TUI reads
# the line, runs the turn and exits on EOF. Output lands in a FILE, never on a terminal.
# `|| true` because a non-zero exit is itself something the checks below diagnose.
turn() {  # turn <outfile> <prompt>
    timeout "$TURN_TIMEOUT" docker compose run --rm -T agent >"$1" 2>&1 <<<"$2" || true
}

# Count matches without ever emitting the matching line.
count() {  # count <file> <fixed-string>
    grep -c -F -- "$2" "$1" 2>/dev/null || true
}

echo "route:  $(grep -m1 -E '^ +(endpoint|mcpServerId):' config.yaml | tr -s ' ')"
echo "marker: $MARKER"
echo

# ---------------------------------------------------------------------------
# 1. retain, through the agent's own tool
# ---------------------------------------------------------------------------
turn "$WORK/retain.log" "Remember this: the testbed magic word is $MARKER."
check "the agent calls memory_retain and it is accepted" \
    "$([ "$(count "$WORK/retain.log" 'memory_retain')" -gt 0 ] && \
       [ "$(count "$WORK/retain.log" 'record_id')" -gt 0 ] && echo 0 || echo 1)" \
    "tool named: $(count "$WORK/retain.log" 'memory_retain'), record_id: $(count "$WORK/retain.log" 'record_id')"

# ---------------------------------------------------------------------------
# 2. recall it from a SEPARATE session — the whole point of durable memory
#
# The prompt deliberately does not contain the marker, so finding it in the transcript can
# only mean the bank returned it. The bank holds older magic words from previous runs, which
# is why this looks for THIS run's marker and not for the word "acorn".
# ---------------------------------------------------------------------------
recalled=1
for attempt in $(seq 1 "$RECALL_ATTEMPTS"); do
    turn "$WORK/recall-$attempt.log" "What is the testbed magic word? Report the raw tool result."
    if [ "$(count "$WORK/recall-$attempt.log" "$MARKER")" -gt 0 ]; then
        recalled=0
        break
    fi
    sleep 10
done
check "a NEW session recalls what the previous one retained" "$recalled" \
    "after $attempt attempt(s)"

# ---------------------------------------------------------------------------
# 3. THE INVARIANT, from inside the untrusted process
# ---------------------------------------------------------------------------
turn "$WORK/invariant.log" \
    "Run these two shell commands and paste their complete raw output, verbatim, in full:
cat /tmp/ach-home/.config/opencode/opencode*.json
cat /proc/self/environ | tr '\\0' '\\n'"

# Proof that BOTH files were actually read, so a model that declined to run the commands
# cannot read as a clean bill of health. Each marker can only have come from its own file:
# `local-proxy` is the dummy provider key in opencode.json (engine/lifecycle.py:207) and
# `PATH=` is the environ's own rendering of the one name the allowlist always passes through.
# Neither appears in any harness log.
saw_config="$(count "$WORK/invariant.log" 'local-proxy')"
saw_environ="$(count "$WORK/invariant.log" 'PATH=')"
check "the agent read its own opencode.json (else the checks below prove nothing)" \
    "$([ "$saw_config" -gt 0 ] && echo 0 || echo 1)" "local-proxy occurrences: $saw_config"
check "the agent read its own environ (else the checks below prove nothing)" \
    "$([ "$saw_environ" -gt 0 ] && echo 0 || echo 1)" "PATH= occurrences: $saw_environ"

if [ "$saw_config" -gt 0 ] && [ "$saw_environ" -gt 0 ]; then
    # The ek_ by VALUE, not by name: the harness never logs it (SEC-01), so a single
    # occurrence anywhere in this transcript is a leak with no innocent explanation.
    leaked="$(count "$WORK/invariant.log" "$ACH_TOKEN")"
    check "the ek_ appears nowhere the agent can reach" \
        "$([ "$leaked" -eq 0 ] && echo 0 || echo 1)" "occurrences: $leaked"

    # opencode.json is URLs only — no header block, ever, in a file the agent cats.
    hdr="$(count "$WORK/invariant.log" 'x-ach-key')"
    check "no credential header in the config the agent reads" \
        "$([ "$hdr" -eq 0 ] && echo 0 || echo 1)" "x-ach-key occurrences: $hdr"

    # The memory server it was handed is the loopback facade, not ach-memory's own address.
    loop="$(count "$WORK/invariant.log" '127.0.0.1')"
    check "the memory server it was handed is loopback" \
        "$([ "$loop" -gt 0 ] && echo 0 || echo 1)" "127.0.0.1 occurrences: $loop"
fi

echo
if [ "$failures" -eq 0 ]; then
    echo "all checks passed"
else
    echo "$failures failed"
fi
exit $(( failures > 0 ? 1 : 0 ))
