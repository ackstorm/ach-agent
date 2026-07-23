# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from ach_agent.engine.base.driver import TurnResult
from ach_agent.engine.pi.driver import PiDriver
from ach_agent.engine.pi.protocol import (
    EV_AGENT_END,
    EV_AGENT_SETTLED,
    EV_ASSISTANT_INNER,
    EV_INNER_TEXT_DELTA,
    EV_MESSAGE_UPDATE,
    EV_SESSION_CREATED,
    EV_TOOL_START,
    F_SESSION_PATH,
)


class _ScriptedClient:
    """Replay queued Pi events and record commands sent by the driver."""

    def __init__(self, events: list[dict[str, Any]]) -> None:
        self._events = list(events)
        self.sent: list[dict[str, Any]] = []

    async def send(self, cmd: dict[str, Any]) -> None:
        self.sent.append(cmd)

    async def recv(self) -> dict[str, Any]:
        return self._events.pop(0) if self._events else {"type": "__eof__"}

    async def close(self) -> None:
        return None


class _Server:
    def __init__(self, client: _ScriptedClient) -> None:
        self._client = client

    def is_alive(self) -> bool:
        return True


def _text(value: str) -> dict[str, Any]:
    return {
        "type": EV_MESSAGE_UPDATE,
        EV_ASSISTANT_INNER: {"type": EV_INNER_TEXT_DELTA, "text": value},
    }


async def test_new_session_then_prompt_accumulates_text() -> None:
    client = _ScriptedClient(
        [
            {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/abc.json"},
            _text("hel"),
            _text("lo"),
            {"type": EV_AGENT_SETTLED},
        ]
    )
    sessions: dict[str, str] = {}
    stats: dict[str, Any] = {}
    result = await PiDriver().run_turn(
        _Server(client),
        conv_key="k",
        prompt="p",
        reuse=True,
        sessions=sessions,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats=stats,
    )
    assert result == TurnResult(text="hello", session_ref="/s/abc.json", aborted=False)
    assert sessions["k"] == "/s/abc.json"
    assert client.sent[0]["type"] == "new_session"
    assert client.sent[1] == {"type": "prompt", "message": "p"}


async def test_session_ref_switches_and_bypasses_map() -> None:
    client = _ScriptedClient([_text("wrapped"), {"type": EV_AGENT_SETTLED}])
    sessions: dict[str, str] = {}
    result = await PiDriver().run_turn(
        _Server(client),
        conv_key="k",
        prompt="wrap",
        reuse=True,
        sessions=sessions,
        session_ref="/s/fixed.json",
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert result.session_ref == "/s/fixed.json" and result.text == "wrapped"
    assert sessions == {}
    assert client.sent[0] == {"type": "switch_session", "sessionPath": "/s/fixed.json"}


async def test_max_tool_calls_aborts_and_flags() -> None:
    client = _ScriptedClient(
        [
            {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/a.json"},
            {"type": EV_TOOL_START, "toolName": "t", "callId": "c1"},
            {"type": EV_TOOL_START, "toolName": "t", "callId": "c2"},
            {"type": EV_AGENT_SETTLED},
        ]
    )
    result = await PiDriver().run_turn(
        _Server(client),
        conv_key="k",
        prompt="p",
        reuse=True,
        sessions={},
        on_text=None,
        on_tool=None,
        max_tool_calls=2,
        stats={},
    )
    assert result.aborted is True
    assert {"type": "abort"} in client.sent


async def test_cancel_sends_abort() -> None:
    class _Hanging(_ScriptedClient):
        async def recv(self) -> dict[str, Any]:
            await asyncio.sleep(3600)
            return {}

    client = _Hanging([{"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/a.json"}])
    task = asyncio.ensure_future(
        PiDriver().run_turn(
            _Server(client),
            conv_key="k",
            prompt="p",
            reuse=False,
            sessions={},
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {"type": "abort"} in client.sent


async def test_usage_is_stored_in_stats() -> None:
    client = _ScriptedClient(
        [
            {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/usage.json"},
            {
                "type": "message_end",
                "message": {
                    "id": "msg-usage",
                    "usage": {
                        "input": 12,
                        "output": 8,
                        "cacheRead": 4,
                        "cacheWrite": 1,
                        "cost": {"total": 0.42},
                    },
                },
            },
            _text("done"),
            {"type": EV_AGENT_SETTLED},
        ]
    )
    stats: dict[str, Any] = {}
    await PiDriver().run_turn(
        _Server(client),
        conv_key="usage",
        prompt="p",
        reuse=True,
        sessions={},
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats=stats,
    )
    mapped = stats["usage"]
    assert mapped.input_tokens == 12
    assert mapped.output_tokens == 8
    assert mapped.cache_read == 4
    assert mapped.cache_write == 1
    assert mapped.cost == 0.42


async def test_agent_end_will_retry_is_not_terminal() -> None:
    client = _ScriptedClient(
        [
            {"type": EV_SESSION_CREATED, F_SESSION_PATH: "/s/retry.json"},
            _text("before"),
            {"type": EV_AGENT_END, "willRetry": True},
            _text("after"),
            {"type": EV_AGENT_END, "willRetry": False},
        ]
    )
    result = await PiDriver().run_turn(
        _Server(client),
        conv_key="retry",
        prompt="p",
        reuse=True,
        sessions={},
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert result.text == "beforeafter"
