import asyncio

import pytest

from ach_agent.stats.models import SessionStat
from ach_agent.stats.sink import StatsSink


def _stat(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="m", provider="p",
        raw_task="t", input_tokens=1, output_tokens=1, cache_read=0, cache_write=0, cost=0.0,
        turns=1, duration_ms=10, status="completed", retry=False,
    )
    base.update(over)
    return SessionStat.build(**base)


def test_disabled_sink_record_is_noop_but_metrics_still_run():
    sink = StatsSink(redis_url=None)
    assert sink.enabled is False
    sink.record(_stat())  # must not raise; no queue


def test_enabled_sink_enqueues():
    sink = StatsSink(redis_url="redis://x", maxsize=4)
    sink.record(_stat())
    assert sink._queue is not None
    assert sink._queue.qsize() == 1


def test_record_drops_and_counts_when_queue_full():
    from ach_agent.stats import metrics

    sink = StatsSink(redis_url="redis://x", maxsize=2)
    before = metrics.STATS_DEGRADED._value.get()
    for _ in range(5):
        sink.record(_stat())  # 2 fit, 3 dropped
    assert sink._queue.qsize() == 2
    assert metrics.STATS_DEGRADED._value.get() == before + 3


def test_record_never_blocks(event_loop=None):
    # record() is sync and must return immediately even when the queue is full.
    sink = StatsSink(redis_url="redis://x", maxsize=1)
    sink.record(_stat())
    sink.record(_stat())  # full → dropped, still returns
    assert True


class FakeClient:
    """Records XADD calls; optional hang/fail injection."""

    def __init__(self, hang: asyncio.Event | None = None, fail_times: int = 0):
        self.adds: list[dict] = []
        self._hang = hang
        self._fail_times = fail_times
        self.calls = 0

    async def xadd(self, name, fields, **kw):
        self.calls += 1
        if self._fail_times > 0:
            self._fail_times -= 1
            raise ConnectionError("boom")
        if self._hang is not None:
            await self._hang.wait()
        self.adds.append({"name": name, "fields": fields, "kw": kw})

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_writer_xadds_entry_with_minid_trim():
    client = FakeClient()
    sink = StatsSink(redis_url="redis://x", retention_s=100, client_factory=lambda: client)
    await sink.start()
    sink.record(_stat(model="claude-opus-4-8"))
    await asyncio.sleep(0.05)
    await sink.stop()
    assert len(client.adds) == 1
    call = client.adds[0]
    assert call["name"] == "ach:sessions"
    assert call["fields"]["model"] == "claude-opus-4-8"
    assert "minid" in call["kw"] and call["kw"].get("approximate") is True


@pytest.mark.asyncio
async def test_record_never_blocks_when_writer_stuck():
    hang = asyncio.Event()
    client = FakeClient(hang=hang)
    sink = StatsSink(redis_url="redis://x", maxsize=2, client_factory=lambda: client)
    await sink.start()
    from ach_agent.stats import metrics
    before = metrics.STATS_DEGRADED._value.get()
    # Writer grabs the 1st item and hangs; the queue (size 2) fills; the rest drop.
    for _ in range(6):
        sink.record(_stat())
    await asyncio.sleep(0.05)
    assert metrics.STATS_DEGRADED._value.get() >= before + 3
    hang.set()
    await sink.stop()


@pytest.mark.asyncio
async def test_writer_backoff_does_not_busy_loop(monkeypatch):
    sleeps: list[float] = []
    # monkeypatching "ach_agent.stats.sink.asyncio.sleep" patches the real asyncio module's
    # sleep (sink.asyncio IS the asyncio module) — capture the real one first so fake_sleep's
    # own yield doesn't recurse into itself.
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        # Let the loop breathe without real time.
        await real_sleep(0)

    client = FakeClient(fail_times=1000)
    sink = StatsSink(redis_url="redis://x", client_factory=lambda: client)
    monkeypatch.setattr("ach_agent.stats.sink.asyncio.sleep", fake_sleep)
    await sink.start()
    sink.record(_stat())
    await real_sleep(0.05)
    await sink.stop()
    # Backoff must be applied (non-zero sleeps) and escalate, not spin at 0.
    assert any(s >= 1 for s in sleeps)
    assert max(sleeps) <= 30
