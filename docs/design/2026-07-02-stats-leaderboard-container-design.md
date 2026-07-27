# Stats / Leaderboard Container — Design (Sub-project A)

> **Status:** design approved; two external review rounds incorporated; pending final spec review → plan.
> **Date:** 2026-07-02
> **Scope:** Sub-project **A** only (usage dashboard + eval-score *seam*). The eval/benchmark
> harness that produces the score is **Sub-project B**, a separate spec, tackled next.
> **Reviews:** external design review, 2 rounds (gist `40db75ed`); dispositions in §11.
> **Gating spike:** the **R1 probe** (§4.1) MUST run before sink implementation — it settles the
> one remaining empirical unknown and decides whether the sink needs baseline-delta logic.

## 1. Purpose

Give ach-agent an operator-facing **Model Leaderboard dashboard**: a dark, card + ranked-table UI
(visual language lifted from `../alitellm-auth`) showing, per model, the real operational usage the
harness already measures — spend, tokens, turns, throughput, cache-hit, session volume — plus a
**nullable `score` column** reserved for the future eval harness (B).

Inspiration: the Fable-5 "Model Leaderboard" demo. **Divergence:** its `coding score` is faked seed
data. We refuse to fake it — A ships every column we can source honestly and leaves `score = null`
until B fills it.

### Non-goals (A)
- The eval/benchmark harness and the coding `score` itself (→ Sub-project B).
- Grafana dashboards / provisioning / Prometheus scrape config. **A only emits new counters at the
  already-mounted `/metrics`.** Consumption is ops' job.
- BudgetPanel, CSV export, UsageDonut, RequestsChart (deferred past v0 — §6.4).

## 2. Decomposition

| Sub-project | Produces | Status |
|-------------|----------|--------|
| **A — Stats/leaderboard container** (this doc) | usage dashboard + `score` seam | designing |
| **B — Eval/benchmark harness** | the coding `score` per model | next spec |

Seam: the leaderboard row's `score`. A publishes it nullable; B populates the same store A reads.

## 3. Architecture / topology

```
┌─ ach-agent harness (existing process) ──────────────────┐
│  router → engine → turn-summary (once per invocation)   │
│                        │                                 │
│                        ├──► StatsSink.record()  (NEW)    │  record() does ONLY:
│                        │      ├─ prometheus incr (inline)│  prometheus incr + queue.put_nowait.
│                        │      └─ queue.put_nowait(stat)  │  NEVER touches redis. Cannot block.
│                        │            │                     │
│                        │   ┌────────▼─────────┐           │
│                        │   │ single supervised │──► redis XADD (writer OWNS the client;
│                        │   │ writer task       │          socket timeouts; MINID trim;
│                        │   └───────────────────┘          capped-backoff restart)
│                        └──► /metrics  (existing mount)   │
└─────────┬───────────────────────────┬───────────────────┘
          │ redis stream (v:1 entry)   │ scrape (ops-owned)
          ▼                            ▼
┌─ ach-stats service (NEW container) ─┐   ┌─ Prometheus (ops) ─┐
│  FastAPI + pure aggregate + SPA     │   │  durable aggregates │
│  tolerant reader; auth boundary     │   └─────────────────────┘
└─────────────────────────────────────┘
```

- New code: `src/ach_agent/stats/` (sink + writer) + `src/ach_stats/` (FastAPI + `ui/` React).
- ach-agent becomes polyglot: Python harness + Python stats API + React UI.
- **Topology:** multiple harness pods may write one shared `ach:sessions` stream; a single stats
  service reads. Redis Streams accept concurrent `XADD` — no coordination.

### Core invariant (non-negotiable)
The turn path does exactly two non-blocking things: inline Prometheus increments +
`queue.put_nowait()`. It **never** awaits redis, and **nothing redis-shaped (client construction,
connection acquisition, DNS) ever executes in `record()`** — the writer task owns the client. Any
redis failure (down **or slow**) is absorbed by the writer, never the router. Protects the repo IP.

## 4. Data model

### 4.1 Entry semantics (load-bearing) + the R1 unknown

