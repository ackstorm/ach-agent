from __future__ import annotations

import asyncio

import pytest

from ach_agent.boot.completions import CompletionHandler, CompletionRegistry
from ach_agent.channels.a2a import A2AAgentExecutorBridge
from ach_agent.channels.message_event import MessageEvent
from ach_agent.channels.webhook import handle_webhook_request
from ach_agent.config.schema import ChannelConfig
from ach_agent.router.dedup import InMemoryDedupStore
from ach_agent.router.router import Router, RouterAdmitResult


class _Queue:
    def __init__(self) -> None:
        self.events = []

    async def enqueue_event(self, event):
        self.events.append(event)


class _Context:
    task_id = "task-1"
    context_id = "conversation-1"

    def __init__(self, secret: str) -> None:
        self.call_context = type("CallContext", (), {"state": {"headers": {"x-key": secret}}})()

    def get_user_input(self) -> str:
        return "hello"


@pytest.mark.asyncio
async def test_webhook_admission_and_result_use_event_ref_without_callback_payload() -> None:
    seen = []

    async def admit(event):
        seen.append(event)
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit)
    handler = CompletionHandler(registry)
    cfg = ChannelConfig.model_validate(
        {
            "name": "hooks",
            "type": "webhook",
            "source": "generic",
            "webhook": {"auth": {"type": "none"}},
        }
    )

    response = await handle_webhook_request(
        b'{"text":"hello"}',
        {"Idempotency-Key": "evt-1"},
        cfg,
        handler,
    )

    assert response.status_code == 202
    assert len(seen) == 1
    event = seen[0]
    assert not hasattr(event, "reply_future")
    completion = registry.lookup(registry.ref_for(event))
    assert completion.state == "queued"

    await registry.finish(completion.ref, {"text": "done"})
    assert (await registry.wait(completion.ref)).result == {"text": "done"}


@pytest.mark.asyncio
async def test_a2a_bridge_fans_out_registry_completion_and_validates_terminal_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("A2A_TEST_SECRET", "secret")
    cfg = ChannelConfig.model_validate(
        {
            "name": "peer",
            "type": "a2a",
            "a2a": {"auth": {"header": "x-key", "secret": {"env": "A2A_TEST_SECRET"}}},
        }
    )
    registry: CompletionRegistry

    async def admit(event):
        async def complete():
            await asyncio.sleep(0)
            await registry.finish(registry.ref_for(event), {"action": "a2a_reply", "text": "done"})

        asyncio.create_task(complete())
        return RouterAdmitResult.ACCEPTED

    registry = CompletionRegistry(admit)
    handler = CompletionHandler(registry)
    bridge = A2AAgentExecutorBridge(handler, cfg, registry)
    first, retry = _Queue(), _Queue()
    await asyncio.gather(
        bridge.execute(_Context("secret"), first),
        bridge.execute(_Context("secret"), retry),
    )

    assert [event.status.state for event in first.events] == [2, 3]
    assert [event.status.state for event in retry.events] == [2, 3]
    assert first.events[-1].status.message.parts[0].text == "done"


@pytest.mark.asyncio
async def test_router_runner_failure_finishes_registry_waiter() -> None:
    async def runner(_event, _on_kill):
        raise RuntimeError("boom")

    router_ref = {}

    async def admit(event):
        return await router_ref["router"].handle(event)

    registry = CompletionRegistry(admit)
    router = Router(
        max_concurrent_invocations=1,
        max_queued_total=2,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=runner,
        max_invocation_seconds=1,
        completion_notifier=registry.finish_event,
    )
    router_ref["router"] = router
    event = MessageEvent(idempotency_key="failure", session_key="lane", channel_name="cron")
    submission = await registry.submit(event)
    assert submission.completion is not None
    completion = await registry.wait(submission.completion.ref)
    assert completion.state == "failed"
    assert completion.error == "invocation failed"
