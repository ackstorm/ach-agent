# Router / Engine-Pool / Session lifecycle vs legacy `ackbot-process` — comparison & decision record (2026-07-01)

**Status:** Analysis + decisions. No code shipped by this document. Implementation is split into
follow-up plans (see §8); the first is
`docs/superpowers/plans/2026-07-01-engine-timeout-warm-reuse.md`.

**Trigger:** the question *"are we reinventing in `ach-agent` what `ackbot-process` already had — for
process launch, session keys, session management, idle/TTL, locks, multiple inputs, the bus and the
channels?"*

**Method:** four read-only mapping agents (2 per repo, split router-vs-pool), then direct verification
of every load-bearing claim against source (`lane.py`, `main.py` `engine_runner`, `pool.py`,
`lifecycle.py`). Every bug below was confirmed by reading the code, not inferred.

This record captures the *why* and the verdict so the next session does not re-derive it.

---

## 1. Headline: the hunch is half right

| Concern | Verdict | One-line reason |
|---|---|---|
| **Router / bus / per-session serialization** | **NOT reinvented — ours is genuinely new & better.** Keep. | Legacy has **no** per-session lane. Its `session_key` is dead code. |
| **Engine pool / idle / session lifecycle** | **Reinvented, and the mature part was disabled.** Restore. | Legacy's refcount+TTL idle reaper works; ours is present but hardcoded off (`ttl=0`). |

So: the router is the repo's real IP and was **added**, not copied. The pool is where we rebuilt
`ackbot-process/src/agent/server_pool.py` and lost its battle-tested behavior in the process.

---

## 2. What legacy actually is (this reframes everything)

The single most important finding: **legacy `ackbot-process` never had a per-session router.**

- The "bus" (`src/bus/queue.py`, `src/bus/processor.py:630-641`) is **one global `asyncio.Queue`** feeding
  a fan-out loop that spawns **one `asyncio.create_task` per message**. The only throttle is a
  **per-channel `asyncio.Semaphore`** (`processor.py:257`).
- `InboundMessage.session_key` (`bus/events.py:26`) is **defined and never read** — `grep` finds exactly
  one hit: the definition. Continuity in legacy lives entirely in the process pool + an
  adapter-held `session_id`, not in the bus.
- Consequence: **two events for the same GitLab MR run concurrently** (default `concurrency=3`) against
  the **same opencode filesystem** — the legacy config literally warns "sessions share filesystem —
  concurrent writes may collide" (`config.py:38-40`).
- The sophisticated per-user machinery in legacy's `docs/plans/` (`pool.get_or_create`, `UserManager`,
  `state.json`, dual-concurrency) belongs to a **deleted Slack/Telegram architecture**; it is NOT in the
  current opencode code. `docs/plans/2025-12-03-dual-concurrency-limits.md` describes a per-project cap
  that `grep` confirms **is not in the shipped code** — a mature mechanism legacy itself dropped.

`ach-agent`'s per-session FIFO lane + pinned `dedup → backpressure → lane` + three finite bounds is
therefore a **deliberate correctness property legacy never had.** We did not reinvent a lane; we built one.

---

## 3. Side-by-side

| Axis | legacy `ackbot-process` | `ach-agent` | Winner |
|---|---|---|---|
| session_key identity | dead field; `thread_id` overloaded; hand-filled per handler | real per-channel derivation, guarded never-null (422/reject on empty) | **ach-agent** |
| serialization | per-channel semaphore over unbounded fan-out; same-MR events **race** | per-session FIFO lane, ≤1 in-flight per key (`lane.py:89-124`) | **ach-agent** |
| dedup | none at bus; only gitlab handler (dual-key + post-trigger) | first-class store (mem/SQLite), channel-namespaced, mark-before-enqueue | **ach-agent structure / legacy gitlab logic richer** |
| finite bounds | 2 of 3 (per-chan concurrency, query_timeout); **no `maxQueuedTotal`** | all 3 (`maxConcurrentInvocations`, `maxInvocationSeconds`, `maxQueuedTotal`) | **ach-agent** |
| pool keying | `shared.enabled` boolean fork: channel-name OR req-id | clean 1:1 `session_key` | **ach-agent** |
| HOME isolation | shared-HOME collision hazard (drove the whole ephemeral-home saga) | shared HOME + per-key config file (`OPENCODE_CONFIG`) | **ach-agent** |
| **idle reaper** | refcount+TTL, arms only at refcount 0, acquire cancels expiry (default 300s) | machinery present but **all channel TTLs = 0 → dead**; spawn-per-invocation | **legacy** |
| **supervision / liveness** | mid-query `is_alive()` raises; SSE 3-reconnect health-gated | no mid-query liveness; **live SSE path has no reconnect** | **legacy** |
| watchdog kill | n/a (query_timeout wrap) | **duplicated timeout; the real kill path is dead** (§4 B2) | **bug** |
| runaway control | step-budget abort + tool-only correction retry | none | **legacy** |
| cron driver | APScheduler | croniter loop (per constraint) | **ach-agent** |

