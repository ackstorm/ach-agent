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


def _channel15() -> ChannelConfig:
    return ChannelConfig.model_validate({
        "name": "quarter",
        "type": "cron",
        "cron": {"schedule": "*/15 * * * *", "timezone": "UTC"},
    })


async def _boot(monkeypatch, now, last, record_exc=None):
    """Run the scheduler from `now` until its first sleep; return admitted event ids,
    recorded ticks and the first sleep length."""
    from ach_runtime.channels import cron

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz is not None else now.replace(tzinfo=None)

    monkeypatch.setattr(cron, "datetime", FixedDateTime)
    started = asyncio.get_running_loop().create_future()
    started.set_result(None)
    ids: list[str] = []
    recorded: list[tuple[str, datetime]] = []
    sleeps: list[float] = []

    class Admission:
        async def accept(self, routed):
            ids.append(routed.event.event_id)
            return SimpleNamespace(started=started)

    class Journal:
        async def last_tick(self, channel):
            return last

        async def record_tick(self, channel, tick):
            if record_exc is not None:
                raise record_exc
            recorded.append((channel, tick))

    async def stop(seconds: float) -> None:
        sleeps.append(seconds)
        raise RuntimeError("stop scheduler test")

    monkeypatch.setattr(cron.asyncio, "sleep", stop)
    control = SimpleNamespace(admission=Admission(), journal=Journal())
    scheduler = cron.CronScheduler([_channel15()], control)
    with pytest.raises(RuntimeError, match="stop scheduler"):
        await scheduler._run()
    return ids, recorded, sleeps


@pytest.mark.asyncio
async def test_cron_catches_up_missed_tick(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 15, 0, 7, tzinfo=UTC)
    last = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)
    ids, recorded, sleeps = await _boot(monkeypatch, now, last)
    assert ids == ["quarter:2026-10-02T15:00:00+00:00"]
    assert recorded[0][0] == "quarter"
    assert recorded[0][1] == datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    assert sleeps == [pytest.approx(15 * 60 - 7)]


@pytest.mark.asyncio
async def test_cron_no_catchup_when_tick_already_ran(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 15, 0, 7, tzinfo=UTC)
    ids, _, _ = await _boot(monkeypatch, now, datetime(2026, 10, 2, 15, 0, tzinfo=UTC))
    assert ids == []


@pytest.mark.asyncio
async def test_cron_no_catchup_without_record(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 15, 0, 7, tzinfo=UTC)
    ids, _, _ = await _boot(monkeypatch, now, None)
    assert ids == []


@pytest.mark.asyncio
async def test_cron_catchup_is_one_tick_never_a_backlog(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 15, 16, tzinfo=UTC)
    last = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)
    ids, _, _ = await _boot(monkeypatch, now, last)
    assert ids == ["quarter:2026-10-02T15:15:00+00:00"]


@pytest.mark.asyncio
async def test_cron_record_tick_failure_still_admits(monkeypatch: pytest.MonkeyPatch) -> None:
    now = datetime(2026, 10, 2, 15, 0, 7, tzinfo=UTC)
    last = datetime(2026, 10, 2, 14, 45, tzinfo=UTC)
    ids, _, _ = await _boot(monkeypatch, now, last, record_exc=OSError("down"))
    assert ids == ["quarter:2026-10-02T15:00:00+00:00"]


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
        async def accept(self, routed):
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
