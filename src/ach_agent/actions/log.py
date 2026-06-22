# SPDX-License-Identifier: Apache-2.0
"""LogDeliveryAdapter — log-only reply delivery (D-03, D-04).

The Phase 1 delivery adapter. Phase 2 adds GitlabCommentAdapter behind
the same DeliveryAdapter Protocol seam without modifying this file.

ACT-01: reply actions are delivered.
ACT-04: only accepted, validated actions reach this adapter.
RTR-06: NEVER import from hermes_agent.* or engine.* or router.* here.
"""

from __future__ import annotations

from typing import Any

import structlog

log = structlog.get_logger(__name__)


class LogDeliveryAdapter:
    """Delivers reply actions to structured log output.

    Phase 1 implementation of the DeliveryAdapter Protocol (D-03).
    Phase 2+ swaps in GitlabCommentAdapter / other adapters via the same seam.
    """

    async def deliver(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        """Log the reply action to structured output.

        ACT-01: reply action delivered.
        ACT-04: only called with validated, accepted actions.
        """
        log.info(
            "delivery: reply action",
            action_name=action.get("name"),
            action_kind=action.get("kind"),
            input=action.get("input"),
        )
        # context intentionally unused (log adapter has no delivery target)