---

## 4. Confirmed bugs / regressions in `ach-agent` (verified by direct read)

**B1 — Idle reaper dead → cold start every event, AND `session: auto` silently broken.**
`_CHANNEL_IDLE_TTL_S = {webhook:0, cron:0, queue:0, a2a:0}` (`main.py:594-599`). Every
`release(ttl=0)` runs `_stop` immediately (`pool.py:179-180,193-209`), killing the opencode process —
so:
- Every webhook pays full `opencode serve` startup (`poll_ready`, up to 30 s ceiling) each time.
- `ManagedServer._sessions` (the `session_key → opencode ses_id` map, `lifecycle.py:118`) **dies with
  the process.** The next event for the same key starts a fresh server with an empty map → `create_session()`
  fresh → **no conversational continuity.** `channel.session: auto` (default, `schema.py:378`) therefore
  behaves like `none` for webhook/cron/queue/a2a. Only `--tui` keeps continuity because its warm-up holds
  a ref for the whole REPL. **This is the "we already had it, better" case:** legacy's 300 s refcount+TTL
  reaper is exactly the mechanism needed, and ours is switched off.

**B2 — Duplicated `maxInvocationSeconds`; the genuine watchdog kill is dead.** Two nested
`asyncio.timeout(max_invocation_seconds)` with the **same value**: lane (`lane.py:103`, wraps
`pool.acquire` + memory probe + `run_invocation`) and lifecycle (`lifecycle.py:545`, wraps only the SSE
consume). Because the lane timer starts strictly earlier, its deadline is always sooner → **the lane
always fires first**, cancelling `engine_runner` with `CancelledError`. Lifecycle's `except TimeoutError`
(`:551`) — the only place that does `_process_group_kill` + `ENGINE_WATCHDOG_KILLS.inc()` — **never
runs.** Today the process still dies via `pool.release(ttl=0)` in `engine_runner`'s `finally`
(`main.py:511-515`), so no orphan *at ttl=0*. But the metric is permanently dead, and the bug is armed:
once B1 turns TTL on, the timeout path would arm a warm TTL on a runaway server instead of killing it.

**B3 — `reply_future` hangs the TUI on a lane timeout.** In reply mode the future is resolved only in
`except Exception` / the success path (`main.py:470-481`). A lane timeout injects `CancelledError` (a
`BaseException`, **not** `Exception`) → not caught → future never resolved → `tui.py:180
await reply_future` hangs forever. Any TUI turn exceeding `maxInvocationSeconds` wedges the REPL until
Ctrl-C.

**B4 — No SSE reconnect on the live invocation path.** Live invocations use `consume_sse_after_send`
(no reconnect); the reconnecting `consume_sse_to_completion` (`events.py`, 3-retry health-gated) is dead
code. Legacy reconnects with liveness gating (`opencode.py:214-248`). A transient SSE drop fails the
whole invocation.

**B5 — No mid-invocation liveness.** Legacy raises `"OpenCode died during query"` each SSE iteration via
`is_alive()` (`opencode.py:268-271`). We detect death only lazily at the *next* `acquire()`
(`pool.py:104,113`). (Note: that lazy check *does* correctly cover the warm-reuse path once B1 is on — a
warm server that died between events is replaced on next acquire.)

