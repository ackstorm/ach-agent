# SPDX-License-Identifier: Apache-2.0
"""DeliveryAdapter Protocol — named delivery seam (D-03).

First implementation: LogDeliveryAdapter (actions/log.py).
Phase 2 implementation: GitlabCommentAdapter (no refactor of this seam).

Constraint: NEVER import from hermes_agent.* or engine.* or router.* here (RTR-06).
ACT-01: reply actions are delivered.
ACT-04: only accepted, validated actions are executed.
"""

from __future__ import annotations

from typing import Any, Protocol


class DeliveryAdapter(Protocol):
    """Deliver a validated reply action to the appropriate sink.

    ACT-01: reply actions are delivered.
    ACT-04: only accepted, validated actions are executed.
    """

    async def deliver(self, action: dict[str, Any], context: dict[str, Any]) -> None: ...
