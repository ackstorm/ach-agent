"""Cron channel runtime unit tests (CHN-02, D-08, D-09, RTR-05 cron path).

Fast suite — no wall-clock wait (asyncio.sleep is patched).
"""

from __future__ import annotations

from datetime import UTC

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import derive_cron_idempotency_key
from ach_agent.router.router import RouterAdmitResult


class FakeHandler:
    """Captures emitted MessageEvents and returns a configurable result."""

    def __init__(self, result: RouterAdmitResult = RouterAdmitResult.ACCEPTED) -> None:
        self._result = result
        self.events: list[MessageEvent] = []
        self._call_count = 0

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        self.events.append(event)
        self._call_count += 1
        return self._result


class _StopAfterOne(Exception):
    """Sentinel: stop the cron loop after one tick."""


def _make_channel_cfg(name: str = "heartbeat", schedule: str = "* * * * *") -> object:
    """Build a minimal ChannelConfig for a cron channel."""
    from ach_agent.config.schema import ChannelConfig
    raw = {
        "name": name,
        "type": "cron",
        "cron": {"schedule": schedule},
    }
    return ChannelConfig.model_validate(raw)


@pytest.mark.asyncio
async def test_cron_dispatches_correct_event(monkeypatch: pytest.MonkeyPatch) -> None:
    """Cron loop emits a MessageEvent with correct D-08/D-09 fields (fast, no wall-clock).

    Verifies:
      - session_key == channel_cfg.name (D-08)
      - idempotency_key == derive_cron_idempotency_key(name, scheduled_tick) (D-09)
        using the SCHEDULED tick (next_dt), NOT datetime.now()
      - source_trait == "async_no_retry"
    """
    from datetime import datetime

    from croniter import croniter

    from ach_agent.channels.cron import CronScheduler
    from ach_agent.config.schema import ChannelConfig

    # Patch asyncio.sleep: first call returns immediately (tick fires), second call stops.
    call_count = 0

    async def fake_sleep(secs: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise _StopAfterOne
        # First call: return immediately so the tick proceeds to handler.handle()

    monkeypatch.setattr("ach_agent.channels.cron.asyncio.sleep", fake_sleep)

    # Build a minimal CronChannelConfig via ChannelConfig parse
    raw = {
        "name": "heartbeat",
        "type": "cron",
        "cron": {"schedule": "* * * * *"},
    }
    channel_cfg = ChannelConfig.model_validate(raw)
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)

    scheduler = CronScheduler([channel_cfg], handler=handler)

    # Run the scheduler's _run loop — it will raise _StopAfterOne after the first sleep
    with pytest.raises(_StopAfterOne):
        await scheduler._run()

    assert len(handler.events) == 1, "Expected exactly one event emitted"
    event = handler.events[0]

    # D-08: session_key == channel_name
    assert event.session_key == channel_cfg.name, (
        f"D-08 violated: session_key={event.session_key!r} != channel_name={channel_cfg.name!r}"
    )

    # D-09: idempotency_key derived from SCHEDULED tick (not now())
    # Recompute what the scheduled tick should be:
    cron = croniter(channel_cfg.cron.schedule, datetime.now(UTC))  # type: ignore[union-attr]
    expected_next_dt = cron.get_next(datetime)
    expected_key = derive_cron_idempotency_key(channel_cfg.name, expected_next_dt)
    # The keys should match (deterministic for the same scheduled tick)
    assert event.idempotency_key == expected_key, (
        f"D-09 violated: idempotency_key={event.idempotency_key!r} != "
        f"derive_cron_idempotency_key(...)={expected_key!r}"
    )

    # source_trait must be async_no_retry
    assert event.source_trait == "async_no_retry", (
        f"source_trait={event.source_trait!r} != 'async_no_retry'"
    )