**B6 — `queue`/`cron` session_key = channel name.** Correct for cron (ticks must serialize). **Wrong for
queue** (`queue.py:189`): collapses all stream messages onto one lane, so the per-channel `concurrency`
semaphore can never exceed 1. Design fork — decide whether redis-queue parallelism is wanted (key by
message/task id) or intentional single-file ordering (keep channel name). **Recommended v1 default: keep
channel name, document the limitation, defer per-message keying.**

**B7 — Latent race + minor leaks/cruft.**
- `_expire` (`pool.py:182-191`) pops itself and calls `_stop` **without re-checking ref-count under the
  lock** — a re-acquire that lands after `sleep` completes but before `_stop` can, in principle, hand out a
  server that `_stop` then kills. Moot at ttl=0; real once B1 is on. Fix: re-check ref/tasks under the lock
  before stopping.
- `release_port` (`client.py:82-84`) never called → `_reserved_ports` grows unbounded for the process life.
- `AtomicCounter` (`admission.py:16-33`) is a no-op ceremony wrapper around an int.
- `delivery_adapter` threaded through `Router.__init__` but always passed `None` (`main.py:912`).
- Stale docs: `CLAUDE.md` + some strings still describe per-key HOME `servers/oc-<key>`; live code uses
  shared home + per-key config file (already corrected in the keyed-pool record §7 — sync remaining strings).

---

## 5. What `ach-agent` does BETTER — keep, do not second-guess

1. **Real `session_key`** — never null, per-channel, guarded. Legacy's is dead. Superior identity model.
2. **Per-session FIFO lane** — ≤1 in-flight per key. Directly fixes legacy's same-MR concurrent-write race.
3. **`maxQueuedTotal`** — the third bound legacy lacks; prevents unbounded task accumulation under a
   redelivery flood.
4. **1:1 pool keying** — kills legacy's `shared.enabled` boolean fork and its cold-start-every-request mode.
5. **Pinned `dedup → backpressure → lane`** — a duplicate returns before consuming a queue slot.
6. **croniter cron**, shared-HOME + per-key config isolation — both cleaner than legacy.

---

## 6. Proven mechanisms to PORT from legacy — do not re-derive

1. **GitLab dual-key dedup + post-trigger ordering** (`gitlab/handler.py:55-89,316`): a logical composite
   key (`type:project:target:user:contenthash`) **plus** the raw event UUID, with dedup run **after**
   trigger classification so an ignored `open` cannot shadow a later `update`. Our generic idempotency-key
   dedup will treat GitLab's `open`+`update` resend as distinct. Port into the gitlab source parser.
2. **Refcount + TTL idle reaper** (`server_pool.py:435-480`): the invariant — TTL arms only at refcount 0,
   any `acquire` cancels the pending expiry. We already have the shape (`pool.py`); restore the behavior
   (B1) and add the `_expire` re-check (B7).
3. **Step-budget abort + tool-only correction retry** (`opencode.py:303-318`, `processor.py:540-584`):
   runaway-turn control (`POST /session/{id}/abort` near the step ceiling) and recovery when a tools-only
   agent leaks prose. We have neither.
4. **SSE reconnect + mid-query liveness** (`opencode.py:214-271`): bounded, health-gated reconnect and
   per-iteration `is_alive()`. Port onto our live path (B4/B5).
5. **contextvars lesson** (`docs/plans/2026-02-22-user-push-race-condition-fix.md`): any per-request routing
   state shared across concurrently-dispatched tasks must be lane/context-local. Our lane structurally
   prevents the classic failure — keep it that way (never stash per-request state on a shared object).

---

## 7. Legacy cruft to NOT copy

- 653-line `MessageProcessor` conflating routing + backpressure + memory-bank provisioning + prompt surgery
  + telemetry + two retry ladders.
- Dead `_BatchAccumulator` (unreachable at `batch_window=0`).
- `shared.enabled` boolean fork (already killed here).
- 511-line config writers (`opencode_extensions.py`) full of personality/plugin baggage.
- Half-bypassed bus (handlers call `process_message()` directly; `publish_inbound`/`run()` vestigial).
- APScheduler cron (we correctly use croniter).

