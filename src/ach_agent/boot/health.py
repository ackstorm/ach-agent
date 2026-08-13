# SPDX-License-Identifier: Apache-2.0
"""Process health flags shared by the HTTP surface and the SIGTERM drain handler.

`ready` gates GET /readyz (HTTP-02, Pitfall 6: not 200 until the lifespan sets it —
engine warmup is NOT part of the gate). `draining` is the D-12 pre-admission gate: a
straggler inbound during the drain gets a retriable 503, decoupled from engine readiness.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(slots=True)
class HealthState:
    ready: bool = False
    draining: bool = False
