# R1 probe — opencode usage accumulation scope

**Question:** does `message.updated.info.tokens/cost` (parsed as `OpenCodeUsage`) report
per-message or session-cumulative totals under `session:auto` (reused session across
harness invocations)?

## Procedure

Temporary tap in `src/ach_agent/engine/lifecycle.py`'s `elif isinstance(event, OpenCodeUsage):`
branch, logging `session_id`, `output_tokens`, `cost` per event (reverted after the probe —
see git history for this commit, the tap is not present on disk).

Ran the harness directly from source (`uv run python -m ach_agent.main --debug`) against a
scratch config (`model: gemini.gemini-flash-latest`, `capability.ach.baseUrl:
https://ach.ackstorm.ai`), piping two identical lines into the console REPL so both share the
one warm `tui-console` session:

```
reply with exactly: ok
reply with exactly: ok
```

## Observed

Both invocations shared `session_id=ses_0dcd9cb12ffe33w346q3jh2q1v`.

| invocation | `R1_PROBE_USAGE.output_tokens` sequence |
|---|---|
| 1 | 0, 1, 1 |
| 2 | 0, 1, 1 |

Invocation 2's first/only sequence resets to `0, 1, 1` — identical shape to invocation 1 — it
does **not** continue from invocation 1's final total (`1`). A session-cumulative scope would
have invocation 2 start at/above `1` and grow further.

## Decision

**R1: not-applicable** (per-message scope confirmed). Task 8 (baseline-delta subtraction) is
**SKIPPED** — `SessionStat.build`'s `input_tokens`/`output_tokens`/`cost` inputs are already
per-invocation; no baseline tracking needed.
