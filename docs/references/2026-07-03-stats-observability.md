# Stats & observability — sink design, stream contract, tool trace (2026-07-03)

**Date:** 2026-07-03
**Status:** Shipped (on `main`, v0.6.x). Harness-local, env-gated, NOT part of `CONTRACT_v3`.

Records the *why* of the per-invocation + per-tool observability path: the harness-side sink
(`src/ach_agent/stats/`), the redis-stream contract the separate `ach-stats` reader consumes,
the Prometheus surface, and the Tier-1 tool trace. Plans:
`docs/superpowers/plans/2026-07-02-stats-sink-harness-a1.md` (sink),
`docs/superpowers/plans/2026-07-02-stats-container-a2.md` (reader/UI).

## Problem

The harness runs managed agents but emitted nothing durable about *what they did* — tokens,
cost, duration, tool use, per session. Two consumers want different shapes: operators want
live aggregates (`/metrics`), and a dashboard (`ach-stats`, `src/ach_stats/` + React UI) wants
queryable per-invocation history. Neither may slow or break a turn — the router hot path and
`maxInvocationSeconds` bound are load-bearing; observability is not.

## Decisions

1. **Two-tier sink, never in the hot path.** `StatsSink.record()` does ONLY an inline
   Prometheus `observe()` + a `queue.put_nowait()` — it never awaits and never raises
   (`sink.py`). A single supervised writer task owns the redis client and drains a bounded
   queue via `XADD`. A full queue increments `ach_agent_stats_degraded_total` and drops the
   record — degrade, never block.
2. **Redis stream = cross-component contract.** The harness *writes* streams; `ach-stats`
   *reads* them; the two deploy independently. So every entry carries `v="1"` — a breaking
   field change bumps `v`. Stream (not direct DB write) gives decoupling, replay, and retention
   via `XADD ... MINID` (default 35 days, `ACH_STATS_RETENTION`).
3. **Two streams, one writer class.** `ach:sessions` (one entry per invocation, `SessionStat`)
   and `ach:tools` (one entry per tool call, `ToolStat`). `StatsSink` is parametrized by
   `stream` + an `on_record` metrics hook so the queue/backoff/reconnect/retention writer is
   shared, not duplicated.
4. **Prometheus labels stay low-cardinality** — `model`, `channel`, `tool`, `tool_type`,
   `status` only. NEVER `session_key` or task text (cardinality explosion). Per-session detail
   lives in the stream, not in metric labels.
5. **Env-gated, harness-local.** `ACH_STATS_REDIS_URL` unset → Prometheus-only (no queue, no
   writer). These `ACH_STATS_*` vars are harness-local and deliberately NOT in `CONTRACT_v3`
   (the operator does not render them).
6. **`provider` resolved downstream.** Both stats set `provider="unknown"`; the `ach-stats`
   service maps model→provider (its "A2" model-map). The harness stays dumb about provider
   naming.
7. **Tier-1 tool trace reuses SSE, no new infra.** opencode's SSE already emits per-tool
   lifecycle events (`OpenCodeToolUpdate`: tool name, call_id, input, output/error, status),
   already parsed for rendering + count, then discarded. Tier 1 records them: a per-invocation
   `on_tool` wrapper (`main.py:_make_tool_recorder`) stamps a monotonic start on `running`,
   computes duration on `completed`/`error`, dedups per `call_id`, and writes a `ToolStat`.
   Metrics always on; `ach:tools` stream only when redis is configured.
8. **OTel alignment, not adoption.** `ToolStat` fields are OTel `gen_ai.*`-named
   (`tool`→`gen_ai.tool.name`, `session_key`→`gen_ai.conversation.id`, `status=error`→
   `error.type`) so a future OTLP export maps 1:1 — but no OTel SDK, no spans/traces. That
   (Tier 2) is deferred until a collector consumes them. Cost has no OTel metric by design;
   we keep `ach_agent_turn_cost_usd_total` regardless.

## Stream field contracts (`v="1"`)

- **`ach:sessions`** (`SessionStat.to_entry`): `ts`, `session_key`, `channel`, `source`,
  `model`, `provider`, `task` (redacted+truncated), `input_tokens`, `output_tokens`,
  `cache_read`, `cache_write`, `cost`, `turns`, `duration_ms`, `tokens_per_s`, `status`,
  `retry`.
- **`ach:tools`** (`ToolStat.to_entry`): `ts`, `session_key`, `channel`, `source`, `model`,
  `provider`, `tool`, `tool_type` (mcp|builtin), `status` (completed|error), `duration_ms`
  (`""` when the `running` event was missed), `input_size`, `output_size`, `error`
  (truncated). Stores SIZES, never raw args/result (secrets/bloat).

## Prometheus surface

`ach_agent_sessions_total`, `ach_agent_turn_tokens_total{direction}`,
`ach_agent_turn_cost_usd_total`, `ach_agent_turns_total`, `ach_agent_turn_duration_seconds`,
`ach_agent_stats_degraded_total`; and per-tool `ach_agent_tool_calls_total{tool,tool_type,
status}` + `ach_agent_tool_duration_seconds{tool,tool_type}`.

## Deferred / accepted ceilings

- **Tool duration = SSE arrival delta** (`running`→terminal), not opencode's own tool clock.
  Miss the `running` event → `duration_ms` None, count still recorded. Good enough; upgrade
  path is parsing opencode's `state.time` if precision ever matters.
- **No raw tool args/result captured** — sizes only. Add behind a flag if a consumer needs them.
- **Tier 2 (OTLP spans/traces)** not built — add when a collector exists.
- **`ach:tools` has no UI drill-down yet** — the stream mirrors `ach:sessions` for a future
  per-tool view, same as `ach:sessions` predated the leaderboard.
- **Eval/quality score** (per-invocation grading) is a separate future sub-project (B).

## Related

- `docs/references/2026-07-01-keyed-engine-pool.md` (session_key identity feeding the streams)
- `docs/superpowers/plans/2026-07-02-stats-sink-harness-a1.md` / `-stats-container-a2.md`
- OTel GenAI semconv: `github.com/open-telemetry/semantic-conventions-genai` (field-name source)
