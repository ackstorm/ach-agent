# SPDX-License-Identifier: Apache-2.0
"""Engine counters must register at boot, not lazily on first increment.

If DRAIN_COMPLETED / ENGINE_LAUNCH_FAILURES are imported inside functions, they
only register with the prometheus REGISTRY when that code path first runs — so
they are absent from /metrics until the first drain / launch failure. main.py
imports them at module scope; this guards that (isolation-proof: asserts the
module-level binding, not global-registry state that other tests could pollute).
"""
from __future__ import annotations

import prometheus_client

import ach_agent.main as main


def test_engine_counters_imported_eagerly_in_main() -> None:
    for name in ("ENGINE_LAUNCH_FAILURES", "DRAIN_COMPLETED"):
        counter = getattr(main, name, None)
        assert isinstance(counter, prometheus_client.Counter), (
            f"main.{name} must be an eagerly-imported Counter so it registers at "
            f"boot and appears in /metrics as 0 before any increment (got {counter!r})"
        )
