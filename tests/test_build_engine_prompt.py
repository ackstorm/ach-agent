from types import SimpleNamespace

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, WebhookAuthBlock, WebhookBlock
from ach_agent.main import (
    A2A_OUTPUT_INSTRUCTIONS,
    NONE_OUTPUT_INSTRUCTIONS,
    build_engine_prompt,
    build_output_instructions,
    terminal_action_for,
)


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


# ---------------------------------------------------------------------------
# build_output_instructions — harness-owned terminal-contract directive
# (only .type is read, so a SimpleNamespace stub suffices — no full ChannelConfig)
# ---------------------------------------------------------------------------


def test_output_instructions_a2a_gets_a2a_reply_block():
    assert build_output_instructions(SimpleNamespace(type="a2a"), False) == A2A_OUTPUT_INSTRUCTIONS


def test_output_instructions_webhook_gets_none_block():
    got = build_output_instructions(SimpleNamespace(type="webhook"), False)
    assert got == NONE_OUTPUT_INSTRUCTIONS


def test_output_instructions_cron_gets_none_block():
    got = build_output_instructions(SimpleNamespace(type="cron"), False)
    assert got == NONE_OUTPUT_INSTRUCTIONS


def test_output_instructions_queue_gets_none_block():
    got = build_output_instructions(SimpleNamespace(type="queue"), False)
    assert got == NONE_OUTPUT_INSTRUCTIONS


def test_output_instructions_free_form_is_empty():
    # --tui console: no terminal contract, even for an a2a-typed cfg
    assert build_output_instructions(SimpleNamespace(type="a2a"), True) == ""


def test_output_instructions_none_cfg_defaults_to_none_block():
    assert build_output_instructions(None, False) == NONE_OUTPUT_INSTRUCTIONS


def test_a2a_block_names_only_a2a_reply():
    assert '"action":"a2a_reply"' in A2A_OUTPUT_INSTRUCTIONS
    assert '"action":"none"' not in A2A_OUTPUT_INSTRUCTIONS


def test_none_block_names_only_none():
    assert '"action":"none"' in NONE_OUTPUT_INSTRUCTIONS
    assert "a2a_reply" not in NONE_OUTPUT_INSTRUCTIONS


def test_terminal_action_for_a2a():
    assert terminal_action_for(SimpleNamespace(type="a2a"), False) == "a2a_reply"


def test_terminal_action_for_other_classes_is_none():
    assert terminal_action_for(SimpleNamespace(type="webhook"), False) == "none"
    assert terminal_action_for(SimpleNamespace(type="cron"), False) == "none"
    assert terminal_action_for(SimpleNamespace(type="queue"), False) == "none"
    assert terminal_action_for(None, False) == "none"


def test_terminal_action_for_free_form_is_none():
    # tui skips extraction; the action is unused but must not be a2a_reply
    assert terminal_action_for(SimpleNamespace(type="a2a"), True) == "none"


def test_output_instructions_appended_last_and_message_preserved():
    # Documents the handler's assembly (main.py dispatch): message first, block last.
    event = MessageEvent(
        idempotency_key="evt-1",
        session_key="ses-1",
        channel_name="peer-intake",
        payload={"text": "do the thing"},
    )
    base = build_engine_prompt(event, channel_cfg=None)  # a2a no-prompt → raw payload text
    block = build_output_instructions(SimpleNamespace(type="a2a"), False)
    full = f"{base}\n\n{block}"
    assert full.startswith("do the thing")
    assert full.endswith(block)
