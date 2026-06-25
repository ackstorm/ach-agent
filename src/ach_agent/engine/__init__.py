# SPDX-License-Identifier: Apache-2.0
"""Public engine surface for ach-agent.

All downstream plans (00-01b, 00-02, 00-03) implement against these contracts.
Bodies raise NotImplementedError until each plan fills them in.

Constraint: NEVER import from engine.client or engine.events at module top level —
those modules are created by 00-01b. Do NOT import the router or any Hermes type
anywhere in engine/ (D-08, RTR-06).
"""

from collections.abc import Callable

from ach_agent.engine.lifecycle import (
    EngineConfig,
    ManagedServer,
    run_invocation,
)
from ach_agent.engine.pool import EnginePool
from ach_agent.engine.sanitized_env import SanitizedEnv

# OnKillCallback: injected at invocation time.
# Phase 0: FakeSlotManager.on_kill (records call).
# Phase 1: real router triple-release (global + channel + lane).
OnKillCallback = Callable[[], None]

__all__ = [
    "EngineConfig",
    "ManagedServer",
    "EnginePool",
    "OnKillCallback",
    "SanitizedEnv",
    "run_invocation",
]