---

## 8. Decision & rollout

**Decisions locked:**
- **Keep the router as-is** (it is the IP and is strictly better than legacy). No structural change.
- **Restore warm reuse in the pool** (B1) — required for `session: auto` correctness, not just perf.
- **Collapse the double timeout to one authoritative lane-level bound** (B2) and **resolve `reply_future`
  on every terminal outcome incl. cancel** (B3). These are prerequisites for B1 (the timeout path must
  force-kill a runaway regardless of TTL).
- **Port** gitlab dual-key dedup, SSE reconnect + mid-query liveness, and step-budget abort — each its own
  plan.
- **B6:** default to keeping channel-name keying for queue in v1; document the no-intra-channel-parallelism
  limitation; per-message keying is a tracked follow-up.

**Priority order (highest ROI first):**
1. **Plan 1 — engine timeout + reply + warm reuse** (B2, B3, B1, B7 `_expire`/`release_port`). Fully
   task-decomposed at `docs/superpowers/plans/2026-07-01-engine-timeout-warm-reuse.md`. These four are
   coupled (warm TTL is unsafe until the timeout path force-kills and the future always resolves) and are
   the only cluster I can spec end-to-end from verified reading. Start here.
2. **Plan 2 — SSE reconnect + mid-query liveness on the live path** (B4, B5). Port from legacy
   `opencode.py`; needs a close read of `engine/events.py` first.
3. **Plan 3 — gitlab dual-key dedup + post-trigger ordering** (port). Needs a read of the webhook/gitlab
   source parser.
4. **Plan 4 — runaway control** (step-budget abort + tool-only correction retry).
5. **Cleanup pass** — `AtomicCounter`, `delivery_adapter`, stale HOME strings, B6 documentation.

Each of Plans 2–4 deserves its own TDD plan when picked up — decomposing them now would be speculative
(unread seams). This record is the shared *why*; the plans are the *how*.

---

## 9. Update (2026-07-01): Plan 1 shipped

`docs/superpowers/plans/2026-07-01-engine-timeout-warm-reuse.md` is implemented and merged.
Closed: **B2, B3, B1, and the B7 `_expire`/`release_port` items** (B7's queue dual-key dedup
stays a Plan 3 follow-up). Behavior now:

- **Single `maxInvocationSeconds` owner (B2).** `run_invocation` no longer self-times-out or
  kills — the lane's `asyncio.timeout(maxInvocationSeconds)` (`router/lane.py`) is the sole
  authoritative bound (RTR-04). `ENGINE_WATCHDOG_KILLS` is incremented in the lane's
  `except TimeoutError` (fires only on a real deadline, never on shutdown-cancel).
- **Always-resolve `reply_future` + force-kill on timeout (B3).** `engine_runner` (`main.py`) is
  a single `try/except CancelledError/except Exception/finally`. A lane deadline (or shutdown)
  cancels it → the cancel branch sets `reply_future` to `InvocationTimeout` (so a TUI/reply turn
  never hangs) and marks `timed_out`. The `finally` releases the pooled server with **`ttl=0`
  when `timed_out`** (force kill of the runaway) else the channel's warm TTL. `pool.acquire` is
  inside the `try`, so a cancel during a cold-start acquire also resolves the future.
- **Configurable warm reuse (B1).** `engine.idle_ttl_seconds` (default **60**, `ge=0`) keeps an
  idle keyed server warm after its last release, so `channel.session: auto` persists the opencode
  session (`ManagedServer._sessions`) across events for the same `session_key` instead of
  respawning. `0` restores spawn-per-invocation. `--tui` is unaffected (it pins a held ref for the
  whole REPL).
- **Race-safe `_expire` (B7).** After its sleep, `EnginePool._expire` re-checks under the key lock:
  it refuses to stop a server whose ref-count is back above 0 or whose TTL task was superseded, and
  the recheck + pop are one atomic critical section (inlined `_stop`).
- **Port release on stop (B7).** `ManagedServer.stop()` now calls `release_port(self.port)`
  (safe on double-stop), so a stopped server frees its reserved ephemeral port.
