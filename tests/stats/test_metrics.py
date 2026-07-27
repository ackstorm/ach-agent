from prometheus_client import REGISTRY

from ach_agent.stats import metrics
from ach_agent.stats.models import SessionStat


def _val(name, labels):
    return REGISTRY.get_sample_value(name, labels) or 0.0


def _stat(**over):
    base = dict(
        ts_ms=1, session_key="k", channel="cron", source="cron", model="glm-5-2",
        provider="zhipu", raw_task="t", input_tokens=100, output_tokens=40, cache_read=0,
        cache_write=0, cost=0.01, turns=2, duration_ms=1000, status="completed", retry=False,
    )
    base.update(over)
    return SessionStat.build(**base)


def test_observe_increments_sessions_and_cost():
    before_s = _val("ach_agent_sessions_total", {"model": "glm-5-2", "channel": "cron"})
    before_c = _val("ach_agent_turn_cost_usd_total", {"model": "glm-5-2", "channel": "cron"})
    metrics.observe(_stat(cost=0.05))
    after_s = _val("ach_agent_sessions_total", {"model": "glm-5-2", "channel": "cron"})
    after_c = _val("ach_agent_turn_cost_usd_total", {"model": "glm-5-2", "channel": "cron"})
    assert after_s == before_s + 1
    assert round(after_c - before_c, 4) == 0.05


def test_observe_increments_input_and_output_tokens():
    b_in = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "input"})
    b_out = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "output"})
    metrics.observe(_stat(input_tokens=100, output_tokens=40))
    a_in = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "input"})
    a_out = _val("ach_agent_turn_tokens_total", {"model": "glm-5-2", "direction": "output"})
    assert a_in == b_in + 100
    assert a_out == b_out + 40


def test_degraded_counter_exists():
    before = _val("ach_agent_stats_degraded_total", {})
    metrics.STATS_DEGRADED.inc()
    assert _val("ach_agent_stats_degraded_total", {}) == before + 1


def test_duration_histograms_have_buckets_past_the_invocation_ceiling():
    """maxInvocationSeconds is 1800 in prod and tool calls routinely pass 10s.

    prometheus_client's default buckets stop at 10s, which pinned every
    histogram_quantile(0.9|0.95|0.99, ...) panel to 10s or +Inf.
    """
    # A labeled histogram has no children (and so no samples) until first observed.
    metrics.TURN_DURATION_SECONDS.labels("m").observe(0.0)
    metrics.TOOL_DURATION_SECONDS.labels("t", "mcp").observe(0.0)
    for name, ceiling in (
        ("ach_agent_turn_duration_seconds_bucket", 1800),
        ("ach_agent_tool_duration_seconds_bucket", 300),
    ):
        les = {
            float(s.labels["le"])
            for m in REGISTRY.collect()
            for s in m.samples
            if s.name == name
        }
        assert les, f"{name} is not registered"
        assert max(les - {float("inf")}) >= ceiling, f"{name} saturates below {ceiling}s"
        assert len({le for le in les if 10 < le < float("inf")}) >= 3
