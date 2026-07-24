# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from ach_agent.engine.base.driver import EngineConfig, TurnResult
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
from ach_agent.engine.pi.rpc import PiRpcError


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


def _response(
    request_id: str,
    command: str,
    *,
    success: bool = True,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {
        "type": "response",
        "id": request_id,
        "command": command,
        "success": success,
    }
    if data is not None:
        response["data"] = data
    return response


class _LaunchProcess:
    returncode = None
    stdin = None
    stdout = None
    stderr = None

    async def wait(self) -> int:
        return 0


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
    client = _ScriptedClient(
        [
            _response("ach-pi-1", "switch_session"),
            _text("wrapped"),
            {"type": EV_AGENT_SETTLED},
        ]
    )
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
    assert client.sent[0] == {
        "type": "switch_session",
        "sessionPath": "/s/fixed.json",
        "id": "ach-pi-1",
    }


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


async def test_new_session_response_is_correlated_before_prompt() -> None:
    client = _ScriptedClient(
        [
            _response("stale-id", "new_session", data={"sessionPath": "/s/stale.json"}),
            _response("ach-pi-1", "new_session", data={"sessionPath": "/s/good.json"}),
            _text("good"),
            {"type": EV_AGENT_SETTLED},
        ]
    )
    sessions: dict[str, str] = {}
    result = await PiDriver().run_turn(
        _Server(client),
        conv_key="correlated",
        prompt="p",
        reuse=True,
        sessions=sessions,
        on_text=None,
        on_tool=None,
        max_tool_calls=0,
        stats={},
    )
    assert result.session_ref == "/s/good.json"
    assert sessions == {"correlated": "/s/good.json"}
    assert client.sent[1] == {"type": "prompt", "message": "p"}


@pytest.mark.parametrize(
    ("command", "response"),
    [
        ("new_session", _response("ach-pi-1", "new_session", success=False)),
        ("new_session", _response("ach-pi-1", "new_session", data={"cancelled": True})),
        ("switch_session", _response("ach-pi-1", "switch_session", success=False)),
        ("switch_session", _response("ach-pi-1", "switch_session", data={"cancelled": True})),
    ],
)
async def test_failed_or_cancelled_session_response_never_prompts(
    command: str, response: dict[str, Any]
) -> None:
    sessions = {"k": "/s/existing.json"} if command == "switch_session" else {}
    client = _ScriptedClient([response])
    with pytest.raises(PiRpcError):
        await PiDriver().run_turn(
            _Server(client),
            conv_key="k",
            prompt="must not run",
            reuse=command == "switch_session",
            sessions=sessions,
            session_ref="/s/existing.json" if command == "switch_session" else None,
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    assert not any(item.get("type") == "prompt" for item in client.sent)


async def test_stale_switch_response_never_authorizes_prompt() -> None:
    client = _ScriptedClient(
        [
            _response("stale-id", "switch_session"),
            _response("ach-pi-1", "switch_session", success=False),
        ]
    )
    with pytest.raises(PiRpcError):
        await PiDriver().run_turn(
            _Server(client),
            conv_key="stale-switch",
            prompt="must not run",
            reuse=True,
            sessions={"stale-switch": "/s/existing.json"},
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
    assert not any(item.get("type") == "prompt" for item in client.sent)


@pytest.mark.parametrize(
    ("compose", "prompt", "flag"),
    [
        ("replace", "persona", "--system-prompt"),
        ("append", "persona", "--append-system-prompt"),
        ("replace", "", None),
        ("append", "", None),
    ],
)
async def test_launch_persona_and_exclude_tools_argv(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    compose: str,
    prompt: str,
    flag: str | None,
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _LaunchProcess:
        captured["args"] = args
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pi_module, "PiRpcClient", lambda _proc: object())
    cfg = EngineConfig(
        binary_path="pi",
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
        compose=compose,
        system_prompt=prompt,
        exclude_tools=["bash", "read"],
    )
    await PiDriver().launch(cfg, "argv")
    args = list(captured["args"])
    if flag is None:
        assert "--system-prompt" not in args
        assert "--append-system-prompt" not in args
    else:
        assert args[args.index(flag) + 1] == prompt
        other_flag = "--append-system-prompt" if flag == "--system-prompt" else "--system-prompt"
        assert other_flag not in args
    assert args[args.index("--exclude-tools") + 1] == "bash,read"


async def test_launch_adds_thinking_flag_when_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pi_module, "PiRpcClient", lambda _proc: object())
    cfg = EngineConfig(
        binary_path="pi",
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
        thinking_effort="high",
    )
    await PiDriver().launch(cfg, "argv")
    args = list(captured["args"])
    assert args[args.index("--thinking") + 1] == "high"


async def test_launch_omits_thinking_flag_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(pi_module, "PiRpcClient", lambda _proc: object())
    cfg = EngineConfig(
        binary_path="pi", home=str(tmp_path / "home"), work_dir=str(tmp_path / "work")
    )
    await PiDriver().launch(cfg, "argv")
    assert "--thinking" not in list(captured["args"])


async def test_run_tui_uses_native_mode_not_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import ach_agent.engine.pi.driver as pi_module

    captured: dict[str, Any] = {}

    async def fake_exec(*args: Any, **kwargs: Any) -> _LaunchProcess:
        captured["args"] = args
        captured["kwargs"] = kwargs
        return _LaunchProcess()

    monkeypatch.setattr(pi_module.shutil, "which", lambda _binary: "/usr/bin/pi")
    monkeypatch.setattr(pi_module.asyncio, "create_subprocess_exec", fake_exec)
    cfg = EngineConfig(
        binary_path="pi",
        home=str(tmp_path / "home"),
        work_dir=str(tmp_path / "work"),
        model="ackstorm.smart",
        thinking_effort="low",
    )

    await PiDriver().run_tui(cfg, "tui-console")

    args = list(captured["args"])
    assert args[0] == "/usr/bin/pi"
    assert "--mode" not in args
    assert args[args.index("--provider") + 1] == "ach-openai"
    assert args[args.index("--model") + 1] == "ackstorm.smart"
    assert args[args.index("--thinking") + 1] == "low"
    assert "stdin" not in captured["kwargs"]
    assert "stdout" not in captured["kwargs"]
