# SPDX-License-Identifier: Apache-2.0
"""MessageHandler Protocol — the named in-process seam (RTR-06).

Only this module is imported by channel adapters. The router implements
this Protocol. Engine never imports this module (D-08, RTR-06).

Constraint: NEVER import from hermes_agent.* or engine.* here (RTR-06).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.router import RouterAdmitResult


class MessageHandler(Protocol):
    """The named in-process seam between channels and the router.

    Channel adapters call handler.handle(event); the router is the impl.
    Keeping this a Protocol means mypy enforces the seam from both sides
    without creating an import cycle (RTR-06).
    """

    async def handle(self, event: MessageEvent) -> RouterAdmitResult: ...
