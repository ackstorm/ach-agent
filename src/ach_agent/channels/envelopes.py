"""Serializable channel event envelopes and event correlation keys."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from ach_agent.channels.message_event import MessageEvent


def _finite_json(value: JsonValue) -> JsonValue:
    """Reject non-finite floats anywhere in a JSON value."""
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for item in value.values():
            _finite_json(item)
    elif isinstance(value, list):
        for item in value:
            _finite_json(item)
    return value


class EventRef(BaseModel):
    """Stable event identity; the channel is part of the deduplication namespace."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    agent: str
    channel_name: str
    idempotency_key: str


class EventEnvelope(BaseModel):
    """JSON-safe projection of ``MessageEvent`` with explicit free-form data."""

    model_config = ConfigDict(extra="forbid", strict=True)

    idempotency_key: str
    session_key: str
    channel_name: str
    secondary_idempotency_key: str | None = None
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    delivery_context: dict[str, JsonValue] = Field(default_factory=dict)
    source_trait: Literal["sync", "async_no_retry"]
    received_at: datetime
    task_id: str = ""
    free_form: bool = False

    _payload_finite = field_validator("payload", "delivery_context", "free_form")(_finite_json)

    @classmethod
    def from_message_event(cls, event: MessageEvent, *, free_form: bool = False) -> EventEnvelope:
        """Project a known ``MessageEvent`` and reject its local callback sink."""
        if getattr(event, "reply_future", None) is not None:
            raise ValueError("reply_future cannot be serialized in an EventEnvelope")
        return cls(
            idempotency_key=event.idempotency_key,
            session_key=event.session_key,
            channel_name=event.channel_name,
            secondary_idempotency_key=event.secondary_idempotency_key,
            payload=event.payload,
            delivery_context=event.delivery_context,
            source_trait=event.source_trait,
            received_at=event.received_at,
            task_id=event.task_id,
            free_form=free_form,
        )

    def event_ref(self, agent: str) -> EventRef:
        return EventRef(
            agent=agent,
            channel_name=self.channel_name,
            idempotency_key=self.idempotency_key,
        )
