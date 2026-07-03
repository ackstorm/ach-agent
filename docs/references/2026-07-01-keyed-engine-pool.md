# Keyed EnginePool — design, decisions & session record (2026-07-01)

**Status:** Shipped. Merged to `main` (`c57c92a`, `--no-ff`) and pushed. Validated live in `--tui`.
**Branch:** `feat/keyed-engine-pool` (7 commits).
**Built with:** superpowers `writing-plans` + `subagent-driven-development` (fresh implementer per task,
per-task review, whole-branch review). Plan: `docs/superpowers/plans/2026-07-01-keyed-engine-pool.md`.

> **Stale note (2026-07-03):** two §3 claims drifted. The `engine_has_been_ready_once` gate
> (and `COLD_START_REJECTS`/`COLD_START_DROPS`) is described as surviving as dead state — it has
> since been **fully deleted** (acceptance now gates only on harness readiness + `draining`; see
> router doc B8). The §5 follow-ups `session: auto|none` and codemem/hindsight templating have
> **shipped** (`SessionBlock` in `config/schema.py`). Core pool design (shared HOME + per-session
> `opencode<suffix>.json`, keyed pool) is unchanged and current.

This document records the whole reasoning session — not just the diff — so the *why* survives.

---

## 1. Why we did this

The trigger was a memory question: make codemem's `project` (and, by the same logic, hindsight's
`bankID`) reflect **session identity** rather than a fixed per-agent constant, so memory can be scoped
per repo / per cron / per user. That surfaced a deeper architectural gap: **codemem's
`CODEMEM_PROJECT` is an env fixed at MCP-child spawn**, and the harness ran **one shared opencode
server** for all sessions — so there was no per-session seam to hang a per-session project on.

Resolving "per-session memory" therefore required resolving the pool architecture first. codemem
templating and the `session: auto|none` flag are deliberately **deferred** to follow-ups; this branch
delivers only the seam they need: **a pool keyed by `session_key`.**

Reference implementation studied: `../ackbot-process/src/agent/server_pool.py` — a keyed
`ServerPool` (`dict[server_id → ManagedServer]`, per-key ref-count + TTL).

---

## 2. The model we locked (session identity)

### session_key = the identity, derived per channel

Each channel derives a `session_key` from its own internal facts. This already existed in the router:

| channel | session_key |
|---------|-------------|
| cron    | channel name |
| webhook + gitlab | server + repo (`_parse_gitlab`) |
| webhook + github | `_parse_github` |
| webhook + generic | idempotency-derived (`_parse_generic`) |
| queue   | channel name |
| a2a     | `context_id` |
| tui     | fixed `"tui-console"` (`_CONSOLE_SESSION_KEY`) |

*(future e.g. telegram → per user)*

### "one session_key → one agent, queue the rest" = the router (already correct)

