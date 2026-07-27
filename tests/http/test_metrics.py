# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

from collections.abc import Iterable

import prometheus_client
import pytest
from prometheus_client.core import Metric
from prometheus_client.registry import CollectorRegistry, RestrictedRegistry

from ach_agent import identity
from ach_agent.http.metrics import IdentityRegistry


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


def _samples(registry: IdentityRegistry) -> dict[str, object]:
    return {
        sample.name: sample
        for metric in registry.collect()
        for sample in metric.samples
    }


def test_counter_total_name_matches_prometheus_client_025() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("thing_total", "doc", registry=registry).inc()
    identity.configure("classifier", "platform")
    samples = _samples(IdentityRegistry(registry))
    assert "thing_total" in samples
    assert "thing_total_total" not in samples


def test_prometheus_025_registry_types_are_not_conflated() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("typed_total", "doc", registry=registry).inc()
    library_restricted = registry.restricted_registry(["typed_total"])
    wrapped_metrics = list(IdentityRegistry(registry).collect())

    assert isinstance(library_restricted, RestrictedRegistry)
    assert all(isinstance(metric, Metric) for metric in wrapped_metrics)


class SharedMetricCollector:
    def __init__(self) -> None:
        self.metric = Metric("shared", "shared documentation", "gauge", unit="seconds")
        self.metric.add_sample(
            "shared_seconds",
            {"agent": "collector-agent", "source": "collector"},
            3.0,
            timestamp=123.0,
        )

    def collect(self) -> Iterable[Metric]:
        yield self.metric


def test_process_identity_wins_collision_and_preserves_every_metric_field() -> None:
    collector = SharedMetricCollector()
    identity.configure("process-agent", "platform")
    copied = next(iter(IdentityRegistry(collector).collect()))

    assert copied is not collector.metric
    assert copied.name == collector.metric.name
    assert copied.documentation == collector.metric.documentation
    assert copied.type == collector.metric.type
    assert copied.unit == "seconds"
    assert copied.samples[0] == collector.metric.samples[0]._replace(
        labels={"agent": "process-agent", "source": "collector", "environment": "platform"}
    )
    assert collector.metric.samples[0].labels == {
        "agent": "collector-agent",
        "source": "collector",
    }


def test_repeated_scrapes_do_not_mutate_or_share_returned_metrics() -> None:
    collector = SharedMetricCollector()
    wrapper = IdentityRegistry(collector)

    identity.configure("first-agent", "first-environment")
    first = next(iter(wrapper.collect()))
    identity.configure("second-agent", "second-environment")
    second = next(iter(wrapper.collect()))

    assert first is not second
    assert first.samples is not second.samples
    assert first.samples[0].labels["agent"] == "first-agent"
    assert second.samples[0].labels["agent"] == "second-agent"
    assert collector.metric.samples[0].labels["agent"] == "collector-agent"


def test_restricted_registry_filters_sample_names_and_preserves_unit() -> None:
    collector = SharedMetricCollector()
    identity.configure("classifier", "platform")
    restricted = IdentityRegistry(collector).restricted_registry(["shared_seconds"])
    metrics = list(restricted.collect())

    assert len(metrics) == 1
    assert metrics[0].unit == "seconds"
    assert [sample.name for sample in metrics[0].samples] == ["shared_seconds"]
    assert metrics[0].samples[0].labels["agent"] == "classifier"


def test_restricted_registry_drops_unrequested_metrics() -> None:
    registry = CollectorRegistry()
    prometheus_client.Counter("kept_total", "doc", registry=registry).inc()
    prometheus_client.Counter("dropped_total", "doc", registry=registry).inc()
    identity.configure("classifier", "platform")

    samples = _samples(IdentityRegistry(registry).restricted_registry(["kept_total"]))
    assert set(samples) == {"kept_total"}
