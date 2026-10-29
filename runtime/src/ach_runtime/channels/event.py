# SPDX-License-Identifier: MIT
"""Normalized Event / RoutedEvent — the canonical types crossing channels -> harness (contract §2).

Event carries only what normalization produces: stable eventId, channel, normalized type,
raw payload, channel-specific context (e.g. `{"gitlab": {...}}`), actor and source trait.
RoutedEvent adds the workspace/session identity computed by `channels.routing.route()`.
Both are frozen — route() never mutates an Event, and the workspace/session keys it derives
are opaque strings, never parsed back apart by downstream consumers (contract §2/§11).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class Event:
    event_id: str
    channel: str
    type: str
    source: str
    payload: dict[str, Any] = field(default_factory=dict)
    context: dict[str, Any] = field(default_factory=dict)
    actor: str = ""


@dataclass(frozen=True, slots=True)
class RoutedEvent:
    event: Event
    workspace_key: str
    session_key: str

    @property
    def event_id(self) -> str:
        return self.event.event_id

    @property
    def channel(self) -> str:
        return self.event.channel
