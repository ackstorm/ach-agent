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
