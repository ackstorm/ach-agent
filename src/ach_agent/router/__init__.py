# SPDX-License-Identifier: Apache-2.0
"""Public router surface — Router and RouterAdmitResult only cross this boundary.

Constraint: No Hermes imports. No engine imports. Seam is one-directional
(channels→router→engine via on_kill). RTR-06, D-08.

NEVER import from hermes_agent.* or engine.* in router/channels/actions.
Engine never imports the router. RTR-06, D-08.
"""

from ach_agent.router.router import Router, RouterAdmitResult

__all__ = ["Router", "RouterAdmitResult"]
