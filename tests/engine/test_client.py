"""Tests for engine/client.py and engine/events.py.

Covers:
  - parse_opencode_event: typed-event mapping (text/user-echo/idle/error/retry)
  - test_send_message: POST /session/{id}/message body shape
  - test_find_free_port_collision_retry: 00-02 hardening stub

The live SSE consume + reconnect behavior is tested in test_lifecycle.py against
lifecycle.consume_sse_after_send (the retired consume_sse_to_completion is gone).
"""
from __future__ import annotations

import json
from unittest.mock import patch

from ach_agent.engine.events import (
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


def test_parse_message_part_delta_ignored():
    """message.part.delta is intentionally ignored — text comes from the cumulative
    message.part.updated snapshots, not the token-granular deltas."""
    data = {
        "type": "message.part.delta",
        "properties": {"messageID": "msg_1", "partID": "prt_1", "field": "text", "delta": "x"},
    }
    assert parse_opencode_event(data) is None


def test_parse_tool_running():
    """message.part.updated (part.type=tool, state=running) → OpenCodeToolUpdate."""
    from ach_agent.engine.events import OpenCodeToolUpdate, ToolStateRunning

    data = {
        "type": "message.part.updated",
        "properties": {
            "sessionID": "ses_1",
            "part": {
                "id": "prt_t1",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "mcp-google-calendar-ro_mcp-google-calendar-ro_auth_wait",
                "callID": "cj1__thought__ABC",
                "state": {"status": "running", "input": {"timeout_seconds": 120}},
            },
        },
    }
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeToolUpdate)
    assert isinstance(event.state, ToolStateRunning)
    assert event.part_id == "prt_t1"
    assert event.tool_name.endswith("auth_wait")
    assert event.state.input == {"timeout_seconds": 120}


def test_parse_tool_error():
    """message.part.updated (part.type=tool, state=error) → OpenCodeToolUpdate w/ error."""
    from ach_agent.engine.events import OpenCodeToolUpdate, ToolStateError

    data = {
        "type": "message.part.updated",
        "properties": {
            "part": {
                "id": "prt_t2",
                "messageID": "msg_1",
                "type": "tool",
                "tool": "calendar_list_events",
                "state": {"status": "error", "error": "boom"},
            },
        },
    }
    event = parse_opencode_event(data)
    assert isinstance(event, OpenCodeToolUpdate)
    assert isinstance(event.state, ToolStateError)
    assert event.state.error == "boom"


def test_parse_tool_pending_ignored():
    """A tool part still pending carries nothing renderable → None."""
    data = {
        "type": "message.part.updated",
        "properties": {"part": {"id": "p", "type": "tool", "state": {"status": "pending"}}},
    }
    assert parse_opencode_event(data) is None


def test_parse_server_connected():
    """server.connected → OpenCodeStreamReady (subscription-live signal, gates the send)."""
    from ach_agent.engine.events import OpenCodeStreamReady

    event = parse_opencode_event({"type": "server.connected", "properties": {}})
    assert isinstance(event, OpenCodeStreamReady)


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
# delete_session / compact_session
# ---------------------------------------------------------------------------


async def test_delete_session_issues_delete() -> None:
    """delete_session issues DELETE /session/{id} (verified live: opencode 1.17 → 200)."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    deleted: list[str] = []

    async def handle_delete(request: web.Request) -> web.Response:
        deleted.append(request.match_info["sid"])
        return web.Response(status=200, text="true")

    app = web.Application()
    app.router.add_delete("/session/{sid}", handle_delete)

    async with TestClient(TestServer(app)) as tc:
        from ach_agent.engine.client import OpenCodeClient

        client = OpenCodeClient(str(tc.make_url("")))
        async with client:
            await client.delete_session("ses_del1")

    assert deleted == ["ses_del1"]


async def test_compact_session_issues_post() -> None:
    """compact_session issues POST /session/{id}/compact with a JSON body."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    compacted: list[str] = []

    async def handle_compact(request: web.Request) -> web.Response:
        compacted.append(request.match_info["sid"])
        return web.Response(status=200, text="{}")

    app = web.Application()
    app.router.add_post("/session/{sid}/compact", handle_compact)

    async with TestClient(TestServer(app)) as tc:
        from ach_agent.engine.client import OpenCodeClient

        client = OpenCodeClient(str(tc.make_url("")))
        async with client:
            await client.compact_session("ses_cmp1")

    assert compacted == ["ses_cmp1"]


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
        with patch("ach_agent.engine.opencode.client.socket.socket", FakeSocket):
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
