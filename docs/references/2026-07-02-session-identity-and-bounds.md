# Session identity + bounds (channel.session block)

**Date:** 2026-07-02
**Status:** accepted

> **Update (2026-07-03, v0.6.1, `2940182`):** the config *shape* below was reworked. The
> overloaded `key` field split into an explicit `type: auto | none | custom` discriminator
> (`SessionBlock`, `config/schema.py`); `key` now holds *only* the `{{ }}` template and is
> required iff `type == "custom"`, forbidden otherwise. **Default is `type: "none"`** (not
> `key: "none"`). Shorthand: `auto|none` → `{type: v}`; any other string → `{type: "custom",
> key: v}`. The *behavior* (stateless default, template render, maxTokens/overflow) is
> unchanged — only the field layout moved.

## Problem

`session: auto|none` conflated two identities. `session_key` is (1) the router
lane key — ordering, dedup, concurrency, pool — and (2) the conversation identity
— which opencode session a turn reuses. They coincide for gitlab (per-MR) but
diverge everywhere else: a stateless cron wants a lane but no conversation; a
queue wants one FIFO lane but per-task conversations; a webhook may want the
emitter to name the conversation (header — deferred, see below).

`auto` as default also meant unbounded conversation growth by accident, and
`none` leaked one opencode session row per event into the persistent home
(sessions are stored in SQLite under `~/.local/share/opencode/opencode.db` and
were never deleted).

## Decision

- `channel.session` becomes a block: `key` / `maxTokens` / `overflow`
  (string shorthand `session: auto|none|"{{ … }}"` maps to `{key: …}`).
- **Default `key: "none"`** — the human operator opts into memory explicitly.
  `none` deletes its session post-turn: stateless = no residue.
- `key` accepts a `{{ }}` template (existing zero-dep engine) rendered per event:
  `{{ internal.channel.name }}` (conversational cron), `{{ payload.task_id }}`
  (queue per task). Empty render → `none` + WARN (never a `""` shared key).
- The **router lane key is untouched** — this feature only selects which opencode
  session the engine reuses. Router invariants (dedup → backpressure → lane, the
  three bounds) are frozen.
- `maxTokens` + `overflow`: post-turn check of the turn's `input_tokens`;
  `compact` = POST /session/{id}/compact in place (default — if the operator
  opted into memory, keep it); `rotate` = drop LRU entry + DELETE old session.
- No `ChannelConfig` (--tui console) → `auto` behavior (REPL continuity).

## Verified against opencode 1.17.13 (live)

- Sessions persist in SQLite under HOME; a fresh `opencode serve` on the same
  home lists old sessions and accepts `POST /session/{old_id}/message` → 200.
- Unknown id → 404 `NotFoundError` (clean JSON), not 500.
- `DELETE /session/{id}` → 200, removed from listing.
- `POST /session/{id}/compact` (`{}` body) → 200. `summarize` needs a body → unused.

## Accepted residue / deferred

- LRU eviction (>256 live conversations) orphans the evicted `ses_` row on disk —
  no client is at hand at eviction time. Janitor sweep (GET /session, delete
  unmapped) only if it ever hurts.
- Timeout-cancelled turns skip post-turn cleanup — orphan accepted, the server is
  force-killed anyway.
- `header.*`-based session keys wait on threading inbound headers across the
  channel→router seam (the `header` template namespace is reserved and empty by
  design). A header template today renders empty → safe `none` fallback.
- The `session_key → ses_` map is in-memory (harness restart = fresh
  conversations). Disk persistence is a clean future upgrade — the 404
  stale-guard already covers a map entry outliving opencode's store.

## Related

- `docs/references/2026-07-01-keyed-engine-pool.md` (lane/pool identity)
