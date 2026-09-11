"""CONTRACT §6.9: egress is the agent's (via external MCP), NOT the channel's.

Invariant: the harness has NO channel-side posting path. The old v2 delivery layer
(`ach_agent.actions.*`) is gone, and the engine_runner never posts on the model's
behalf — for an async event with no registry admission it does nothing (egress
already happened via the agent's MCP tool calls). The registry stores outcomes
for admitted work and never posts to a channel.
"""

from __future__ import annotations

import importlib
from typing import Any

import pytest

from ach_agent.boot.completions import CompletionRegistry
from ach_agent.channels.envelopes import EventRef
from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.router import RouterAdmitResult


def test_no_harness_side_delivery_module() -> None:
    """§6.9: the v2 harness-side delivery layer must not exist (removed in Plan 1)."""
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ach_agent.actions.gitlab_comment")
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("ach_agent.actions")


class _FakeServer:
    proxy_token = "tok"


class _FakePool:
    """Minimal EnginePool stand-in: acquire returns a server, release is a no-op."""

    sessions: dict[str, str] = {}

    async def acquire(self, _session_key: str, _cfg: Any) -> _FakeServer:
        return _FakeServer()

    async def release(self, _session_key: str, ttl_seconds: float = 0.0) -> None:
        return None


async def test_engine_runner_does_not_post(monkeypatch: Any) -> None:
    """§6.9: async event without registry admission has no harness-side delivery.

    The engine_runner runs the invocation and, finding no registry admission, returns
    without posting anywhere. An admitted event gets a retained terminal outcome.
    """
    import ach_agent.engine.base.terminal as terminal
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver

    async def _fake_run_contract_turn(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        # a2a_reply terminal so the registry positive control resolves.
        return {"action": "a2a_reply", "text": "the agent already acted via MCP"}

    # make_engine_runner imports run_contract_turn from base.terminal at call time —
    # patch the source.
    monkeypatch.setattr(terminal, "run_contract_turn", _fake_run_contract_turn)

    async def _accepted(_event: MessageEvent) -> RouterAdmitResult:
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(_accepted)
    runner = make_engine_runner(
        pool=_FakePool(),
        driver=OpencodeDriver(),
        engine_cfg=EngineConfig(),
        max_invocation_seconds=30,
        memory_cfg=None,
        completion_registry=registry,
    )

    def _on_kill() -> None:
        return None

    # Async webhook event is not admitted to the registry.
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

    # Positive control: an admitted event gets a retained terminal outcome.
    seam_event = MessageEvent(
        idempotency_key="k-seam",
        session_key="ctx-1",
        channel_name="a2a-peer",
        payload={},
        source_trait="async_no_retry",
    )
    ref = EventRef(agent="default", channel_name="a2a-peer", idempotency_key="k-seam")
    await registry.submit(seam_event)
    await runner(seam_event, _on_kill)

    completion = await registry.wait(ref)
    assert completion.state == "completed"
    assert completion.result == {"action": "a2a_reply", "text": "the agent already acted via MCP"}
