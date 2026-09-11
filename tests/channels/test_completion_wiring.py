from __future__ import annotations

import pytest

from ach_agent.boot.completions import CompletionHandler, CompletionRegistry
from ach_agent.channels.webhook import handle_webhook_request
from ach_agent.config.schema import ChannelConfig
from ach_agent.router.router import RouterAdmitResult


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
    assert event.reply_future is None
    completion = registry.lookup(registry.ref_for(event))
    assert completion.state == "queued"

    await registry.finish(completion.ref, {"text": "done"})
    assert (await registry.wait(completion.ref)).result == {"text": "done"}
