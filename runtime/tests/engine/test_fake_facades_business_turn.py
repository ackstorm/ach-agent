from __future__ import annotations

import json

import httpx
import pytest

from tests.engine.fake_facades import fake_facades  # match existing engine-test import form
from tests.e2e.test_pr_review import _business_input_occurrences as review_occurrences
from tests.e2e.test_recovery import _business_input_occurrences as recovery_occurrences


pytestmark = pytest.mark.asyncio

PROMPT = {"role": "user", "parts": [{"text": (
    "Review the current state of this merge request.\nProject: team/other\nMR: 5\nTrigger: merge_request\n"
    'Payload: {"project": {"id": 2}, "object_attributes": {"iid": 5}}\nRepository: ./handoff/repo\n'
)}]}
UPDATE = {"role": "user", "parts": [{"text": "<system-update>rename-current-session</system-update>"}]}
NOTE_TOOL = "mcp-gitlab-ro_create_merge_request_note"
CALL = {"role": "model", "parts": [{"functionCall": {"name": NOTE_TOOL, "args": {}}}]}
RESULT = {"role": "user", "parts": [{"functionResponse": {"name": NOTE_TOOL, "response": {"output": "ok"}}}]}
TOOLS = [{"functionDeclarations": [{"name": NOTE_TOOL}]}]


async def test_business_input_occurrences_skip_native_system_update():
    prompt = {"role": "user", "parts": [{"text": "Review the current state of this merge request.\nProject: team/service\n"}]}
    update = {"role": "user", "parts": [{"text": "<system-update>rename-current-session</system-update>"}]}
    call = {"role": "model", "parts": [{"functionCall": {"name": "execute", "args": {}}}]}
    result = {"role": "user", "parts": [{"functionResponse": {"name": "execute", "response": {}}}]}
    text = {"role": "model", "parts": [{"text": "pong"}]}

    def record(*contents):
        return {"url": "/v1/models/fixture:streamGenerateContent?alt=sse", "body": {"contents": list(contents)}}

    # Role shapes of the followup METADATA records 0-5: native appends a system-update user
    # content after the first tool result, and Gemini keeps prior-invocation history.
    first = (prompt, call, result, update, call, result, text)
    records = [
        record(prompt),
        record(prompt, call, result, update),
        record(prompt, call, result, update, call, result),
        record(*first, prompt),
        record(*first, prompt, call, result, update),
        record(*first, prompt, call, result, update, call, result),
    ]
    assert review_occurrences(records) == [prompt, prompt]
    assert recovery_occurrences(records) == [prompt, prompt]


async def _reply_parts(model_url, contents, **extra):
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"{model_url}/models/fixture:streamGenerateContent?alt=sse",
            json={"contents": contents, "tools": TOOLS, **extra},
        )
    return json.loads(response.text.removeprefix("data: ").strip())["candidates"][0]["content"]["parts"]


async def test_payload_comes_from_review_prompt_not_system_update():
    async with fake_facades("loop") as facades:
        parts = await _reply_parts(facades.model_url, [PROMPT, CALL, RESULT, UPDATE])
    args = parts[0]["functionCall"]["args"]
    assert args["project_id"] == 2 and args["merge_request_iid"] == 5


async def test_native_none_tool_mode_gets_text_reply():
    async with fake_facades("loop") as facades:
        allowed = await _reply_parts(facades.model_url, [PROMPT])
        disabled = await _reply_parts(
            facades.model_url, [PROMPT], toolConfig={"functionCallingConfig": {"mode": "NONE"}},
        )
    assert allowed[0]["functionCall"]["name"] == NOTE_TOOL
    assert disabled == [{"text": "pong"}]
