"""Durability e2e tests — DUR-01 store selection + DUR-03 SIGTERM drain.

Architecture: hermetic, no live services.

Task 1 (store selection, RED gate):
  - test_store_selection_in_memory: _open_dedup_store with persistence.enabled=False
    returns an InMemoryDedupStore.
  - test_store_selection_file_backed: persistence.enabled=True + writable tmp_path
    returns a FileBackedDedupStore and creates ${mount}/dedup.db.
  - test_missing_mount_exits: persistence.enabled=True + missing mount → SystemExit.
  - test_corrupt_db_fail_open: corrupt dedup.db → fail-open, usable store, metric inc.

Task 2 (drain, RED gate):
  - test_sigterm_flips_readyz: draining=True + ready=False → /readyz 503.
  - test_sigterm_stops_intake: draining=True → POST /channels/.../events → 503.
  - test_sigterm_drain_completes_inflight: in-flight invocation completes before drain
    returns; DRAIN_COMPLETED incremented.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from ach_agent.config.schema import ChannelConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_persistence_cfg(
    *,
    enabled: bool,
    mount_path: str = "/var/lib/ach-agent",
) -> Any:
    """Build a minimal AgentConfig with only persistence + required fields."""
    from ach_agent.config.schema import AgentConfig

    raw = {
        "schemaVersion": "1",
        "agent": {"name": "test", "namespace": "default"},
        "model": {"name": "openai.gpt-5", "type": "openai"},
        "capability": {"ach": {"baseUrl": "https://ach.example", "environment": "test"}},
        "persistence": {
            "enabled": enabled,
            "mountPath": mount_path,
        },
    }
    return AgentConfig.model_validate(raw)


def _make_webhook_cfg(
    secret_path: str,
    name: str = "gitlab-mr-review",
) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": name,
            "type": "webhook",
            "source": "gitlab",
            "webhook": {
                "auth": {"type": "gitlab_token", "secretPath": secret_path},
            },
        }
    )


def _make_headers(secret: str, *, event_uuid: str | None = None) -> dict[str, str]:
    return {
        "X-Gitlab-Token": secret,
        "X-Gitlab-Event": "Merge Request Hook",
        "X-Gitlab-Event-UUID": event_uuid or str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


# ---------------------------------------------------------------------------
# Task 1: Store selection + fail policy (DUR-01, D-03/D-04)
# ---------------------------------------------------------------------------


def test_store_selection_in_memory() -> None:
    """DUR-01 / D-03: persistence.enabled=False → InMemoryDedupStore returned."""
    from ach_agent.main import _open_dedup_store
    from ach_agent.router.dedup import InMemoryDedupStore

    cfg = _make_persistence_cfg(enabled=False)
    store = _open_dedup_store(cfg)
    assert isinstance(store, InMemoryDedupStore), (
        f"Expected InMemoryDedupStore, got {type(store).__name__}"
    )


def test_store_selection_file_backed(tmp_path: Path) -> None:
    """DUR-01 / D-03: persistence.enabled=True + writable mount → FileBackedDedupStore.

    Asserts the dedup.db file is created in the mountPath directory.
    """
    from ach_agent.main import _open_dedup_store
    from ach_agent.router.dedup import FileBackedDedupStore

    cfg = _make_persistence_cfg(enabled=True, mount_path=str(tmp_path))
    store = _open_dedup_store(cfg)
    assert isinstance(store, FileBackedDedupStore), (
        f"Expected FileBackedDedupStore, got {type(store).__name__}"
    )
    db_path = tmp_path / "dedup.db"
    assert db_path.exists(), f"Expected dedup.db at {db_path}, not found"
    # Cleanup
    store.close()


def test_missing_mount_exits() -> None:
    """DUR-01 / D-04a: persistence.enabled=True + missing mount → sys.exit(1) (fail-closed)."""
    from ach_agent.main import _open_dedup_store

    # Use a path guaranteed not to exist
    cfg = _make_persistence_cfg(enabled=True, mount_path="/nonexistent/ach-agent-test-dir")
    with pytest.raises(SystemExit) as exc_info:
        _open_dedup_store(cfg)
    assert exc_info.value.code == 1, f"Expected sys.exit(1), got exit code {exc_info.value.code}"


def test_corrupt_db_fail_open(tmp_path: Path) -> None:
    """DUR-01 / D-04b: corrupt dedup.db → fail-open (move aside, fresh store, metric inc).

    The store must work (can mark/seen) and PERSISTENCE_DEGRADED must be incremented.
    Must NOT raise SystemExit.
    """
    from ach_agent.main import _open_dedup_store
    from ach_agent.router.metrics import PERSISTENCE_DEGRADED

    # Plant a garbage file as dedup.db
    db_path = tmp_path / "dedup.db"
    db_path.write_bytes(b"THIS IS NOT A VALID SQLITE DATABASE GARBAGE BYTES")

    # Record metric value before (use prometheus collect() — no private attrs)
    before_val = list(PERSISTENCE_DEGRADED.collect())[0].samples[0].value

    cfg = _make_persistence_cfg(enabled=True, mount_path=str(tmp_path))
    store = _open_dedup_store(cfg)

    # Must not have raised SystemExit (we are here, so it did not)
    # Store must be usable
    store.mark("test-key", ttl_seconds=3600)
    assert store.seen("test-key"), "Fail-open store must be usable (mark/seen works)"

    # PERSISTENCE_DEGRADED must have been incremented
    after_val = list(PERSISTENCE_DEGRADED.collect())[0].samples[0].value
    assert after_val > before_val, (
        f"PERSISTENCE_DEGRADED must be incremented on fail-open, "
        f"before={before_val}, after={after_val}"
    )

    # Corrupt file must have been moved aside (not deleted).
    # _open_dedup_store uses db_path.with_suffix(f".corrupt.{ts}.db"), so
    # "dedup.db" → "dedup.corrupt.{ts}.db" (with_suffix replaces the last suffix).
    aside_files = list(tmp_path.glob("dedup.corrupt.*.db"))
    assert len(aside_files) >= 1, (
        f"Corrupt dedup.db must be moved aside; no aside file found in {tmp_path}. "
        f"Files present: {list(tmp_path.iterdir())}"
    )

    # Cleanup
    if hasattr(store, "close"):
        store.close()


# ---------------------------------------------------------------------------
# Task 2: SIGTERM drain orchestration (DUR-03, D-09/D-10/D-11/D-12)
# ---------------------------------------------------------------------------


MR_PAYLOAD: dict[str, Any] = {
    "object_kind": "merge_request",
    "project": {"id": 42, "name": "my-repo"},
    "object_attributes": {"iid": 7, "title": "Add feature X", "state": "opened"},
}


class _FakePool:
    """Minimal fake EnginePool for test isolation."""

    def __init__(self, ready: bool = True) -> None:
        self.engine_has_been_ready_once = ready


@pytest.mark.asyncio
async def test_sigterm_flips_readyz(tmp_path: Path) -> None:
    """DUR-03 / D-09: SIGTERM sets draining=True + ready=False → /readyz returns 503.

    Tests via the shared state dict (app.extra['state']) directly — same path
    the drain handler uses — without sending an actual OS signal.
    """
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.http.app import create_app
    from ach_agent.router import Router
    from ach_agent.router.dedup import InMemoryDedupStore

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text("s3cr3t")
    channel_cfg = _make_webhook_cfg(str(secret_file))

    async def fake_engine(event: MessageEvent, on_kill: Any) -> None:
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine,
        delivery_adapter=None,
    )

    pool = _FakePool(ready=True)
    app = create_app(channels=[channel_cfg], handler=router, pool=pool)

    # Flip state via the same dict the drain handler uses
    state: dict[str, Any] = app.extra["state"]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        # Before drain: app lifespan is not running in test mode, so ready=False.
        # We set ready=True manually to prove drain flips it back to False.
        state["ready"] = True
        state["draining"] = False
        resp_before = await client.get("/readyz")
        assert resp_before.status_code == 200, (
            f"Expected 200 before drain, got {resp_before.status_code}"
        )

        # Simulate what _drain does: flip draining + ready
        state["draining"] = True
        state["ready"] = False

        resp_after = await client.get("/readyz")
        assert resp_after.status_code == 503, (
            f"DUR-03: readyz must return 503 after draining=True, got {resp_after.status_code}"
        )


@pytest.mark.asyncio
async def test_sigterm_stops_intake(tmp_path: Path) -> None:
    """DUR-03 / D-12: draining=True → POST /channels/.../events returns 503 (straggler gate).

    No router handler should be invoked.
    """
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.http.app import create_app
    from ach_agent.router import Router
    from ach_agent.router.dedup import InMemoryDedupStore

    handler_called = False

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text("s3cr3t")
    channel_cfg = _make_webhook_cfg(str(secret_file))

    async def fake_engine(event: MessageEvent, on_kill: Any) -> None:
        nonlocal handler_called
        handler_called = True
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine,
        delivery_adapter=None,
    )

    pool = _FakePool(ready=True)
    app = create_app(channels=[channel_cfg], handler=router, pool=pool)
    state: dict[str, Any] = app.extra["state"]

    # Set draining = True (as _drain does)
    state["draining"] = True
    state["ready"] = True  # would be False in prod; test the straggler gate separately

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t"),
        )

    assert resp.status_code == 503, (
        f"D-12: straggler POST must return 503 during drain, got {resp.status_code}: {resp.text}"
    )
    assert not handler_called, "D-12: handler must NOT be called during drain"


@pytest.mark.asyncio
async def test_sigterm_drain_completes_inflight(tmp_path: Path) -> None:
    """DUR-03 / D-11: _drain awaits in-flight lane work, then returns cleanly.

    An in-flight engine invocation that takes ~0.3s is started before _drain is
    called. _drain must wait for it to complete, signal uvicorn via should_exit,
    and return (no sys.exit). DRAIN_COMPLETED is incremented.

    Uses asyncio.Event + asyncio.timeout(5.0) — no naked sleep polling (CLAUDE.md).
    """
    from unittest.mock import MagicMock

    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.engine.metrics import DRAIN_COMPLETED
    from ach_agent.http.app import create_app
    from ach_agent.main import _drain
    from ach_agent.router import Router
    from ach_agent.router.dedup import InMemoryDedupStore

    inflight_done: asyncio.Event = asyncio.Event()
    work_started: asyncio.Event = asyncio.Event()

    secret_file = tmp_path / "hmac_secret"
    secret_file.write_text("s3cr3t")
    channel_cfg = _make_webhook_cfg(str(secret_file))

    async def slow_engine(event: MessageEvent, on_kill: Any) -> None:
        """Simulate a ~0.3s in-flight invocation."""
        work_started.set()
        await asyncio.sleep(0.3)
        inflight_done.set()
        on_kill()

    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=10,
        idempotency_window_seconds=3600,
        dedup_store=InMemoryDedupStore(),
        engine_runner=slow_engine,
        delivery_adapter=None,
    )

    pool = _FakePool(ready=True)
    app = create_app(channels=[channel_cfg], handler=router, pool=pool)
    state: dict[str, Any] = app.extra["state"]

    # Record DRAIN_COMPLETED value before
    before_val = list(DRAIN_COMPLETED.collect())[0].samples[0].value

    # Build a fake uvicorn server stub (only should_exit attr needed)
    fake_uv_server = MagicMock()
    fake_uv_server.should_exit = False

    # Post an event to start the in-flight invocation
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://harness",
    ) as client:
        resp = await client.post(
            "/channels/gitlab-mr-review/events",
            content=json.dumps(MR_PAYLOAD).encode(),
            headers=_make_headers("s3cr3t"),
        )
        assert resp.status_code == 202, f"Expected 202 to start work, got {resp.status_code}"

    # Wait for the slow engine to start
    async with asyncio.timeout(5.0):
        await work_started.wait()

    # Now call _drain — it must wait for the in-flight lane task, then return cleanly.
    # _drain no longer calls sys.exit(0): it signals uvicorn via should_exit and lets
    # main() await uvicorn's graceful shutdown, avoiding the asyncio CancelledError
    # traceback that a force-cancelled serve task produced (03-HUMAN-UAT.md Test 1).
    await _drain(
        state=state,
        uv_server=fake_uv_server,
        cron_scheduler=None,
        router=router,
        dedup_store=InMemoryDedupStore(),
    )

    # _drain must have signaled uvicorn to stop accepting new connections
    assert fake_uv_server.should_exit is True, (
        "_drain must set uv_server.should_exit so uvicorn stops accepting"
    )

    # In-flight work must have completed before _drain returned
    assert inflight_done.is_set(), "D-11: _drain must wait for in-flight invocation to complete"

    # DRAIN_COMPLETED must be incremented
    after_val = list(DRAIN_COMPLETED.collect())[0].samples[0].value
    assert after_val > before_val, (
        f"DRAIN_COMPLETED must be incremented, before={before_val}, after={after_val}"
    )
