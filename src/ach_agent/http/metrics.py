# SPDX-License-Identifier: Apache-2.0
"""Prometheus exposition wrapper that stamps immutable process identity."""

from __future__ import annotations

from collections.abc import Iterable
from copy import copy

from prometheus_client import REGISTRY
from prometheus_client.core import Metric
from prometheus_client.registry import Collector

from ach_agent import identity


class IdentityRegistry:
    """Expose copied, identity-stamped snapshots from an underlying collector."""

    def __init__(
        self,
        registry: Collector = REGISTRY,
        names: frozenset[str] | None = None,
    ) -> None:
        self._registry = registry
        self._names = names

    def collect(self) -> Iterable[Metric]:
        stamp = identity.current()
        process_labels = {"agent": stamp.agent, "environment": stamp.environment}
        for metric in self._registry.collect():
            samples = [
                sample._replace(labels={**sample.labels, **process_labels})
                for sample in metric.samples
                if self._names is None or sample.name in self._names
            ]
            if not samples:
                continue
            copied = copy(metric)
            copied.samples = samples
            yield copied

    def restricted_registry(self, names: Iterable[str]) -> IdentityRegistry:
        requested = frozenset(names)
        if self._names is not None:
            requested &= self._names
        return IdentityRegistry(self._registry, requested)
