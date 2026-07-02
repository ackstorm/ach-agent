from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, WebhookAuthBlock, WebhookBlock
from ach_agent.main import build_engine_prompt


def _webhook_channel(prompt: str | None) -> ChannelConfig:
    return ChannelConfig(
        name="gitlab-mr",
        type="webhook",
        source="gitlab",
        prompt=prompt,
        webhook=WebhookBlock(auth=WebhookAuthBlock(type="none")),
    )


def test_channel_prompt_template_is_rendered():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={"object_attributes": {"url": "https://gl/mr/7"}},
    )
    ch = _webhook_channel("Review this merge request: {{ payload.object_attributes.url }}")
    out = build_engine_prompt(event, channel_cfg=ch)
    assert out == "Review this merge request: https://gl/mr/7"


def test_channel_prompt_internal_namespace():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={},
    )
    ch = _webhook_channel("agent={{ internal.agent.name }} bank={{ internal.memory.bank }}")
    out = build_engine_prompt(event, channel_cfg=ch, agent_name="rev", memory_bank="b1")
    assert out == "agent=rev bank=b1"


def test_no_channel_prompt_falls_back_to_text_payload():
    # console / queue path: payload['text'] is returned verbatim (existing behavior)
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="console",
        payload={"text": "hello there"},
    )
    assert build_engine_prompt(event) == "hello there"


def test_unset_channel_prompt_uses_fallback():
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="gitlab-mr",
        payload={"scheduled_tick": "tick-9"},
    )
    ch = _webhook_channel(None)
    # prompt is None on the channel -> fall through to existing cron/scheduled logic
    assert build_engine_prompt(event, channel_cfg=ch) == "tick-9"