**One redis entry == one channel-turn == one invocation == one inbound event handled.** This is one
"session" row in the UI (the demo's per-session "TURNS" column = within-invocation loop count =
`stats["tool_count"]`). Recorded values = that invocation's **final** usage
(`ReplyAccumulator.usage()`, keep-latest of `message.updated`, `events.py:386`).

Confirmed against code:
- `acc = ReplyAccumulator(...)` is created **fresh per invocation** (`lifecycle.py:665`); it persists
  only across SSE reconnects **and the ≤1 terminal-output retry** *within* one invocation. So there
  is **no cross-invocation carryover**: an invocation that errors before its first assistant message
  yields `acc.usage() is None` → the entry records zero usage, not the previous run's.
- The ≤1 terminal-output retry is **internal to `run_invocation`** (`terminal_retries`, `main.py:571`)
  → **one record per inbound event, never two rows.** (A retry that re-prompts may *undercount* the
  discarded attempt — an R2-family effect, below — but does not duplicate a row.)

**Totals:** `sessions = COUNT(entries)`, `spend/tokens = SUM(entries)`. Correct only because each
entry holds one invocation's final usage — never a running total across invocations.

**Aborted invocations are recorded** (`status` from `stats["aborted"]`) and **included in `sessions`
and `spend`** — an aborted run burned real tokens and a cost dashboard must never understate cost.
`totals.aborted` (count) is surfaced separately so the KPI row can show it. (Aborted runs are
excluded only from B's *quality* scoring, which is B's aggregation rule, not this entry's.)
**Hard process-kill mid-invocation** produces no record and no Prometheus increment (same site) —
the invocation is invisible to stats. Acceptable for metrics; stated here so it's a known bound.

**Two correctness risks, both empirical, gated on opencode's `message.updated` accumulation scope:**

- **R1 — cross-turn accumulation (catastrophic).** With `session:auto` the opencode session is
  *reused* across invocations. If `info.tokens/cost` is *session*-cumulative (running total over the
  whole reused session, not the current invocation), keep-latest grows every invocation and row-SUM
  inflates **superlinearly**: n equal-cost invocations report `c·n(n+1)/2` instead of `c·n`
  (≈ n/2× over-count, worst on the most-engaged sessions). **Highest risk in the design.**
- **R2 — within-turn undercount (bounded).** If one invocation emits multiple assistant messages
  (or the terminal retry re-prompts), keep-latest drops the earlier ones → the entry undercounts.
  Bounded; opposite direction from R1.

**R1 PROBE (gating spike, run before writing the sink):** one reused session, two trivial prompts
("reply with exactly: ok") against the cheapest model, with a temporary debug tap logging every raw
`message.updated` usage payload. Compare invocation 2's **first** usage event vs invocation 1's
final — starts near zero → per-message scope (R2 territory at worst); starts at/above invocation 1's
total → **R1 confirmed**. ~10 min, a few cents. **Discriminate on `output_tokens`/`cost`, NEVER
`input_tokens`** — in a reused session invocation 2 legitimately resends history, so input tokens
grow even under correct per-message scope (asserting on input/totals false-positives).

**Smallest fix if R1 confirms — baseline snapshot at invocation start:** snapshot the session's
cumulative usage when the invocation begins; record `final − baseline` at the summary site. State
lives in the in-process session object (which `session:auto` already pins for reuse), per-pod, no
eviction policy. Do **not** use a sink-side `dict[session_key → last_seen]` — it leaks without
eviction and puts state in the component we designed stateless.

**Optional field:** `retry: bool` (needs `run_invocation` to expose whether a retry fired) so the
recent table can badge retried runs. `sessions = invocations` regardless; never dedupe (deduping
hides real cost).

### 4.2 Redis — event store (feeds the UI), a **versioned cross-component contract**
The entry schema is a real contract: the harness writes it, the stats service reads it, and they
**deploy independently**. It gets the same rigor as CONTRACT_v3 and the page contract:
- Every entry carries **`v: 1`**. A future breaking change bumps it (detectable, not silently
  misparsed).
- The reader is **tolerant**: missing field → documented default; unknown field → ignored.

- Stream `ach:sessions`, `XADD` auto-ID (=ms ts → `XRANGE start-end` date filter,
  `XREVRANGE COUNT n` recent).
- **Retention 35d** (≥ longest UI range; a 7d default makes "this month" a lie by construction).
  `ACH_STATS_RETENTION` may only **shrink** it for dev. Trim is **owned by the writer**: inline
  approximate trim on every write, `XADD ach:sessions MINID ~ <now−retention> ...` (O(1) amortized,
  no cron, no second component). *Comment at the trim site:* a future "last month" panel needs up to
  62d lookback and 35d would silently break it.
- Entry fields:
  ```
  v:1, ts, session_key, channel, source, model, provider,
  task,            # ≤80 chars, ek_/secret-redacted (only consumer = recent table, §6.3)
  input_tokens, output_tokens, cache_read, cache_write,
  cost, turns, duration_ms, tokens_per_s, status, [retry]
  ```

### 4.3 Prometheus — durable aggregates (ops-owned)
Low-cardinality labels only (`model`, `channel`, `provider`). **Never** `session_key`/`task`.
```
ach_agent_sessions_total{model,channel}         counter
ach_agent_turn_tokens_total{model,direction}    counter   # input|output|cache_read|cache_write
ach_agent_turn_cost_usd_total{model,channel}    counter
ach_agent_turns_total{model,channel}            counter
ach_agent_turn_duration_seconds{model}          histogram
ach_agent_stats_degraded_total                  counter   # sink drops (queue full / writer error)
```
Follows `src/ach_agent/router/metrics.py`; exposed via `/metrics` (`http/app.py:200`).

### 4.4 Leaderboard contract (stats API → UI, page-ready)
UI is a **dumb renderer** (mirrors alitellm-auth `build_stats_contract`, incl. `null`-vs-`0`).
```
range   { start, end, days, coverage_start, tz }   # coverage_start = oldest entry in stream
totals  { sessions, tokens, spend, avg_cost_per_session, aborted, partial }
leaderboard {
  sorted_by: "spend" | "score",                    # explicit; UI renders the header from this
  rows: [ { rank, model, provider,
            score,        # nullable — eval seam (B fills); null → row tagged "unrated"
            speed_tok_s, cost_per_mtok, spend, sessions, tag } ]
}
cost_per_session [ { model, avg } ]
sessions_this_month { rows:[{model,count}], partial }
series [ { date, spend, sessions, tokens, partial } ]
recent [ { ts, task, model, tokens, cost, turns, status, retry } ]
```
- **Per-panel partiality (server-computed, not one page flag):** `totals.partial`,
  `sessions_this_month.partial`, and per-point `series[].partial` are each derived from the single
  `coverage_start` (`coverage_start > panel_window_start`). Page-level `coverage_start` stays for the
  "showing data since {date}" banner. A single page flag would over-warn (7d chart complete while
  month KPI isn't) or lie.
- **Calendar boundaries are timezone-explicit.** `ACH_STATS_TZ` (default `UTC`; we set
  `Europe/Madrid`). "Sessions this month" = calendar month-to-date computed in that TZ. Retention 35d
  ≥ 31d so MTD never runs short; `coverage_start`/`partial` covers cold-start. Without an explicit TZ,
  late-on-the-31st CET sessions land in the wrong month and totals visibly drift for local viewers.
- **Ranking is explicit, never silently flipped.** `sorted_by="spend"` today. When B lands, scores
  arrive per-model (not atomically): scored rows sort by score (desc) **above** unscored rows (which
  sort by spend, tagged "unrated"), and `sorted_by` flips to `"score"`. The reorder is a feature.
- Derived in `aggregate.py`: `speed_tok_s = output_tokens/(duration_ms/1000)`, `cost_per_mtok`,
  `avg_cost_per_session = spend/sessions`.
- `provider`, `tag` from a static **model-metadata map** keyed by model id; unknown →
  `provider="unknown"`, `tag=null`.
- **No version field on this page contract** — the SPA is served from the same image as the API
  (deploy-atomic, no skew). Versioning lives on the redis entry (§4.2), the seam that *does* skew.
- **`budget` deferred** (env-var budget is a toy; add with a real budget source — §6.4).

## 5. Harness changes — the sink (thin, provably non-blocking)

`src/ach_agent/stats/sink.py`, called once at the turn-summary site in `main.py`:

- **`record(SessionStat)`** does **only**: inline Prometheus increments + `queue.put_nowait(stat)`;
  on `asyncio.QueueFull` → drop + `stats_degraded_total`++. Sub-µs, cannot block, cannot accumulate.
  Task text is truncated (≤80) then redacted (structlog `ek_` path) **before** entering the queue.
- **Bounded `asyncio.Queue(maxsize=256)`.** *Reasoning (documented so it isn't cargo-culted):* memory
  bound is irrelevant (256 × ~1KB ≈ 256KB); the number is a **coverage** bound — at a few
  entries/minute arrival and a stalled redis serializing ~1–2 timeout-bound attempts/s, 256 buffers
  **hours** of outage. Rationale: "survives any redis restart/redeploy; longer is a real incident and
  metrics loss is the correct casualty." Drop-on-full is right for a metrics stream —
  block-with-timeout reintroduces the exact latency the queue exists to remove. (`put_nowait` drops
  the *newest*; drop-head/freshest-wins would be marginally better but asyncio.Queue lacks it and at
  this volume it's noise — noted, not built.)
- **Single long-lived supervised writer task** — the only component that touches redis:
  - **Owns** the redis client (created inside the task, never lazily in `record()`); client has
    `socket_connect_timeout` + `socket_timeout ≈ 250–500ms` so a *stalled* socket can't wedge it.
  - `XADD` with inline `MINID ~` trim (§4.2).
  - **Capped-exponential-backoff restart (1s → 30s) + rate-limited log** on persistent failure (bad
    URL / DNS / auth). A naive restart loop would busy-spin CPU and flood logs on the router's
    process — this is the last path where the sink could hurt the harness.
  - **Best-effort shutdown drain:** on harness shutdown, drain the queue with a hard ~2s timeout,
    then drop + count. Difference between "lose data only under failure" and "lose data every deploy".
- **No `ACH_STATS_REDIS_URL` → no queue, no writer; Prometheus still emits.** Dev harness without
  redis boots normally.
- **Pipelining: not in v0** (single-digit entries/min buys nothing, adds partial-failure semantics).
  Recorded future option: *opportunistic* batch — writer drains whatever is queued and pipelines it,
  useful only for backlog recovery after an outage.

**Rejected shapes (recorded so we don't regress):** inline `await xadd()` → slow redis blocks the
turn coroutine; per-turn `create_task(xadd())` → slow redis grows unbounded pending tasks + exhausts
the connection pool (indirect router risk). Bounded-queue + single-writer is the only shape that
bounds both latency and memory.

**Config: env-gated, NOT CONTRACT_v3.** `ACH_STATS_REDIS_URL`, `ACH_STATS_RETENTION`, `ACH_STATS_TZ`.
A `stats:` config block would be a frozen-seam change dragging in the Go operator. **Hygiene:** add
one non-normative line to `CONTRACT_v3.md` — "`ACH_STATS_*` env vars are harness-local and explicitly
outside this contract." Promote to a real config block only if Sub-project B needs the operator to
know about eval infrastructure.

## 6. Stats service (`src/ach_stats/`)

### 6.1 API
- `GET /api/leaderboard?range=&model=` → §4.4 contract
- `GET /api/sessions?range=&model=` → recent list (filters)
- `GET /healthz`; serves the built React SPA (static) at `/`
- reads its own env: `ACH_STATS_REDIS_URL` (read side), `ACH_STATS_TZ`

### 6.2 Aggregation
`aggregate.py` — pure redis-rows → contract; zero HTTP; unit-testable. Lifts alitellm-auth
`stats.py` shape, retargeted, reusing its `null`-vs-`0` guards. **Aggregate-on-read** (window scan +
`defaultdict` group-by): self-healing (the stream is the truth; every render recomputes), no
write-time invariants. ~45k entries (100 sessions/day × 5 turns × 90d) → single-digit ms at
dashboard QPS; a secondary index is YAGNI. Optional 30s in-process TTL cache if latency ever bites
(one dict, no new invariant). Implements the **tolerant reader** (§4.2: missing→default,
unknown→ignore, dispatch on `v`), the per-panel `partial` booleans, and the `ACH_STATS_TZ`
calendar-boundary math.

### 6.3 Auth / boundary
`/api/sessions` serves redacted `task` text on a **new** surface.
- **v0:** the stats service is network-policied to the internal plane, **never** publicly exposed;
  the recent/`task` panel is gated behind that boundary.
- **Target:** lift alitellm-auth's OIDC/Dex auth (it's the UI donor anyway). Tracked as follow-up.

### 6.4 UI (v0 cut)
Lift the `alitellm-auth/src/ui` design system: shadcn `components/ui/*`, `AppShell`, `ThemeToggle`.
**v0 leaves:** new `Leaderboard.tsx` (rank·model·provider·score·speed·$/Mtok·tag), `KpiRow` (incl.
`aborted`), recent-sessions table (badges `status`/`retry`), one `SpendChart`. **Deferred:**
`BudgetPanel`, `ExportCsvButton`, `UsageDonut`, `RequestsChart`. TanStack Query hooks → our `/api`.

## 7. Deploy
- Multi-stage `docker/stats.Dockerfile` (node build UI → python runtime serving static + api),
  explicit `COPY` paths, `.dockerignore` (no `COPY . .`).
- `docker-compose.dev.yml`: add `redis` + `ach-stats`; harness gets
  `ACH_STATS_REDIS_URL=redis://redis:6379`, `ACH_STATS_TZ=Europe/Madrid`.
- Prod: harness emits the new counters at `/metrics`. No Grafana / scrape config / dashboards.

## 8. Error handling & the intended divergence

| Failure | Behavior |
|---------|----------|
| Redis **down or slow** at the sink | writer absorbs (socket timeouts + backoff); queue fills → `record()` drops + `stats_degraded_total`++. Turn never blocks. |
| Writer crashes | capped-backoff restart + rate-limited log + counter. No busy-loop. |
| Harness shutdown with queued entries | best-effort ~2s drain, then drop + count. |
| No `ACH_STATS_REDIS_URL` | Prometheus-only; no queue/writer. |
| Redis unreachable at stats API | `503` → UI error card (exists in lifted design). |
| Prometheus | best-effort; counters always safe to increment. |

**Intended divergence (documented, not a bug):** Prometheus counters are durable; the redis stream
is TTL'd (35d) and is the UI's only source. On a dropped `XADD` the Prometheus counter still
increments while redis misses that entry — the two **will** diverge. Acceptable because they never
share a screen; `coverage_start`/per-panel `partial` tell the UI when its own window is incomplete.
No Prometheus→redis backfill.

## 9. Testing (TDD)
- **R1 probe (manual gating spike, before the sink):** §4.1 — assert on `output_tokens`/`cost`, not
  `input_tokens`. Outcome decides whether baseline-delta logic is needed.
- **Harness sink:**
  - **R1 regression** — two sequential invocations on one reused (`session:auto`) session; assert
    each entry's `output_tokens`/`cost` are independent (non-growing). If baseline-delta is added,
    assert `recorded == final − baseline`.
  - **Stale-carryover guard** — invocation 2 forced to fail pre-LLM → entry 2 has zero usage, not
    entry 1's (already holds via fresh-per-invocation `acc`; guards future refactors).
  - **Retry** — a terminal-retry invocation emits exactly one record; `sessions=invocations`;
    document the undercount bound.
  - **Aborted** — recorded with `status`; included in `totals.sessions`/`spend`; `totals.aborted`++.
  - **Slow-socket** (not just refused) → `record()` returns immediately, stat dropped,
    `stats_degraded_total`++ (fakeredis + stalled socket).
  - **Persistent-failure backoff** — writer against an always-failing redis restarts with capped
    backoff, does **not** busy-loop; logs rate-limited.
  - **Shutdown drain** — queued entries flushed within the timeout, remainder counted.
  - **MINID trim** — entries older than retention are trimmed on write.
  - **No-redis** → Prometheus-only path.
- **Stats API / aggregate.py:** pure units (mirror alitellm-auth `test_stats.py`); **tolerant
  reader** (missing→default, unknown→ignore, `v` dispatch); `sorted_by=spend` then partial-score
  ordering when scores present; per-panel `partial` when the window predates `coverage_start`;
  `ACH_STATS_TZ` calendar-month boundary (e.g. late-31st Madrid session lands in-month);
  `XRANGE` date filter.
- **UI:** vitest + `Leaderboard.tsx` ("unrated" rows, header driven by `sorted_by`), KPI `aborted`,
  recent-table `status`/`retry` badges, "showing data since X" banner on partial.

## 10. Open follow-ups (out of scope for A)
- Sub-project **B**: eval/benchmark harness that fills `score`.
- Stats-API OIDC auth (v0 ships behind a network boundary).
- BudgetPanel + real budget source; CSV export; UsageDonut; RequestsChart.
- If R2 is real: sum-per-message usage in the accumulator (turn-summary fix).
- "Last month" panel → needs ≤62d retention (35d silently breaks it; comment left at the trim site).
- Opportunistic writer pipelining for backlog recovery; drop-head queue policy.
- Grafana dashboards / prod Prometheus scrape config.

## 11. External review disposition (gist `40db75ed`, 2 rounds)

| Point | Verdict | Where |
|-------|---------|-------|
| R1: entry semantics / accumulation scope | **Accepted, corrected across both rounds** — keep-latest per invocation (not sum); risk is R1 (session-cumulative, catastrophic) / R2 (undercount). Probe gates impl; baseline-delta fix if confirmed. | §4.1, §9 |
| R1 test asserted on totals | **Fixed** — assert on `output_tokens`/`cost`; input grows with history. | §4.1, §9 |
| Stale carryover on zero-usage invocation | **Accepted (already holds)** — fresh-per-invocation `acc`; guard test kept. | §4.1, §9 |
| Retry "emits a second record" | **Corrected** — retry is internal to `run_invocation` → one record; effect is undercount, not duplication. | §4.1 |
| `event` discriminator | **Dropped** (conceded) — one record/site + `status`. | §4.1 |
| Aborted runs in totals | **Accepted** — included in spend+sessions; `totals.aborted` surfaced. | §4.1, §4.4 |
| Writer: backoff / client-ownership / shutdown-drain / MINID-trim | **Accepted (4 specifics)** | §5, §8 |
| `maxsize=256` rationale | **Accepted** — coverage bound documented; drop-on-full; drop-head noted-not-built. | §5 |
| Pipelining | **Deferred** — opportunistic backlog batch as future option. | §5, §10 |
| Per-panel partiality | **Accepted** — server-side booleans from one `coverage_start`. | §4.4, §6.2 |
| Timezone calendar boundary | **Accepted** — `ACH_STATS_TZ` (Europe/Madrid). | §4.4, §6 |
| Redis entry = unversioned contract (strongest finding) | **Accepted** — `v:1` + tolerant reader. | §4.2, §6.2 |
| Retention 35d, TZ, "last month" 62d trap | **Accepted** — 35d default + comment at trim site. | §4.2, §10 |
| Nullable-score ranking | **Accepted** — explicit `sorted_by`, "unrated". | §4.4 |
| Env vs CONTRACT_v3 | **Accepted** — non-normative carve-out note. | §5 |
| Cheaper cut | **Accepted** — v0 leaves cut; keep service. | §6.4 |
| Stream vs sorted-set / on-read agg / page-contract versioning | **No change** — confirmed correct / YAGNI. | §4.2, §4.4, §6.2 |
