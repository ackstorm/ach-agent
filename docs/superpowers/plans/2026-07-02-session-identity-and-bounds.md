# Session Identity + Bounds (`channel.session` block) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `channel.session: auto|none` with a session block — templated conversation key (default `none`), `maxTokens` growth bound, and `overflow: compact|rotate` — with clean opencode-session deletion for stateless turns.

**Architecture:** `session_key` stays the router lane / pool key (untouched — router invariants frozen). What becomes configurable is only the **conversation identity**: which opencode session a turn reuses. `session.key` is `none` (fresh session per event, DELETEd post-turn), `auto` (channel-derived `session_key`, today's behavior), or a `{{ }}` template rendered per event with the existing zero-dependency engine (`templating/render.py`). `maxTokens` bounds growth by checking the previous turn's `input_tokens` and either compacting the session in place (`POST /session/{id}/compact`) or rotating it (drop LRU entry + DELETE).

**Tech Stack:** Python 3.12, Pydantic v2 (`extra=forbid`), aiohttp, existing `{{ }}` template engine, pytest(+asyncio, `asyncio_mode=auto`), uv/ruff/mypy.

## Prerequisite (HARD)

The persistent-map plan (`docs/superpowers/plans/2026-07-02-persistent-oc-session-map.md`) MUST be merged first. Tasks 3–4 consume its interfaces: `EnginePool.oc_sessions` (`_LRUSessionMap`), `run_invocation(..., oc_sessions=...)`, `_create_oc_session(client)`, and the 404 stale-guard. If `git log` does not show those commits, STOP and escalate.

## Global Constraints

- Virtual environment only — run everything via `uv run …`, never system pip.
- `mypy --strict` and `ruff` stay green.
- No router or Hermes imports in `engine/` modules (D-08, RTR-06).
- **The router lane key derivation is NOT touched.** `event.session_key`, `pool.acquire/release(event.session_key, …)`, dedup, and all bounds stay byte-identical. Only the opencode-session (conversation) key changes.
- `session.key: "none"` must never read nor write the LRU map, and must DELETE its opencode session post-turn.
- The `ek_` bearer is never logged. New log lines carry only `session_key` / `oc_session_id` / config values.
- Template rendering must never fail an event: `render_template` never raises (unresolved tokens → `""` + WARN); an empty/whitespace rendered key falls back to `none` behavior + WARN.
- `--tui` console (no `ChannelConfig`) keeps REPL continuity: absent channel config resolves to `auto` behavior.
- Deletion/compaction are best-effort: failure → WARN, never fail the event.
- Breaking CONTRACT_v3 change (default `auto` → `none`; enum → block). Task 5 updates the contract doc + decision record.

## Verified Facts (live-tested on opencode 1.17.13 — do not re-derive)

- `DELETE /session/{id}` → 200, session removed from `GET /session` listing.
- `POST /session/{id}/compact` with `{}` body → 200. (`…/summarize` exists but 400s without a body — not used.)
- Sessions persist in SQLite under the shared home; without DELETE, every created session accumulates forever (`session: none` today leaks one row per event once the home is persistent).
- `turn_stats["usage"].input_tokens` already reaches `engine_runner` per turn (read by the `engine: summary` log, `main.py` ~line 590).
- Template namespaces (`templating/render.py:79-109`): `payload.*`, `internal.channel.name/type/source`, `internal.agent.name`, `internal.event.id`, `internal.session.key`, `internal.memory.bank`. `header.*` is **reserved and empty** (seam drops headers by design) — header-based session keys are OUT OF SCOPE until headers are threaded across the channel→router seam; a `{{ header.* }}` key today renders empty → falls back to `none` + WARN, which is safe.

---

### Task 1: `SessionBlock` schema + string shorthand

**Files:**
- Modify: `src/ach_agent/config/schema.py` (imports; `ChannelConfig` ~line 432-446)
- Test: `tests/config/test_schema.py` (replace the session test at ~lines 1071-1084; add new tests beside it)

**Interfaces:**
- Produces: `class SessionBlock(BaseModel)` with `key: str = "none"`, `max_tokens: int | None` (alias `maxTokens`, `gt=0`), `overflow: Literal["compact", "rotate"] = "compact"`; and `ChannelConfig.session: SessionBlock` (default `SessionBlock()`, i.e. `key="none"`), accepting the YAML shorthand `session: auto` / `session: none` / `session: "{{ … }}"` (any string → `{"key": <str>}`). Task 4 reads `ch_cfg.session.key/.max_tokens/.overflow`.

- [ ] **Step 1: Replace the old session test and add the new ones**

Find the old test: `grep -n "defaults to 'auto'" tests/config/test_schema.py` (docstring at ~line 1073: `"""channel.session defaults to 'auto', accepts 'none', rejects other strings."""`). Replace that ENTIRE test function with the following (reuse the exact same `cron_block` construction the replaced test used for its `ChannelConfig(...)` calls):

```python
def test_channel_session_block_and_shorthand() -> None:
    """channel.session: defaults to key='none'; string shorthand maps to the block;
    templates are valid keys; unknown block fields are rejected."""
    c = ChannelConfig(name="c", type="cron", **cron_block)
    assert c.session.key == "none"
    assert c.session.max_tokens is None
    assert c.session.overflow == "compact"

    # string shorthand: auto / none / template all become SessionBlock(key=...)
    assert ChannelConfig(name="c", type="cron", session="auto", **cron_block).session.key == "auto"
    assert ChannelConfig(name="c", type="cron", session="none", **cron_block).session.key == "none"
    tmpl = "{{ internal.channel.name }}"
    assert ChannelConfig(name="c", type="cron", session=tmpl, **cron_block).session.key == tmpl

    # full block form
    c2 = ChannelConfig(
        name="c",
        type="cron",
        session={"key": "{{ payload.task_id }}", "maxTokens": 50000, "overflow": "rotate"},
        **cron_block,
    )
    assert c2.session.key == "{{ payload.task_id }}"
    assert c2.session.max_tokens == 50000
    assert c2.session.overflow == "rotate"

    # extra=forbid still bites inside the block
    with pytest.raises(ValidationError):
        ChannelConfig(name="c", type="cron", session={"mode": "auto"}, **cron_block)
    # bad overflow value rejected
    with pytest.raises(ValidationError):
        ChannelConfig(
            name="c", type="cron", session={"key": "auto", "overflow": "explode"}, **cron_block
        )
    # maxTokens must be positive
    with pytest.raises(ValidationError):
        ChannelConfig(
            name="c", type="cron", session={"key": "auto", "maxTokens": 0}, **cron_block
        )
```

NOTE: the pre-existing test at ~line 243 that feeds `"session": {"mode": "auto"}` and expects a `ValidationError` keeps passing unchanged (`mode` is unknown under `extra=forbid`) — do not touch it.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/config/test_schema.py -q -k session_block`
Expected: FAIL — `AttributeError: 'str' object has no attribute 'key'` (session is still the old Literal)

- [ ] **Step 3: Implement**

In `src/ach_agent/config/schema.py`:

(a) Ensure `field_validator` is in the pydantic import line (add it if absent — the file already imports `BaseModel, ConfigDict, Field, model_validator` and friends).

(b) Add `SessionBlock` immediately above `class ChannelConfig` (~line 432):

```python
class SessionBlock(BaseModel):
    """channel.session — conversation identity + growth bounds.

    key: 'none' → fresh opencode session per event, DELETEd post-turn (no residue);
         'auto' → the channel-derived session_key (per-MR for gitlab, name for cron…);
         any other string → {{ }} template rendered per event (payload.* / internal.*);
         an empty render falls back to 'none' behavior + WARN.
    max_tokens: when the previous turn's input_tokens exceed this, apply `overflow`.
    overflow: 'compact' → POST /session/{id}/compact in place (keeps memory);
              'rotate' → drop the LRU entry + DELETE the old session (fresh start).
    The router lane key (event.session_key) is NOT affected by any of this.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    key: str = "none"
    max_tokens: int | None = Field(default=None, alias="maxTokens", gt=0)
    overflow: Literal["compact", "rotate"] = "compact"
```

(c) In `ChannelConfig`, replace line 441:

```python
    session: Literal["auto", "none"] = "auto"
```

with:

```python
    session: SessionBlock = Field(default_factory=SessionBlock)
```

and add inside `ChannelConfig` (next to its existing validator):

```python
    @field_validator("session", mode="before")
    @classmethod
    def _session_shorthand(cls, v: Any) -> Any:
        """YAML shorthand: `session: auto|none|"{{ … }}"` ≡ `session: {key: <str>}`."""
        if isinstance(v, str):
            return {"key": v}
        return v
```

(If `Any` is not already imported in schema.py, add it to the typing import.)

- [ ] **Step 4: Run the config suite**

Run: `uv run pytest tests/config/ -q`
Expected: PASS. If other config tests constructed `ChannelConfig(..., session="auto")`, they still pass via the shorthand. If any test asserts `c.session == "auto"` (string equality), update it to `c.session.key == "auto"` — `grep -n 'session ==' tests/config/test_schema.py` to find them.

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/config/schema.py tests/config/test_schema.py && uv run mypy src/ach_agent/config/schema.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/config/schema.py tests/config/test_schema.py
git commit -m "feat(config): channel.session becomes a block (templated key, maxTokens, overflow)"
```

---

### Task 2: client `delete_session` + `compact_session`

**Files:**
- Modify: `src/ach_agent/engine/client.py` (after `abort_session`, ~line 189)
- Test: `tests/engine/test_client.py` (append after `test_send_message`, ~line 246)

**Interfaces:**
- Produces: `OpenCodeClient.delete_session(session_id: str) -> None` (DELETE `/session/{id}`) and `OpenCodeClient.compact_session(session_id: str) -> None` (POST `/session/{id}/compact` with `{}` body). Both `raise_for_status()`. Task 3's helpers call them.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_client.py` (mirrors the `test_send_message` TestServer pattern at line 217):

```python
# ---------------------------------------------------------------------------
# delete_session / compact_session
# ---------------------------------------------------------------------------


async def test_delete_session_issues_delete() -> None:
    """delete_session issues DELETE /session/{id} (verified live: opencode 1.17 → 200)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    deleted: list[str] = []

    async def handle_delete(request: web.Request) -> web.Response:
        deleted.append(request.match_info["sid"])
        return web.Response(status=200, text="true")

    app = web.Application()
    app.router.add_delete("/session/{sid}", handle_delete)

    async with TestClient(TestServer(app)) as tc:
        from ach_agent.engine.client import OpenCodeClient

        client = OpenCodeClient(str(tc.make_url("")))
        async with client:
            await client.delete_session("ses_del1")

    assert deleted == ["ses_del1"]


async def test_compact_session_issues_post() -> None:
    """compact_session issues POST /session/{id}/compact with a JSON body."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    compacted: list[str] = []

    async def handle_compact(request: web.Request) -> web.Response:
        compacted.append(request.match_info["sid"])
        return web.Response(status=200, text="{}")

    app = web.Application()
    app.router.add_post("/session/{sid}/compact", handle_compact)

    async with TestClient(TestServer(app)) as tc:
        from ach_agent.engine.client import OpenCodeClient

        client = OpenCodeClient(str(tc.make_url("")))
        async with client:
            await client.compact_session("ses_cmp1")

    assert compacted == ["ses_cmp1"]
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/engine/test_client.py -q -k "delete_session or compact_session"`
Expected: FAIL — `AttributeError: 'OpenCodeClient' object has no attribute 'delete_session'`

- [ ] **Step 3: Implement**

In `src/ach_agent/engine/client.py`, after `abort_session` (~line 189):

```python
    async def delete_session(self, session_id: str) -> None:
        """DELETE /session/{id} — remove the session from opencode's store.

        Used by session.key='none' post-turn cleanup and overflow='rotate', so
        stateless turns leave no residue in the persistent home (opencode.db).
        """
        assert self._session is not None, "Call open() first"
        async with self._session.delete(f"{self._base_url}/session/{session_id}") as resp:
            resp.raise_for_status()
            await resp.read()

    async def compact_session(self, session_id: str) -> None:
        """POST /session/{id}/compact — summarize history in place (bounds tokens)."""
        assert self._session is not None, "Call open() first"
        async with self._session.post(
            f"{self._base_url}/session/{session_id}/compact", json={}
        ) as resp:
            resp.raise_for_status()
            await resp.read()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/engine/test_client.py -q`
Expected: PASS

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/client.py tests/engine/test_client.py && uv run mypy src/ach_agent/engine/client.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/client.py tests/engine/test_client.py
git commit -m "feat(engine): client delete_session + compact_session endpoints"
```

---

### Task 3: lifecycle — expose `oc_session_id` via stats + best-effort helpers

**Files:**
- Modify: `src/ach_agent/engine/lifecycle.py` (`run_invocation` body; new helpers next to `_create_oc_session`)
- Test: `tests/engine/test_lifecycle.py` (append; reuses `_server_with_client` helper added by the persistent-map plan)

**Interfaces:**
- Consumes: `_create_oc_session`, the `sessions`/`reused`/`oc_session_id` locals and the 404-guard from the persistent-map plan; `OpenCodeClient.delete_session/compact_session` (Task 2).
- Produces: `run_invocation` writes `stats["oc_session_id"] = oc_session_id` (final value — updated again if the 404 stale-guard re-created the session); module-level `async def discard_oc_session(server: ManagedServer, oc_session_id: str) -> None` and `async def compact_oc_session(server: ManagedServer, oc_session_id: str) -> None`, both best-effort (any exception → `log.warning`, never raise). Task 4 calls all three.

- [ ] **Step 1: Write the failing tests**

Append to `tests/engine/test_lifecycle.py`:

```python
# ---------------------------------------------------------------------------
# stats oc_session_id + discard/compact helpers
# ---------------------------------------------------------------------------


async def test_run_invocation_reports_oc_session_id_in_stats() -> None:
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    server = _server_with_client(19892, AsyncMock(return_value={"id": "ses-stats"}))
    turn_stats: dict = {}

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=False,
            stats=turn_stats,
        )

    assert turn_stats["oc_session_id"] == "ses-stats"


async def test_stats_oc_session_id_reflects_stale_retry() -> None:
    """After the 404 stale-guard recreates the session, stats carries the NEW id."""
    from ach_agent.engine.lifecycle import run_invocation

    canned = '{"action":"none","text":"ok","thoughts":""}'
    shared_map: dict[str, str] = {"key-a": "ses-stale"}
    server = _server_with_client(19893, AsyncMock(return_value={"id": "ses-new"}))
    turn_stats: dict = {}

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        side_effect=[_client_response_error(404), canned],
    ):
        await run_invocation(
            server=server,
            session_id="key-a",
            prompt="p",
            terminal_retries=1,
            reuse=True,
            oc_sessions=shared_map,
            stats=turn_stats,
        )

    assert turn_stats["oc_session_id"] == "ses-new"


async def test_discard_oc_session_swallows_errors() -> None:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, discard_oc_session

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.delete_session = AsyncMock(side_effect=RuntimeError("boom"))
    server = ManagedServer(port=19894)
    server._client = mock_client

    await discard_oc_session(server, "ses-x")  # must not raise
    mock_client.delete_session.assert_awaited_once_with("ses-x")

    # no client at all → silent no-op
    await discard_oc_session(ManagedServer(port=19895), "ses-y")


async def test_compact_oc_session_calls_client() -> None:
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, compact_oc_session

    mock_client = AsyncMock(spec=OpenCodeClient)
    server = ManagedServer(port=19896)
    server._client = mock_client

    await compact_oc_session(server, "ses-z")
    mock_client.compact_session.assert_awaited_once_with("ses-z")
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/engine/test_lifecycle.py -q -k "oc_session_id or discard or compact_oc"`
Expected: FAIL — `KeyError: 'oc_session_id'` and `ImportError: cannot import name 'discard_oc_session'`

- [ ] **Step 3: Implement**

In `src/ach_agent/engine/lifecycle.py`:

(a) In `run_invocation`, immediately after the existing `stats = stats if stats is not None else {}` line, add:

```python
    stats["oc_session_id"] = oc_session_id
```

(b) In the 404 stale-guard branch (from the persistent-map plan), right after `sessions[session_id] = oc_session_id`, add:

```python
        stats["oc_session_id"] = oc_session_id
```

(c) Add the helpers immediately after `_create_oc_session`:

```python
async def discard_oc_session(server: ManagedServer, oc_session_id: str) -> None:
    """DELETE the opencode session, best-effort (session.key='none' / overflow='rotate').

    Failure leaves an orphan row in opencode.db — disk residue only, never worth
    failing the event over. WARN and move on.
    """
    from ach_agent.engine.client import OpenCodeClient

    client = server._client
    if not isinstance(client, OpenCodeClient):
        return
    try:
        await client.delete_session(oc_session_id)
    except Exception:  # noqa: BLE001
        log.warning("engine: session delete failed", oc_session_id=oc_session_id, exc_info=True)


async def compact_oc_session(server: ManagedServer, oc_session_id: str) -> None:
    """POST /session/{id}/compact, best-effort (overflow='compact').

    Failure means the session keeps growing until the next turn retries — WARN only.
    """
    from ach_agent.engine.client import OpenCodeClient

    client = server._client
    if not isinstance(client, OpenCodeClient):
        return
    try:
        await client.compact_session(oc_session_id)
    except Exception:  # noqa: BLE001
        log.warning("engine: session compact failed", oc_session_id=oc_session_id, exc_info=True)
```

- [ ] **Step 4: Run the lifecycle suite**

Run: `uv run pytest tests/engine/test_lifecycle.py -q`
Expected: PASS (all)

- [ ] **Step 5: Lint + typecheck**

Run: `uv run ruff check src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py && uv run mypy src/ach_agent/engine/lifecycle.py`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/engine/lifecycle.py tests/engine/test_lifecycle.py
git commit -m "feat(engine): expose oc_session_id via stats; discard/compact session helpers"
```

---

### Task 4: engine_runner — conversation-key resolution + post-turn cleanup/overflow

**Files:**
- Modify: `src/ach_agent/main.py` (import at ~line 461; the `reuse = …` line at ~561; run_invocation call ~569; post-turn block after the `engine: summary` log ~599)
- Test: `tests/test_main_wiring.py` (append)

**Interfaces:**
- Consumes: `ch_cfg.session` (`SessionBlock`, Task 1), `render_template` + the `ctx` already built in `engine_runner` (~line 473), `run_invocation` stats `oc_session_id` + `discard_oc_session`/`compact_oc_session` (Task 3), `pool.oc_sessions` (persistent-map plan).
- Produces: production behavior — the whole feature wired end to end.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main_wiring.py`. Shared scaffolding first (the file already imports `AsyncMock`/`patch`; add missing imports as needed — `from types import SimpleNamespace`, `from typing import Any`, `from ach_agent.channels.message_event import MessageEvent`, `from ach_agent.engine.lifecycle import EngineConfig`, `from ach_agent.main import _make_engine_runner`, `from ach_agent.config.schema import SessionBlock`):

```python
# ---------------------------------------------------------------------------
# session identity resolution + post-turn cleanup (session block)
# ---------------------------------------------------------------------------


class _SessPool:
    def __init__(self) -> None:
        self.oc_sessions: dict[str, str] = {}

    async def acquire(self, _session_key: str, _cfg: Any) -> Any:
        return object()

    async def release(self, _session_key: str, ttl_seconds: float = 0.0) -> None:
        return None


def _sess_event(session_key: str = "lane-1", channel_name: str = "ch1") -> MessageEvent:
    return MessageEvent(
        idempotency_key="k1",
        session_key=session_key,
        channel_name=channel_name,
        payload={"task_id": "T-42"},
        delivery_context={},
        source_trait="async_no_retry",
    )


def _sess_chcfg(session: SessionBlock) -> Any:
    """Minimal channel-config stand-in: engine_runner only reads .type/.source/.session/.prompt."""
    return SimpleNamespace(type="cron", source=None, session=session, prompt=None)


async def _run_sess_case(
    session: SessionBlock | None,
    *,
    input_tokens: int = 100,
    oc_session_id: str = "ses-t1",
    pool: "_SessPool | None" = None,
) -> tuple[dict[str, Any], Any, Any, Any]:
    """Drive engine_runner once. Returns (run_invocation kwargs, pool, discard mock, compact mock)."""
    import ach_agent.engine.lifecycle as lifecycle

    pool = pool if pool is not None else _SessPool()
    captured: dict[str, Any] = {}

    async def _fake_run(**kw: Any) -> dict[str, Any]:
        captured.update(kw)
        kw["stats"]["oc_session_id"] = oc_session_id
        kw["stats"]["usage"] = SimpleNamespace(
            input_tokens=input_tokens, output_tokens=1, cost=0.0, duration_ms=1
        )
        return {"action": "none", "text": ""}

    channels = {"ch1": _sess_chcfg(session)} if session is not None else {}

    with (
        patch.object(lifecycle, "run_invocation", new=AsyncMock(side_effect=_fake_run)),
        patch.object(lifecycle, "discard_oc_session", new_callable=AsyncMock) as discard,
        patch.object(lifecycle, "compact_oc_session", new_callable=AsyncMock) as compact,
    ):
        runner = _make_engine_runner(
            pool=pool,
            engine_cfg=EngineConfig(),
            max_invocation_seconds=30,
            channels_by_name=channels,
        )
        await runner(_sess_event(), lambda: None)

    return captured, pool, discard, compact


async def test_session_none_fresh_and_deleted() -> None:
    """key='none' (the default): reuse=False and the session is DELETEd post-turn."""
    captured, _pool, discard, _compact = await _run_sess_case(SessionBlock())
    assert captured["reuse"] is False
    discard.assert_awaited_once()
    assert discard.await_args.args[1] == "ses-t1"


async def test_session_auto_reuses_lane_key() -> None:
    """key='auto': conversation key = event.session_key, reuse=True, no delete."""
    captured, _pool, discard, _compact = await _run_sess_case(SessionBlock(key="auto"))
    assert captured["reuse"] is True
    assert captured["session_id"] == "lane-1"
    discard.assert_not_awaited()


async def test_session_template_renders_conversation_key() -> None:
    """A template key renders per event and becomes the conversation (map) key."""
    captured, _pool, discard, _compact = await _run_sess_case(
        SessionBlock(key="{{ payload.task_id }}")
    )
    assert captured["reuse"] is True
    assert captured["session_id"] == "T-42"
    discard.assert_not_awaited()


async def test_session_template_empty_falls_back_to_none() -> None:
    """A template that renders empty behaves as 'none': fresh + deleted (never key='')."""
    captured, _pool, discard, _compact = await _run_sess_case(
        SessionBlock(key="{{ payload.missing_field }}")
    )
    assert captured["reuse"] is False
    discard.assert_awaited_once()


async def test_no_channel_config_keeps_continuity() -> None:
    """--tui console (no ChannelConfig) resolves to auto behavior: REPL continuity."""
    captured, _pool, discard, _compact = await _run_sess_case(None)
    assert captured["reuse"] is True
    assert captured["session_id"] == "lane-1"
    discard.assert_not_awaited()


async def test_max_tokens_overflow_compact() -> None:
    captured, _pool, discard, compact = await _run_sess_case(
        SessionBlock(key="auto", max_tokens=50, overflow="compact"), input_tokens=51
    )
    compact.assert_awaited_once()
    assert compact.await_args.args[1] == "ses-t1"
    discard.assert_not_awaited()


async def test_max_tokens_overflow_rotate() -> None:
    """rotate: LRU entry dropped AND the old session deleted (clean)."""
    pool = _SessPool()
    pool.oc_sessions["lane-1"] = "ses-t1"
    _captured, pool, discard, compact = await _run_sess_case(
        SessionBlock(key="auto", max_tokens=50, overflow="rotate"),
        input_tokens=51,
        pool=pool,
    )
    assert "lane-1" not in pool.oc_sessions
    discard.assert_awaited_once()
    compact.assert_not_awaited()


async def test_max_tokens_not_exceeded_no_action() -> None:
    _captured, _pool, discard, compact = await _run_sess_case(
        SessionBlock(key="auto", max_tokens=50, overflow="compact"), input_tokens=49
    )
    compact.assert_not_awaited()
    discard.assert_not_awaited()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_main_wiring.py -q -k "session_none or session_auto or session_template or no_channel_config or max_tokens"`
Expected: FAIL — first on `AttributeError` (`str` has no `.key`) or `captured["reuse"]` mismatches, depending on Task 1 landing order.

- [ ] **Step 3: Implement**

In `src/ach_agent/main.py`:

(a) Extend the lifecycle import inside `_make_engine_runner` (~line 461):

```python
    from ach_agent.engine.lifecycle import compact_oc_session, discard_oc_session, run_invocation
```

(b) Add `from ach_agent.templating import render_template` to the same import area if `render_template` is not already imported at module level (check — `build_template_context` is; `grep -n render_template src/ach_agent/main.py`).

(c) Replace the single line ~561:

```python
            reuse = getattr(ch_cfg, "session", "auto") != "none"
```

with the resolution block (`ctx` is already in scope — built at ~line 473):

```python
            # Conversation identity (session block). The router lane key
            # (event.session_key) is NOT affected — only which opencode session
            # this turn reuses. No ch_cfg (--tui console) → auto: REPL continuity.
            session_cfg = getattr(ch_cfg, "session", None)
            conv_key = event.session_key
            if session_cfg is None or session_cfg.key == "auto":
                reuse = True
            elif session_cfg.key == "none":
                reuse = False
            else:
                rendered = render_template(session_cfg.key, ctx).strip()
                if rendered:
                    conv_key, reuse = rendered, True
                else:
                    log.warning(
                        "session: template rendered empty — falling back to none",
                        channel=event.channel_name,
                        template=session_cfg.key,
                    )
                    reuse = False
```

(d) In the `run_invocation` call (~569), change `session_id=event.session_key` to:

```python
                session_id=conv_key,
```

(the `oc_sessions=pool.oc_sessions` kwarg from the persistent-map plan stays as is).

(e) Immediately AFTER the `engine: summary` `log.info(...)` call (~line 591-599, after its closing `)`), add the post-turn block:

```python
            # Post-turn session hygiene. Skipped on timeout (this code is not reached
            # when the lane cancels run_invocation) — that orphan is accepted, the
            # server is force-killed anyway.
            _oc_sid = turn_stats.get("oc_session_id", "")
            if _oc_sid and not reuse:
                # key='none' (or empty template render): stateless turn leaves no residue.
                await discard_oc_session(server, _oc_sid)
            elif (
                _oc_sid
                and session_cfg is not None
                and session_cfg.max_tokens is not None
                and getattr(_usage, "input_tokens", 0) > session_cfg.max_tokens
            ):
                if session_cfg.overflow == "compact":
                    log.info(
                        "session: maxTokens exceeded — compacting",
                        session_key=event.session_key,
                        oc_session_id=_oc_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    await compact_oc_session(server, _oc_sid)
                else:  # rotate: drop the map entry + delete the old session (clean)
                    log.info(
                        "session: maxTokens exceeded — rotating",
                        session_key=event.session_key,
                        oc_session_id=_oc_sid,
                        input_tokens=getattr(_usage, "input_tokens", 0),
                        max_tokens=session_cfg.max_tokens,
                    )
                    pool.oc_sessions.pop(conv_key, None)
                    await discard_oc_session(server, _oc_sid)
```

NOTE: `_usage` is the local the summary log already reads (`_usage = turn_stats.get("usage")` at ~line 590) — reuse it, do not re-fetch.

- [ ] **Step 4: Run the wiring + templating + e2e suites**

Run: `uv run pytest tests/test_main_wiring.py tests/test_memory_templating.py tests/e2e/ tests/conformance/ -q`
Expected: PASS. If any pre-existing test constructed a fake `ch_cfg` with `session="auto"`/`session="none"` as a plain string, `getattr(ch_cfg, "session", None)` now returns that string and `.key` access fails — update those fakes to `SessionBlock(key="auto")` (or drop the attr). `grep -rn 'session="' tests/ --include="*.py"` to find them.

- [ ] **Step 5: Full-repo verification**

Run: `uv run pytest -q && uv run ruff check src/ tests/ && uv run mypy src/ach_agent`
Expected: full suite PASS, ruff clean, mypy clean

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/main.py tests/test_main_wiring.py
git commit -m "feat(engine): templated session identity, none default, maxTokens compact/rotate"
```

---

### Task 5: CONTRACT_v3 §2 update + decision record

**Files:**
- Modify: `docs/plan/CONTRACT_v3.md` (§2 channel schema — find the `session:` line under the channel entry spec)
- Create: `docs/references/2026-07-02-session-identity-and-bounds.md`

**Interfaces:**
- Consumes: the shipped behavior of Tasks 1–4.
- Produces: contract text for ach-runtime (the Go operator renders channel config) + the committed decision record.

- [ ] **Step 1: Update CONTRACT_v3 §2**

Find the current session line: `grep -n "session" docs/plan/CONTRACT_v3.md`. Replace the `session: auto|none` schema entry (and its description) with:

```markdown
- `session`: SessionBlock | string shorthand. **BREAKING (v3→v3.1): default changed
  `auto` → `none`.**
  - `key` (string, default `"none"`): conversation identity for the opencode session.
    `"none"` = fresh session per event, deleted post-turn. `"auto"` = the
    channel-derived `session_key` (per-MR for gitlab/github, channel name for
    cron/queue, `context_id` for a2a). Any other string = `{{ }}` template rendered
    per event (`payload.*`, `internal.*`; `header.*` reserved). Empty render →
    `none` behavior + WARN. The router lane key is unaffected.
  - `maxTokens` (int > 0, optional): when the previous turn's `input_tokens` exceed
    this, apply `overflow`.
  - `overflow` (`"compact"` | `"rotate"`, default `"compact"`): `compact` summarizes
    the session in place (POST /session/{id}/compact); `rotate` starts a fresh
    session and deletes the old one.
  - Shorthand: `session: auto` ≡ `session: {key: auto}` (same for `none` / a template).
  - Recommended: `auto` for gitlab/github and a2a channels; `none` (default)
    elsewhere unless conversational memory is wanted.
```

Match the surrounding document's formatting conventions (this is a schema-entry edit, not a rewrite — keep the section's existing style).

- [ ] **Step 2: Write the decision record**

Create `docs/references/2026-07-02-session-identity-and-bounds.md`:

```markdown
# Session identity + bounds (channel.session block)

**Date:** 2026-07-02
**Status:** accepted

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
- `docs/superpowers/plans/2026-07-02-persistent-oc-session-map.md` (substrate:
  pool-owned LRU + 404 guard)
- `docs/superpowers/plans/2026-07-02-session-identity-and-bounds.md` (this plan)
```

- [ ] **Step 3: Commit**

```bash
git add docs/plan/CONTRACT_v3.md docs/references/2026-07-02-session-identity-and-bounds.md
git commit -m "docs: contract v3.1 session block + session-identity decision record"
```
