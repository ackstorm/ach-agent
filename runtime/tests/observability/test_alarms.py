# SPDX-License-Identifier: MIT
from __future__ import annotations

from prometheus_client import REGISTRY, generate_latest

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.observability import IdentityRegistry, RunRecord, RunUsage, alarm, metrics


def test_metric_observation_and_identity_collection_do_not_mutate_shared_samples() -> None:
    identity = ProcessIdentity(agent="test-agent", environment="test-env")
    record = RunRecord("test-agent", "test-env", "inv", "run", None, None,
                       "webhook", "ws", "session", "config", "model", 0,
                       "completed", RunUsage("opencode", "engine_estimate", 0, 1, 2,
                                              0, 0, 0, 1))
    metrics.observe(record)
    before = list(metrics.TURNS_TOTAL.collect()[0].samples)
    wrapped = IdentityRegistry(identity=identity)
    rendered = generate_latest(wrapped).decode()
    after = list(metrics.TURNS_TOTAL.collect()[0].samples)
    assert before == after
    assert "ach_agent_turns_total" in rendered
    assert 'agent="test-agent"' in rendered and 'environment="test-env"' in rendered


def test_unknown_usage_adds_only_completion_and_alarm_kind_is_finite() -> None:
    before = metrics.ANOMALIES_TOTAL.labels(kind="usage_invalid")._value.get()
    alarm("usage_invalid")
    output = generate_latest(IdentityRegistry(identity=ProcessIdentity(
        agent="a", environment="e"
    ))).decode()
    assert (
        f'ach_agent_anomalies_total{{agent="a",environment="e",kind="usage_invalid"}} {before + 1}'
        in output
    )


def test_negative_finite_native_usage_is_rejected_by_counters_but_kept_in_record() -> None:
    before_tokens = metrics.TURN_TOKENS_TOTAL.labels(model="negative-model", direction="input")._value.get()
    before_cost = metrics.TURN_COST_USD_TOTAL.labels(model="negative-model", channel="webhook")._value.get()
    before_anomaly = metrics.ANOMALIES_TOTAL.labels(kind="usage_invalid")._value.get()
    usage = RunUsage("opencode", "engine_estimate", -2, None, None, None, None, -0.5, 1)
    record = RunRecord("agent", "env", "negative-inv", None, None, None, "webhook",
                       "ws", "session", "config", "negative-model", 1,
                       "completed", usage)
    assert usage.to_dict()["input"] == -2
    metrics.observe(record)
    assert metrics.TURN_TOKENS_TOTAL.labels(model="negative-model", direction="input")._value.get() == before_tokens
    assert metrics.TURN_COST_USD_TOTAL.labels(model="negative-model", channel="webhook")._value.get() == before_cost
    assert metrics.ANOMALIES_TOTAL.labels(kind="usage_invalid")._value.get() == before_anomaly + 1