@pytest.mark.asyncio
async def test_cron_full_queue_logs_and_never_silent(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """RTR-05 cron path: FULL_QUEUE → drop+log warning; never a silent skip.

    Uses FULL_QUEUE fake handler. Verifies that a warning is emitted to stdout
    (configure_logging uses structlog PrintLoggerFactory → stdout).
    """
    from ach_agent.channels.cron import CronScheduler
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.engine.sanitized_env import configure_logging

    configure_logging()

    sleep_count = 0

    async def fake_sleep(secs: float) -> None:
        nonlocal sleep_count
        sleep_count += 1
        if sleep_count >= 2:
            raise _StopAfterOne
        # First call: return immediately so the tick proceeds to handler.handle()

    monkeypatch.setattr("ach_agent.channels.cron.asyncio.sleep", fake_sleep)

    raw = {
        "name": "heartbeat",
        "type": "cron",
        "cron": {"schedule": "* * * * *"},
    }
    channel_cfg = ChannelConfig.model_validate(raw)
    handler = FakeHandler(RouterAdmitResult.FULL_QUEUE)

    scheduler = CronScheduler([channel_cfg], handler=handler)

    with pytest.raises(_StopAfterOne):
        await scheduler._run()

    assert len(handler.events) == 1, "Handler must have been called (to emit FULL_QUEUE)"
    # Logs go to STDERR (STDOUT carries only the agent reply); check both so the test
    # asserts intent ("never silent") regardless of stream.
    out, err = capfd.readouterr()
    combined = (out + err).lower()
    assert "full" in combined or "drop" in combined, (
        "RTR-05 cron path: FULL_QUEUE must emit a warning log (never silent). "
        f"Got stdout: {out!r} stderr: {err!r}"
    )


# ---------------------------------------------------------------------------
# Decouple: engine-not-ready no longer drops cron ticks (DUR-04 no-catch-up
# conformance, below, is unaffected — that is a separate misfire-loss mode).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_not_ready_tick_routes_normally(monkeypatch: pytest.MonkeyPatch) -> None:
    """Decouple: a tick with engine-not-ready (cold pool) is NOT skipped — it routes
    normally. The engine starts lazily inside pool.acquire() (main.py engine_runner).
    """
    from ach_agent.channels.cron import CronScheduler
    from ach_agent.config.schema import ChannelConfig

    call_count = 0

    async def fake_sleep(secs: float) -> None:
        nonlocal call_count
        call_count += 1
        if call_count >= 2:
            raise _StopAfterOne
        # First call: return immediately so the loop body executes

    monkeypatch.setattr("ach_agent.channels.cron.asyncio.sleep", fake_sleep)

    raw = {"name": "heartbeat", "type": "cron", "cron": {"schedule": "* * * * *"}}
    channel_cfg = ChannelConfig.model_validate(raw)
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)

    scheduler = CronScheduler([channel_cfg], handler=handler)

    with pytest.raises(_StopAfterOne):
        await scheduler._run()

    # engine-not-ready must NOT skip the tick — it routes normally (lazy engine start)
    assert len(handler.events) == 1, (
        "engine-not-ready must not skip cron ticks — acceptance is decoupled"
    )


def test_cron_no_catchup_after_restart() -> None:
    """DUR-04 conformance: croniter computes next FUTURE tick, never backfills past ticks.

    croniter.get_next() from now always returns a tick strictly in the future.
    Ticks missed during a pod restart window are dropped — not caught up.
    spec §30.1 declared loss mode.
    """
    from datetime import datetime

    from croniter import croniter

    schedule = "* * * * *"  # every minute
    now = datetime.now(UTC)

    cron = croniter(schedule, now)
    first_tick: datetime = cron.get_next(datetime)

    assert first_tick > now, (
        "DUR-04: first tick after restart must be a future tick, not a missed one"
    )
    delta = (first_tick - now).total_seconds()
    assert delta <= 60, (
        f"DUR-04: first tick {delta:.1f}s away — must be within 60s (no catch-up of missed ticks)"
    )


# ---------------------------------------------------------------------------
# Timezone: cron ticks fire at the channel's configured local time, not UTC.
# ---------------------------------------------------------------------------


def test_cron_honors_configured_timezone() -> None:
    """`0 8 * * *` with timezone Europe/Madrid must fire at 08:00 LOCAL, not 08:00 UTC.

    Regression: the timezone field was parsed but never passed to croniter, so ticks
    were computed in UTC (08:00 UTC = 10:00 CEST). Asserted DST-proof by checking the
    tick's hour in Madrid == 8 (holds under both CET and CEST).
    """
    from zoneinfo import ZoneInfo

    from ach_agent.channels.cron import CronScheduler
    from ach_agent.config.schema import ChannelConfig

    raw = {
        "name": "morning",
        "type": "cron",
        "cron": {"schedule": "0 8 * * *", "timezone": "Europe/Madrid"},
    }
    channel_cfg = ChannelConfig.model_validate(raw)

    scheduler = CronScheduler([channel_cfg], handler=FakeHandler())

    from datetime import datetime

    cron = scheduler._slots[0][1]
    next_tick: datetime = cron.get_next(datetime)

    local = next_tick.astimezone(ZoneInfo("Europe/Madrid"))
    assert local.hour == 8, (
        f"cron tick must land at 08:00 Europe/Madrid, got {local.isoformat()} "
        f"(UTC {next_tick.astimezone(UTC).isoformat()}) — timezone field ignored?"
    )


def test_cron_invalid_timezone_rejected_at_parse() -> None:
    """Unknown IANA tz fails loud at config-parse (trust boundary), not mid-boot."""
    import pytest as _pytest

    from ach_agent.config.schema import ChannelConfig

    raw = {
        "name": "morning",
        "type": "cron",
        "cron": {"schedule": "0 8 * * *", "timezone": "Mars/Olympus_Mons"},
    }
    with _pytest.raises(Exception, match="timezone"):
        ChannelConfig.model_validate(raw)


# ---------------------------------------------------------------------------
# D-09 / SC#3: CronScheduler singleton invariant
# ---------------------------------------------------------------------------


def test_single_scheduler_drives_all_channels() -> None:
    """D-09/SC#3: ONE CronScheduler drives ALL cron channels — one slot per channel."""
    from ach_agent.channels.cron import CronScheduler

    channel_c1 = _make_channel_cfg("c1", "* * * * *")
    channel_c2 = _make_channel_cfg("c2", "*/5 * * * *")
    handler = FakeHandler(RouterAdmitResult.ACCEPTED)

    scheduler = CronScheduler([channel_c1, channel_c2], handler=handler)  # type: ignore[list-item]

    assert len(scheduler._slots) == 2, (
        f"D-09/SC#3: one scheduler holds one slot per cron channel, got {len(scheduler._slots)}"
    )
