"""Tests for engine/client.py and engine/events.py.

Covers:
  - test_consume_sse_to_idle: accumulated text deltas returned on session.idle
  - test_consume_sse_to_error: EngineError raised on session.error
  - test_send_message: POST /session/{id}/message body shape
  - test_find_free_port_collision_retry: 00-02 hardening stub
  - test_sse_reconnect: 00-02 hardening stub
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from ach_agent.engine.events import (
    EngineError,
    consume_sse_to_completion,
    parse_opencode_event,
    OpenCodeSessionError,
    OpenCodeSessionIdle,
    OpenCodeTextUpdate,
    OpenCodeUserMessage,
)


# ---------------------------------------------------------------------------
# Helpers to build fake SSE event dicts
# ---------------------------------------------------------------------------


def _text_part_event(session_id: str, message_id: str, text: str) -> dict:
    return {
        "type": "message.part.updated",
        "properties": {
            "sessionID": session_id,
            "part": {
                "type": "text",
                "id": "part_001",
                "messageID": message_id,
                "text": text,
            },
        },
    }


def _user_message_event(session_id: str, message_id: str) -> dict:
    return {
        "type": "message.updated",
        "properties": {
            "sessionID": session_id,
            "info": {
                "id": message_id,
                "role": "user",
                "tokens": {},
                "time": {},
                "cost": 0,
            },
        },
    }


def _session_idle_event(session_id: str) -> dict:
    return {
        "type": "session.idle",
        "properties": {"sessionID": session_id},
    }


def _session_error_event(session_id: str, error_type: str, message: str) -> dict:
    return {
        "type": "session.error",
        "properties": {
            "sessionID": session_id,
            "error": {"type": error_type, "message": message},
        },
    }


def _session_status_retry_event(session_id: str) -> dict:
    return {
        "type": "session.status",
        "properties": {
            "sessionID": session_id,
            "status": {"type": "retry"},
        },
    }


# ---------------------------------------------------------------------------
# parse_opencode_event unit tests
# ---------------------------------------------------------------------------


def test_parse_text_update():
    data = _text_part_event("ses_1", "msg_1", "hello world")
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeTextUpdate)
    assert event.text == "hello world"
    assert event.message_id == "msg_1"
    assert event.session_id == "ses_1"


def test_parse_user_message():
    data = _user_message_event("ses_1", "msg_u1")
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeUserMessage)
    assert event.message_id == "msg_u1"


def test_parse_session_idle():
    data = _session_idle_event("ses_1")
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeSessionIdle)
    assert event.session_id == "ses_1"


def test_parse_session_error():
    data = _session_error_event("ses_1", "auth_error", "Invalid API key")
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeSessionError)
    assert event.error_type == "auth_error"
    assert event.message == "Invalid API key"


def test_parse_unknown_event_returns_none():
    data = {"type": "some.unknown.event", "properties": {}}
    result = parse_opencode_event(data)
    assert result is None


def test_parse_session_status_retry_returns_none():
    """session.status with type=retry must NOT be treated as terminal — returns None."""
    data = _session_status_retry_event("ses_1")
    result = parse_opencode_event(data)
    assert result is None


# ---------------------------------------------------------------------------
# consume_sse_to_completion tests
# ---------------------------------------------------------------------------


async def _fake_iter_sse(events: list[dict]):
    """Async generator yielding parsed events from a list of event dicts."""
    from ach_agent.engine.events import parse_opencode_event as parse_ev

    for data in events:
        ev = parse_ev(data)
        if ev is not None:
            yield ev


async def test_consume_sse_to_idle():
    """Text deltas are accumulated and returned on session.idle."""
    events = [
        _user_message_event("ses_1", "msg_user"),
        _text_part_event("ses_1", "msg_asst", "Hello "),
        _text_part_event("ses_1", "msg_asst", "world"),
        _session_status_retry_event("ses_1"),  # transient — must NOT terminate
        _text_part_event("ses_1", "msg_asst", "!"),
        _session_idle_event("ses_1"),
    ]

    from ach_agent.engine.client import OpenCodeClient

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    def _fake_iter(client, resp):
        return _fake_iter_sse(events)

    with patch("ach_agent.engine.events._iter_sse_events_from_client", new=_fake_iter):
        result = await consume_sse_to_completion(mock_client, "ses_1")

    # User-echo text is filtered (msg_user), assistant text accumulated
    assert result == "Hello world!"


async def test_consume_sse_to_idle_filters_user_echo():
    """Text from user-echoed message IDs is NOT included in accumulation."""
    events = [
        _user_message_event("ses_1", "msg_user"),
        # These two text parts belong to the user echo — should be filtered
        _text_part_event("ses_1", "msg_user", "user prompt text"),
        _text_part_event("ses_1", "msg_user", " more user text"),
        # This one is the assistant response
        _text_part_event("ses_1", "msg_asst", '{"actions":[]}'),
        _session_idle_event("ses_1"),
    ]

    from ach_agent.engine.client import OpenCodeClient

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    def _fake_iter(client, resp):
        return _fake_iter_sse(events)

    with patch("ach_agent.engine.events._iter_sse_events_from_client", new=_fake_iter):
        result = await consume_sse_to_completion(mock_client, "ses_1")

    assert "user prompt text" not in result
    assert '{"actions":[]}' in result


async def test_consume_sse_to_error():
    """EngineError is raised when session.error is received."""
    events = [
        _text_part_event("ses_1", "msg_asst", "some partial text"),
        _session_error_event("ses_1", "auth_error", "Invalid API key"),
    ]

    from ach_agent.engine.client import OpenCodeClient

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    def _fake_iter(client, resp):
        return _fake_iter_sse(events)

    with patch("ach_agent.engine.events._iter_sse_events_from_client", new=_fake_iter):
        with pytest.raises(EngineError) as exc_info:
            await consume_sse_to_completion(mock_client, "ses_1")

    assert exc_info.value.error_type == "auth_error"
    assert "Invalid API key" in str(exc_info.value)


async def test_sse_consumer_ignores_session_status_retry():
    """SSE consumer does NOT terminate on session.status type=retry (transient)."""
    events = [
        _session_status_retry_event("ses_1"),
        _session_status_retry_event("ses_1"),
        _session_status_retry_event("ses_1"),
        _text_part_event("ses_1", "msg_asst", "final answer"),
        _session_idle_event("ses_1"),
    ]

    from ach_agent.engine.client import OpenCodeClient

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())

    def _fake_iter(client, resp):
        return _fake_iter_sse(events)

    with patch("ach_agent.engine.events._iter_sse_events_from_client", new=_fake_iter):
        result = await consume_sse_to_completion(mock_client, "ses_1")

    assert result == "final answer"


# ---------------------------------------------------------------------------
# test_send_message
# ---------------------------------------------------------------------------


async def test_send_message():
    """send_message issues POST /session/{id}/message with correct body."""
    import aiohttp
    from aiohttp.test_utils import TestServer, TestClient
    from aiohttp import web

    received: list[dict] = []

    async def handle_message(request: web.Request) -> web.Response:
        body = await request.json()
        received.append(body)
        return web.Response(status=200, text="{}")

    app = web.Application()
    app.router.add_post("/session/{sid}/message", handle_message)

    async with TestClient(TestServer(app)) as tc:
        base_url = str(tc.make_url(""))
        from ach_agent.engine.client import OpenCodeClient

        client = OpenCodeClient(base_url)
        async with client:
            await client.send_message("ses_123", "hello")

    assert len(received) == 1
    body = received[0]
    assert "parts" in body
    assert len(body["parts"]) == 1
    assert body["parts"][0]["type"] == "text"
    assert body["parts"][0]["text"] == "hello"


# ---------------------------------------------------------------------------
# find_free_port
# ---------------------------------------------------------------------------


def test_find_free_port_returns_valid_port():
    """find_free_port returns a port in the valid range."""
    from ach_agent.engine.client import find_free_port

    port = find_free_port()
    assert 1024 < port < 65536


def test_find_free_port_not_in_reserved():
    """find_free_port does not return a port that is already reserved."""
    from ach_agent.engine.client import find_free_port, _reserved_ports

    port = find_free_port()
    assert port in _reserved_ports
    # Subsequent calls should not return the same port
    port2 = find_free_port()
    assert port != port2 or port2 not in _reserved_ports


# ---------------------------------------------------------------------------
# 00-02 hardening tests (H-04 / H-02)
# ---------------------------------------------------------------------------


def test_find_free_port_collision_retry():
    """H-04: find_free_port retries up to 20 attempts on port collision.

    Pre-seed _reserved_ports with ports that will be returned by the socket
    binding, so the 20-attempt loop must exhaust those before returning a
    port NOT in the reserved set.
    """
    from ach_agent.engine.client import _reserved_ports, find_free_port, release_port

    # Find a real free port to use as a "reserved" one
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        colliding_port = s.getsockname()[1]

    # Pre-seed the reserved set with the colliding port
    _reserved_ports.add(colliding_port)

    # Monkeypatch socket binding to always return colliding_port first, then a different port
    real_socket_class = socket.socket
    call_count = 0

    class FakeSocket:
        def __init__(self, *args, **kwargs):
            self._real = real_socket_class(socket.AF_INET, socket.SOCK_STREAM)
            self._fake_port = None

        def bind(self, addr):
            nonlocal call_count
            call_count += 1
            if call_count <= 1:
                # First call returns the colliding port
                self._fake_port = colliding_port
            else:
                # Subsequent calls return a real free port
                self._real.bind(("127.0.0.1", 0))
                self._fake_port = self._real.getsockname()[1]

        def getsockname(self):
            return ("127.0.0.1", self._fake_port)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            try:
                self._real.close()
            except Exception:
                pass

        def close(self):
            try:
                self._real.close()
            except Exception:
                pass

    try:
        with patch("ach_agent.engine.client.socket.socket", FakeSocket):
            result_port = find_free_port()

        # Result port must NOT be in the pre-seeded collision set
        assert result_port != colliding_port, (
            f"find_free_port returned colliding port {colliding_port}"
        )
        # Verify the returned port was added to _reserved_ports
        assert result_port in _reserved_ports
    finally:
        # Clean up reserved ports used in this test
        _reserved_ports.discard(colliding_port)
        _reserved_ports.discard(result_port if "result_port" in dir() else 0)


async def test_sse_reconnect():
    """H-02: SSE consumer reconnects up to max_reconnects on ClientError if server healthy.

    Feed a fake aiohttp client whose SSE stream raises ClientError; stub check_health()
    True. Assert up to 3 reconnect attempts then re-raises on exhaustion.
    """
    import aiohttp
    from unittest.mock import AsyncMock, patch

    from ach_agent.engine.events import EngineError, consume_sse_to_completion
    from ach_agent.engine.client import OpenCodeClient

    attempt_count = 0

    async def fake_iter_sse_with_error(client, resp):
        nonlocal attempt_count
        attempt_count += 1
        # Raise ClientError to simulate SSE connection drop
        raise aiohttp.ClientError("Connection dropped")
        yield  # make it a generator (unreachable but required for async generator)

    mock_client = AsyncMock(spec=OpenCodeClient)
    mock_client.subscribe_events = AsyncMock(return_value=AsyncMock())
    # Server is "healthy" so reconnect should be attempted
    mock_client.check_health = AsyncMock(return_value=True)

    with patch("ach_agent.engine.events._iter_sse_events_from_client", new=fake_iter_sse_with_error):
        with pytest.raises((aiohttp.ClientError, EngineError)):
            await consume_sse_to_completion(mock_client, "ses_reconnect", max_reconnects=3)

    # Should have attempted max_reconnects+1 times (initial + 3 retries)
    assert attempt_count == 4, (
        f"Expected 4 SSE attempts (1 initial + 3 reconnects), got {attempt_count}"
    )
