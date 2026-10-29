# SPDX-License-Identifier: MIT
"""Channel normalization and routing (contract §2)."""

from ach_runtime.channels.auth import verify_webhook_auth
from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.channels.gitlab import MalformedGitlabEventError, normalize_gitlab
from ach_runtime.channels.routing import RoutingError, route

__all__ = [
    "Event",
    "RoutedEvent",
    "MalformedGitlabEventError",
    "normalize_gitlab",
    "RoutingError",
    "route",
    "verify_webhook_auth",
]
