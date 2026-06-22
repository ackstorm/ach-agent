"""Validator tests: ENG-05 action extraction and schema validation.

Tests owned by this plan (00-01a): stubs marked @pytest.mark.skip(reason="00-03").
Stubs are collected clean now; 00-03 un-skips as it implements.

Per-Task Verification Map (00-VALIDATION.md):
  ENG-05: test_extract_actions    — implemented by 00-03
  ENG-05: test_repair_turn        — implemented by 00-03
"""
from __future__ import annotations


# ---------------------------------------------------------------------------
# ENG-05: {"actions":[...]} extracted from accumulated text
# ---------------------------------------------------------------------------


def test_extract_actions() -> None:
    """ENG-05: extract_actions() finds {"actions":[...]} in accumulated SSE text.

    Covers:
    - Plain JSON embedded in text
    - JSON wrapped in markdown ```json ... ``` fences
    - Multiple JSON blobs: last one wins (rfind strategy)
    - None returned when no {"actions" marker found
    """
    from ach_agent.engine.validator import extract_actions

    # Plain JSON
    plain = '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"hi"}}]}'
    result = extract_actions(plain)
    assert result is not None
    assert len(result) == 1
    assert result[0]["name"] == "channel_message"

    # Text with preamble before JSON
    with_preamble = (
        "Here is the response:\n"
        '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"hello"}}]}'
    )
    result = extract_actions(with_preamble)
    assert result is not None
    assert result[0]["input"]["text"] == "hello"

    # JSON wrapped in markdown fences
    fenced = (
        "```json\n"
        '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"fenced"}}]}\n'
        "```"
    )
    result = extract_actions(fenced)
    assert result is not None
    assert result[0]["input"]["text"] == "fenced"

    # Multiple JSON-like blobs: last {"actions" wins (rfind)
    multi = (
        '{"actions":[{"name":"old","kind":"reply","input":{}}]} some text '
        '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"last"}}]}'
    )
    result = extract_actions(multi)
    assert result is not None
    assert result[0]["name"] == "channel_message"
    assert result[0]["input"]["text"] == "last"

    # No {"actions" marker — returns None
    result = extract_actions("No actions here, just text.")
    assert result is None

    # Incomplete brace — returns None
    result = extract_actions('{"actions":[{"name":"x"')
    assert result is None


# ---------------------------------------------------------------------------
# ENG-05: Repair turn issued on schema validation failure (max 2)
# ---------------------------------------------------------------------------


async def test_repair_turn() -> None:
    """ENG-05: repair_turn() re-prompts on schema validation failure (max 2 attempts).

    Verifies that on invalid actions, a repair prompt is sent and the result
    is re-validated. After max_attempts exhausted, returns None or the best result.
    Uses a fake send_fn so no real opencode binary is needed.
    """
    from ach_agent.engine.validator import repair_turn

    # responseActions schema: action "channel_message" requires "text" in input
    response_actions_schema = [
        {
            "name": "channel_message",
            "kind": "reply",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        }
    ]

    # Scenario 1: first turn invalid (missing "text"), repair turn returns valid
    call_count = 0
    valid_text = '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"hi"}}]}'

    async def send_fn_repairs_on_first_try(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        # The repair prompt is sent — return valid actions
        return valid_text

    # Accumulated text with invalid action (missing required "text" field)
    invalid_text = '{"actions":[{"name":"channel_message","kind":"reply","input":{}}]}'

    result = await repair_turn(
        send_fn=send_fn_repairs_on_first_try,
        accumulated_text=invalid_text,
        response_actions_schema=response_actions_schema,
        max_attempts=2,
    )
    assert result is not None, "repair_turn must return actions after successful repair"
    assert call_count == 1, f"Expected exactly 1 repair call, got {call_count}"
    assert result[0]["input"]["text"] == "hi"

    # Scenario 2: valid actions on first extraction — zero repair turns
    call_count = 0

    async def send_fn_should_not_be_called(prompt: str) -> str:
        nonlocal call_count
        call_count += 1
        return valid_text

    # If we have valid text already, repair_turn should still be callable but
    # the caller in lifecycle.py would not call it. Test that valid input
    # returns immediately with 0 repair calls when wrapped.
    result = await repair_turn(
        send_fn=send_fn_should_not_be_called,
        accumulated_text=valid_text,
        response_actions_schema=response_actions_schema,
        max_attempts=2,
    )
    assert result is not None
    assert call_count == 0, f"Expected 0 repair calls for valid input, got {call_count}"

    # Scenario 3: both attempts fail — returns None after max_attempts
    repair_call_count = 0
    still_invalid = '{"actions":[{"name":"channel_message","kind":"reply","input":{}}]}'

    async def send_fn_always_invalid(prompt: str) -> str:
        nonlocal repair_call_count
        repair_call_count += 1
        return still_invalid

    result = await repair_turn(
        send_fn=send_fn_always_invalid,
        accumulated_text=still_invalid,
        response_actions_schema=response_actions_schema,
        max_attempts=2,
    )
    assert result is None, "repair_turn must return None after max_attempts with invalid output"
    # max_attempts=2 total: original extraction is attempt 0, repair is attempt 1
    # After the repair also fails, we stop. So send_fn called once (the repair call).
    assert repair_call_count == 1, (
        f"Expected exactly 1 repair call before giving up (max_attempts=2), got {repair_call_count}"
    )


# ---------------------------------------------------------------------------
# run_invocation structured result (wired into lifecycle.py)
# ---------------------------------------------------------------------------


async def test_run_invocation_returns_structured_actions() -> None:
    """Verify run_invocation wires extract_actions and returns InvocationResult.

    Uses a fake ManagedServer and mocked consume_sse_after_send so no real
    opencode binary is needed. Asserts the returned InvocationResult has
    {"actions":[...]} and NOT {"raw_text": ...}.
    """
    from unittest.mock import AsyncMock, MagicMock
    from unittest.mock import patch

    from ach_agent.engine.lifecycle import run_invocation, EngineConfig, ManagedServer
    from ach_agent.engine.client import OpenCodeClient

    # Build a minimal fake server
    fake_server = ManagedServer(port=9999)
    fake_client = MagicMock(spec=OpenCodeClient)
    fake_server._client = fake_client
    fake_process = MagicMock()
    fake_process.returncode = None  # still alive
    fake_server._process = fake_process

    # The SSE consumer returns accumulated text with {"actions":[...]}
    canned_text = (
        '{"actions":[{"name":"channel_message","kind":"reply","input":{"text":"hello"}}]}'
    )

    response_actions_schema = [
        {
            "name": "channel_message",
            "kind": "reply",
            "inputSchema": {
                "type": "object",
                "required": ["text"],
                "properties": {"text": {"type": "string"}},
            },
        }
    ]

    with patch(
        "ach_agent.engine.lifecycle.consume_sse_after_send",
        new_callable=AsyncMock,
        return_value=canned_text,
    ):
        result = await run_invocation(
            server=fake_server,
            session_id="ses_test",
            prompt="hello",
            response_actions_schema=response_actions_schema,
            max_invocation_seconds=30,
            on_kill=lambda: None,
        )

    # Must return InvocationResult with "actions" key, not raw_text
    assert "actions" in result, f"InvocationResult must have 'actions' key, got: {result}"
    assert "raw_text" not in result, (
        f"InvocationResult must NOT have 'raw_text' key — extraction not wired: {result}"
    )
    assert isinstance(result["actions"], list)
    assert len(result["actions"]) > 0
    assert result["actions"][0]["name"] == "channel_message"
