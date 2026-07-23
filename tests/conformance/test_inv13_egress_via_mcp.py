"""CONTRACT §6.9: egress is the agent's (via external MCP), NOT the channel's.

Invariant: the harness has NO channel-side posting path. The old v2 delivery layer
(`ach_agent.actions.*`) is gone, the Router carries `delivery_adapter=None`, and the
engine_runner never posts on the model's behalf — for an async event with no reply
seam it does nothing (egress already happened via the agent's MCP tool calls). The
ONLY delivery seam is the injected `on_complete`/reply_future callback.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from ach_agent.channels.message_event import MessageEvent


def test_no_harness_side_delivery_module() -> None:
    """§6.9: the v2 harness-side delivery layer must not exist (removed in Plan 1)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ach_agent.actions.gitlab_comment")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ach_agent.actions")


class _FakeServer:
    pass


class _FakePool:
    """Minimal EnginePool stand-in: acquire returns a server, release is a no-op."""

    sessions: dict[str, str] = {}

    async def acquire(self, _session_key: str, _cfg: Any) -> _FakeServer:
        return _FakeServer()

    async def release(self, _session_key: str, ttl_seconds: float = 0.0) -> None:
        return None


async def test_engine_runner_does_not_post(monkeypatch: Any) -> None:
    """§6.9: async event (no reply_future, no on_complete) → no harness-side delivery.

    The engine_runner runs the invocation and, finding no reply seam, returns without
    posting anywhere. A positive control proves the ONLY delivery path is the injected
    on_complete callback — there is no hardcoded poster.
    """
    import ach_agent.engine.base.terminal as terminal
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver
    from ach_agent.main import _make_engine_runner

    async def _fake_run_contract_turn(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # a2a_reply terminal so the positive-control seam fires (engine_runner routes a
        # valid a2a_reply to on_complete; a none-action async event simply falls through).
        return {"action": "a2a_reply", "text": "the agent already acted via MCP"}

    # _make_engine_runner imports run_contract_turn from base.terminal at call time —
    # patch the source.
    monkeypatch.setattr(terminal, "run_contract_turn", _fake_run_contract_turn)

    runner = _make_engine_runner(
        pool=_FakePool(),
        driver=OpencodeDriver(),
        engine_cfg=EngineConfig(),
        max_invocation_seconds=30,
        memory_cfg=None,
    )

    def _on_kill() -> None:
        return None

    # Async webhook event: no reply_future, no on_complete in delivery_context.
    async_event = MessageEvent(
        idempotency_key="k-async",
        session_key="42:7",
        channel_name="gitlab-mr-review",
        payload={"object_attributes": {"title": "X"}},
        delivery_context={"project_id": 42, "mr_iid": 7},
        source_trait="sync",
    )
    # Must complete with no exception and no posting (there is nothing to post to).
    await runner(async_event, _on_kill)

    # Positive control: the ONLY delivery seam is the injected on_complete callback.
    delivered: list[tuple[str, str]] = []

    def _on_complete(session_key: str, text: str) -> None:
        delivered.append((session_key, text))

    seam_event = MessageEvent(
        idempotency_key="k-seam",
        session_key="ctx-1",
        channel_name="a2a-peer",
        payload={},
        delivery_context={"on_complete": _on_complete},
        source_trait="async_no_retry",
    )
    await runner(seam_event, _on_kill)

    assert delivered == [("ctx-1", "the agent already acted via MCP")], (
        "§6.9: delivery happens ONLY through the injected on_complete seam, not a harness poster"
    )
