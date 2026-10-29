from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from ach_runtime.config.schema import ChannelConfig


def _channel(name: str = "morning") -> ChannelConfig:
    return ChannelConfig.model_validate({
        "name": name,
        "type": "cron",
        "cron": {"schedule": "0 8 * * *", "timezone": "Europe/Madrid"},
    })


@pytest.mark.asyncio
async def test_cron_original_utc_tick_timezone_no_catchup(monkeypatch: pytest.MonkeyPatch) -> None:
    from ach_runtime.channels import cron

    fixed_now = datetime(2026, 10, 2, 7, 30, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now.astimezone(tz) if tz is not None else fixed_now.replace(tzinfo=None)

    monkeypatch.setattr(cron, "datetime", FixedDateTime)
    sleeps: list[float] = []
    tick_admissions: list[object] = []
    started = asyncio.get_running_loop().create_future()
    started.set_result(None)

    async def stop_after_first_tick(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise RuntimeError("stop scheduler test")

    class Admission:
        def admit(self, routed):
            tick_admissions.append(routed)
            return SimpleNamespace(started=started)

    monkeypatch.setattr(cron.asyncio, "sleep", stop_after_first_tick)
    scheduler = cron.CronScheduler([_channel()], SimpleNamespace(admission=Admission()))
    with pytest.raises(RuntimeError, match="stop scheduler"):
        await scheduler._run()

    assert len(tick_admissions) == 1
    routed = tick_admissions[0]
    expected_tick = "2026-10-03T06:00:00+00:00"
    assert routed.event.event_id == f"morning:{expected_tick}"
    assert routed.event.context["cron"]["scheduledTick"] == expected_tick
    assert routed.event.payload["scheduled_tick"] == expected_tick
    assert routed.session_key == expected_tick
    assert routed.event.context["cron"]["scheduledTick"] > fixed_now.isoformat()


@pytest.mark.asyncio
async def test_cron_failed_tick_is_logged_and_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    from ach_runtime.channels import cron

    now = datetime(2026, 10, 2, 7, 30, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    class Stop(Exception):
        pass

    monkeypatch.setattr(cron, "datetime", FixedDateTime)
    attempts: list[object] = []
    warnings: list[tuple[object, ...]] = []

    class Admission:
        def admit(self, routed):
            attempts.append(routed)
            raise RuntimeError("not started")

    async def sleep(seconds: float) -> None:
        if not attempts:
            return
        raise Stop

    monkeypatch.setattr(cron.asyncio, "sleep", sleep)
    monkeypatch.setattr(cron.log, "warning", lambda *args, **kwargs: warnings.append(args))
    scheduler = cron.CronScheduler([_channel()], SimpleNamespace(admission=Admission()))
    with pytest.raises(Stop):
        await scheduler._run()
    assert len(attempts) == 1
    assert warnings
