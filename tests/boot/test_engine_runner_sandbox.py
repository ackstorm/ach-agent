# SPDX-License-Identifier: Apache-2.0
"""Runner over sandbox leases: the leased client runs the invocation with the rewritten config."""

from __future__ import annotations

import contextlib
from types import SimpleNamespace
from typing import Any

import pytest

from ach_agent.boot.engine_runner import make_engine_runner
from ach_agent.channels.message_event import MessageEvent
from ach_agent.execution.wire import PublicEngineConfig
from tests.boot.test_engine_runner_http import _FakeClient


class _Sessions:
    def __init__(self, client: _FakeClient) -> None:
        self.box = SimpleNamespace(client=client)
        self.leases: list[bool] = []
        self.configured: list[PublicEngineConfig] = []

    @contextlib.asynccontextmanager
    async def lease(self, event: MessageEvent, *, persistent: bool):
        self.leases.append(persistent)
        yield self.box

    def engine_config(self, box: Any, cfg: PublicEngineConfig) -> PublicEngineConfig:
        self.configured.append(cfg)
        return cfg.model_copy(update={"model_base_url": "http://gw/s/t/9/v1"})


@pytest.fixture(autouse=True)
def _contract(monkeypatch: Any) -> None:
    import ach_agent.engine.base.terminal as terminal

    async def fake(run_turn: Any, **kwargs: Any) -> dict[str, str]:
        result = await run_turn(
            prompt=kwargs["prompt"],
            max_tool_calls=kwargs["max_tool_calls"],
            on_text=kwargs["on_text"],
            on_tool=kwargs["on_tool"],
            stats=kwargs["stats"],
        )
        return {"action": "none", "text": result.text}

    monkeypatch.setattr(terminal, "run_contract_turn", fake)


def _event() -> MessageEvent:
    return MessageEvent(
        idempotency_key="e", session_key="s1", channel_name="chat", payload={"message": "hi"}
    )


async def test_leased_client_is_used_with_rewritten_config() -> None:
    client = _FakeClient()
    sessions = _Sessions(client)
    runner = make_engine_runner(
        client=None,
        engine_cfg=PublicEngineConfig(),
        max_invocation_seconds=30,
        sandboxes=sessions,  # type: ignore[arg-type]
    )
    result = await runner(_event(), lambda: None)
    assert result is not None and result["action"] == "none"
    assert [n for n, _ in client.calls] == ["acquire", "turn", "release"]
    assert client.calls[0][1].config.model_base_url == "http://gw/s/t/9/v1"
    assert sessions.leases == [True]  # no channel session block => reuse
    assert len(sessions.configured) == 1


def test_exactly_one_of_client_and_sandboxes() -> None:
    with pytest.raises(ValueError):
        make_engine_runner(client=None, engine_cfg=PublicEngineConfig(), max_invocation_seconds=1)
    with pytest.raises(ValueError):
        make_engine_runner(
            client=_FakeClient(),  # type: ignore[arg-type]
            engine_cfg=PublicEngineConfig(),
            max_invocation_seconds=1,
            sandboxes=_Sessions(_FakeClient()),  # type: ignore[arg-type]
        )
