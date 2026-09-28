# SPDX-License-Identifier: Apache-2.0
"""Task 4: channel.handoff cadence and hooks.sessionStart on new sessions.

Uses the same _FakeClient double as test_engine_runner_http.py — reservation state
(new_session) is derived from real directory existence, so the assertions below hold
for the shared-workspace path exercised by every current placement.
"""

from __future__ import annotations

from typing import Any

import pytest

from ach_agent.boot.engine_runner import make_engine_runner
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig
from ach_agent.execution.wire import HookSpec, PublicEngineConfig
from tests.boot.test_engine_runner_http import _FakeClient


async def _fake_contract(run_turn: Any, **kwargs: Any) -> dict[str, str]:
    result = await run_turn(
        prompt=kwargs["prompt"],
        max_tool_calls=kwargs["max_tool_calls"],
        on_text=kwargs["on_text"],
        on_tool=kwargs["on_tool"],
        stats=kwargs["stats"],
    )
    return {"action": "none", "text": result.text}


def _handoff_channel(scope: str) -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": "chat",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "handoff": {"script": "true", "scope": scope},
        }
    )


@pytest.mark.asyncio
async def test_event_scope_handoff_reruns_but_session_start_only_on_new_session(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.engine.base.terminal as terminal

    monkeypatch.setattr(terminal, "run_contract_turn", _fake_contract)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(work_dir=str(tmp_path / "workspace")),
        max_invocation_seconds=30,
        channels_by_name={"chat": _handoff_channel("event")},
        handoff_staging_root=tmp_path / "staging",
    )

    async def run(event_id: str) -> None:
        await runner(
            MessageEvent(
                idempotency_key=event_id,
                session_key="session-1",
                channel_name="chat",
                payload={},
            ),
            lambda: None,
        )

    await run("event-1")
    first_names = [name for name, _ in client.calls]
    assert "import_handoff" in first_names
    assert "start_session" in first_names  # new session

    client.calls.clear()
    await run("event-2")
    second_names = [name for name, _ in client.calls]
    assert "import_handoff" in second_names  # scope=event reruns every invocation
    assert "start_session" not in second_names  # not a new session


@pytest.mark.asyncio
async def test_session_scope_handoff_and_session_start_run_once_only(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.engine.base.terminal as terminal

    monkeypatch.setattr(terminal, "run_contract_turn", _fake_contract)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(work_dir=str(tmp_path / "workspace")),
        max_invocation_seconds=30,
        channels_by_name={"chat": _handoff_channel("session")},
        handoff_staging_root=tmp_path / "staging",
    )

    async def run(event_id: str) -> None:
        await runner(
            MessageEvent(
                idempotency_key=event_id,
                session_key="session-2",
                channel_name="chat",
                payload={},
            ),
            lambda: None,
        )

    await run("event-1")
    first_names = [name for name, _ in client.calls]
    assert "import_handoff" in first_names
    assert "start_session" in first_names

    client.calls.clear()
    await run("event-2")
    second_names = [name for name, _ in client.calls]
    assert "import_handoff" not in second_names  # scope=session, not new
    assert "start_session" not in second_names


@pytest.mark.asyncio
async def test_session_start_hook_without_handoff_runs_only_on_new_session(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.engine.base.terminal as terminal

    monkeypatch.setattr(terminal, "run_contract_turn", _fake_contract)
    client = _FakeClient()
    channel = ChannelConfig.model_validate(
        {"name": "chat", "type": "cron", "cron": {"schedule": "* * * * *"}}
    )
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(
            work_dir=str(tmp_path / "workspace"),
            hook_session_start=HookSpec(script="true"),
        ),
        max_invocation_seconds=30,
        channels_by_name={"chat": channel},
        handoff_staging_root=tmp_path / "staging",
    )

    async def run(event_id: str) -> None:
        await runner(
            MessageEvent(
                idempotency_key=event_id,
                session_key="session-3",
                channel_name="chat",
                payload={},
            ),
            lambda: None,
        )

    await run("event-1")
    first_names = [name for name, _ in client.calls]
    assert "prepare" in first_names
    assert "import_handoff" not in first_names  # no handoff configured
    assert "start_session" in first_names

    client.calls.clear()
    await run("event-2")
    second_names = [name for name, _ in client.calls]
    assert "start_session" not in second_names


@pytest.mark.asyncio
async def test_no_handoff_and_no_hooks_skips_workspace_prepare(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.engine.base.terminal as terminal

    monkeypatch.setattr(terminal, "run_contract_turn", _fake_contract)
    client = _FakeClient()
    channel = ChannelConfig.model_validate(
        {"name": "chat", "type": "cron", "cron": {"schedule": "* * * * *"}}
    )
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(work_dir=str(tmp_path / "workspace")),
        max_invocation_seconds=30,
        channels_by_name={"chat": channel},
    )

    await runner(
        MessageEvent(
            idempotency_key="event-1",
            session_key="session-4",
            channel_name="chat",
            payload={},
        ),
        lambda: None,
    )

    names = [name for name, _ in client.calls]
    assert "prepare" not in names
    assert names == ["acquire", "turn", "session_op", "release"]
    acquire_request = client.calls[0][1]
    assert acquire_request.config.work_dir == str(tmp_path / "workspace")
