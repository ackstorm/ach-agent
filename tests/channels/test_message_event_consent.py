"""MessageEvent.user_consented tests (D-03 — throwaway v1 consent marker).

Verifies that the user_consented field on MessageEvent:
  - defaults to False when not specified (safe default for all channel adapters in v1)
  - can be explicitly set to True (for test fixtures that exercise the consent-pass path)
"""

from __future__ import annotations


def test_user_consented_defaults_false() -> None:
    """D-03: MessageEvent built without user_consented has user_consented is False."""
    from ach_agent.channels.message_event import MessageEvent

    event = MessageEvent(
        idempotency_key="key-001",
        session_key="session-001",
        channel_name="webhook",
    )
    assert event.user_consented is False, (
        f"Expected user_consented=False (default), got {event.user_consented!r}"
    )


def test_user_consented_set_true() -> None:
    """D-03: MessageEvent constructed with user_consented=True sets it True.

    This is the test-fixture path — channel adapters never set this in v1.
    """
    from ach_agent.channels.message_event import MessageEvent

    event = MessageEvent(
        idempotency_key="key-002",
        session_key="session-002",
        channel_name="slack",
        user_consented=True,
    )
    assert event.user_consented is True, (
        f"Expected user_consented=True, got {event.user_consented!r}"
    )
