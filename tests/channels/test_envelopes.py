from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from ach_agent.channels.envelopes import EventEnvelope, EventRef
from ach_agent.channels.message_event import MessageEvent


def make_event(**changes: object) -> MessageEvent:
    values: dict[str, object] = {
        "idempotency_key": "evt-1",
        "session_key": "session-1",
        "channel_name": "webhook",
        "payload": {"text": "hello", "count": 2},
        "delivery_context": {"project_id": "p1"},
        "source_trait": "sync",
        "received_at": datetime(2026, 1, 1, tzinfo=UTC),
        "task_id": "task-1",
    }
    values.update(changes)
    return MessageEvent(**values)  # type: ignore[arg-type]


def test_event_envelope_round_trips_as_strict_json_projection() -> None:
    envelope = EventEnvelope.from_message_event(make_event(), free_form=True)

    restored = EventEnvelope.model_validate_json(envelope.model_dump_json())

    assert restored == envelope
    assert "reply_future" not in envelope.model_dump()
    assert restored.free_form is True


def test_event_envelope_rejects_callbacks_unknown_fields_and_nonfinite_values() -> None:
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(
            {**EventEnvelope.from_message_event(make_event()).model_dump(), "extra": 1}
        )

    with pytest.raises(ValidationError):
        EventEnvelope.from_message_event(make_event(payload={"bad": float("nan")}))


def test_event_ref_uses_channel_namespace_for_same_raw_id() -> None:
    left = EventRef(agent="a", channel_name="webhook", idempotency_key="same")
    right = EventRef(agent="a", channel_name="a2a", idempotency_key="same")

    assert left != right