The router keys per-session FIFO lanes by `session_key` (RTR-02): **same key → same lane → serialized
(queued); different keys → parallel** up to `maxConcurrentInvocations`. Two GitLab hooks for the same
repo wait; a hook for another repo runs in parallel. This is the repo's core IP and was **not** changed.
(Note: ackbot is coarser — it keys its server pool by *channel name*, not per repo; the per-repo lane
granularity is ach-agent's own.)

### harness variables vs agent variables

- **harness / boot-static:** e.g. `agent.name` — fixed once at server launch, one per harness.
- **agent / per-session:** e.g. `session_key` — per event, channel-derived. The right candidate for
  codemem `project` / hindsight `bankID` (follow-up).

### session_key ↔ opencode session, and reuse (auto|none) — DEFERRED

`ManagedServer._sessions: dict[session_key → opencode ses_id]` maps the logical key to the real
opencode session. Today it **always reuses** (map never cleared). The planned `session: auto|none`
per-channel flag governs **only** this opencode-session reuse:

- `auto` → reuse the `ses_` across events (conversational continuity).
- `none` → fresh `ses_` each event (clean run, e.g. each cron tick from scratch).

### The "sin colisión" decision (important)

An early idea was: in `none` mode the channel emits `session_key = None`. That is correct for the
**opencode-session** layer (no set → fresh session), but `session_key` does **triple duty** — it is
also the **router lane key** and the **pool key**. A literal `None` there would collapse all `none`
events from all channels into one lane and one server. **Decision: `session_key` stays a stable,
non-`None` identity always** (governs lane + pool); a **separate** `session: auto|none` flag governs
opencode-session reuse only. Same behaviour, no collision. (`session: auto|none` itself is a follow-up.)

### The architecture fork we took

`EnginePool` held **one** shared server (`self._server`), with opencode's own sessions layered on top.
We migrated it to a **keyed pool** (`dict[session_key → ManagedServer]`) modelled on ackbot's
`ServerPool`. Consequence — and the point: **distinct `session_key`s now get separate opencode
processes** (own port, own HOME), instead of concurrent sessions sharing one server. Real per-repo /
per-session isolation, and the seam per-session codemem/hindsight needs.

---

## 3. What shipped

### Keyed `EnginePool` (`src/ach_agent/engine/pool.py`)

- State: `_servers`, `_ref_counts`, `_ttl_tasks`, `_locks` — all `dict[session_key → …]`. Per-key
  `asyncio.Lock` via `_get_lock` (ackbot pattern).
- API: `acquire(session_key, config)`, `release(session_key, ttl_seconds)`, `stop_all()`, plus per-key
  `_expire` / `_stop`.
- `engine_has_been_ready_once` stays a **pool-global** bool (readiness gate; readers in `http/app.py`,
  `channels/cron.py`, `channels/a2a.py` unchanged).
- Lifecycle preserved: `release(ttl=0)` (v1 default for every channel) stops the key's server on last
  release; `ttl>0` schedules expiry; a re-acquire cancels the pending TTL.
- Hardening from review: TTL scheduling moved **inside** the per-key lock (uniform lock discipline);
  a warning is logged on a spurious release (`ref_count==0`).

### Per-key HOME isolation (`_config_for_key`) — the I-1 fix

`acquire` feeds `_start_server` a config whose `home` is per-key:
`<config.home>/servers/oc-<sanitized_key>-<sha1[:8]>`. HOME carries opencode's `opencode.json`, the
`.local/share/opencode` session store, and `node_modules` — so **distinct keys write distinct files;
no shared-file race**. The path is **deterministic in `session_key`**: the same key reuses its home
across invocations (node_modules cache reuse — reinstalled only when a *new* key first appears).
`config.work_dir` (cwd, `/workspace`) is **not** per-key — it was shared before keying too, so leaving
it shared is not a regression.

### Wiring (`src/ach_agent/main.py`)

- `engine_runner`: `acquire(event.session_key, cfg)` and, in `finally`,
  `release(event.session_key, ttl_by_channel[...])` — same key on both (no leak).
- tui warm-up: `acquire(_CONSOLE_SESSION_KEY, warm_cfg)`; tui shutdown: `stop_all()`.

---

## 4. The bug the process caught (I-1)

The per-task reviews (each seeing one task's diff) passed. The **whole-branch review** (run on the most
capable model) caught a real integration bug at the pool↔lifecycle seam that the green suite could not
see — because **every pool test injects a fake `_start_server`**:

> `_default_start_server` launched every keyed server into the single shared `config.home`. Only the
> port was per-key. With `maxConcurrentInvocations ≥ 2` and two active keys (two GitLab repos — the MR
> reviewer case), two `_start_server` calls concurrently truncate-rewrite the **same**
> `opencode.json` → a subprocess reads torn/partial or cross-wired JSON → opencode dies at startup →
> `poll_ready` calls `sys.exit(1)` → **harness crash**.

Fix = per-key HOME (§3). A focused re-review verified the trace end-to-end (`_config_for_key` →
`dataclasses.replace(home=…)` → `launch` → `write_opencode_config`) and confirmed **I-1 CLOSED**, with
same-key concurrency serialized by the per-key lock and the router lane, and path-sanitization safe
against traversal/collision.

Lesson recorded: a fleet of green unit tests that all stub the process-launch boundary cannot see a
race at that boundary. The whole-branch adversarial pass is what caught it.

---

## 5. Follow-ups (tracked, not in this branch)

- **`session: auto|none` per-channel flag** — govern opencode-session reuse (`_sessions` store/lookup)
  per §2. Needs `ttl>0` to keep a keyed server warm between events for `auto`.
- **codemem `project` / hindsight `bankID` templating** — the original motivation; now unblocked
  (`project = {{ session.key }}` or channel-derived) because each key has its own server.
- **M-3 — per-key home disk reaper.** Homes accumulate (one `node_modules` per distinct key, never
  reaped). Bounded for cron/tui/queue/a2a (key = channel name); unbounded for gitlab (server+repo) over
  a long-lived pod → disk-full risk. Documented as an explicit v1 decision in the `EnginePool`
  docstring. TODO: GC `servers/oc-*` for non-live keys (by mtime).
- **M-4 — shared `/workspace` git tree.** Pre-existing; keying makes it easier to hit concurrent git
  index-lock contention / checkout races across distinct-key processes. Out of scope here.

---

## 6. Commits

```
b53ce20 feat(engine): key EnginePool by session_key
b4a76af test(engine): restore non-pool coverage + drop _pending_key hack
dfb26e1 fix(engine): lock-protect TTL scheduling + warn on spurious release
5c02e73 feat(engine): wire engine_runner + tui to keyed pool
7ce6d1a docs(engine): fix stale release() comment for keyed API
ede6d3e fix(engine): isolate opencode HOME per session_key (I-1)
b0ce381 docs(engine): document per-key home disk tradeoff (M-3)
```

Gate at merge: full suite **309 passed, 1 skipped**; `make _lint` (ruff + mypy --strict) clean.

---

## 7. Correction (2026-07-01): per-key HOME → shared HOME + per-session config file

**Branch:** `feat/shared-home-per-session-config`. **Plan:**
`docs/superpowers/plans/2026-07-01-shared-home-per-session-config.md`.

### The split-brain regression

The per-key HOME design described in §3 (`_config_for_key` → `<home>/servers/oc-<key>`) introduced
a split-brain regression: the harness hydrates skills and `.ach-state` once into the **shared**
`engine.home`, but each opencode process ran with a **per-key** HOME — so it never saw the
hydrated skills, prompts, or state. The agente and the harness were looking at different trees.

### The fix

`_config_for_key` was deleted. Instead:

- All agentes run with the **same** `engine.home` (one shared HOME).
- Per-`session_key` isolation is now the **opencode config file**: each key writes
  `<home>/.config/opencode/opencode_<session_key>.json` (+ personality system-prompt file) and
  the agente is launched with `OPENCODE_CONFIG` pointing at it. opencode reads the config path
  from that env var, so no key ever loads another key's model/MCP wiring.

### Issues resolved

- **I-1 (truncate race):** per-key config *file* names (not a shared `opencode.json`) eliminate the
  concurrent-truncate race — each key owns a distinct file path.
- **M-3 (disk bloat):** `node_modules` and the npm/bun caches are shared across keys (no
  per-key reinstall, no disk reaper needed for those paths).

### New accepted tradeoff

The opencode session store (`<home>/.local/share/opencode`) and caches are shared across
concurrent keyed processes. At v1 concurrency levels this is low-risk. If isolation is needed
later, `XDG_DATA_HOME` can be set per-key in the agente env.

### Gate

Full suite **321 passed, 1 skipped**; `make _lint` clean.
