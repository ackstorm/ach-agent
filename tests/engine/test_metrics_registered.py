# SPDX-License-Identifier: Apache-2.0
"""Engine counters must register at boot, not lazily on first increment.

If DRAIN_COMPLETED / ENGINE_LAUNCH_FAILURES are imported inside functions, they
only register with the prometheus REGISTRY when that code path first runs — so
they are absent from /metrics until the first drain / launch failure. main.py
imports DRAIN_COMPLETED at module scope, and imports make_engine_runner (which
in turn imports ENGINE_LAUNCH_FAILURES at boot.engine_runner's module scope) at
module scope too — this guards that transitive chain (isolation-proof: asserts
the module-level binding, not global-registry state that other tests could
pollute).
"""
from __future__ import annotations

import prometheus_client

import ach_agent.boot.engine_runner as engine_runner
import ach_agent.main as main


def test_engine_counters_imported_eagerly_in_main() -> None:
    for module, name in (
        (engine_runner, "ENGINE_LAUNCH_FAILURES"),
        (main, "DRAIN_COMPLETED"),
    ):
        counter = getattr(module, name, None)
        assert isinstance(counter, prometheus_client.Counter), (
            f"{module.__name__}.{name} must be an eagerly-imported Counter so it registers "
            f"at boot and appears in /metrics as 0 before any increment (got {counter!r})"
        )
