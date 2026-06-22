"""Mock OpenAI-compatible model server for ach-agent round-trip testing.

Implements the OpenAI Responses API (POST /v1/responses), which is what
opencode v1.16.0 uses (NOT the Chat Completions API /v1/chat/completions).

The assembled response text is exactly:
  {"actions":[{"name":"channel_message","kind":"reply","input":{"text":"Mock reply from ach-agent!"}}]}

This is DEV TOOLING ONLY. It is intentionally minimal and never handles real credentials.

Endpoints:
  POST /v1/responses  — OpenAI Responses API streaming SSE
  GET  /v1/models     — minimal model list for opencode startup enumeration
  GET  /health        — health check
"""
from __future__ import annotations

import json
import logging

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s mock-model %(message)s")
log = logging.getLogger("mock-model")

app = FastAPI(title="mock-model")

# The canned assistant message content returned for the main model calls.
# opencode's SSE consumer will accumulate message.part.updated text deltas;
# engine/validator.py will extract the {"actions":[...]} JSON from the accumulated text.
CANNED_CONTENT = json.dumps(
    {
        "actions": [
            {
                "name": "channel_message",
                "kind": "reply",
                "input": {"text": "Mock reply from ach-agent!"},
            }
        ]
    }
)


async def _responses_stream(response_text: str):
    """Stream a properly-formatted OpenAI Responses API SSE response.

    opencode v1.16.0 uses POST /v1/responses (Responses API), not Chat Completions.
    The streaming event sequence must follow the Responses API protocol:
      response.created → response.in_progress → response.output_item.added
      → response.content_part.added → response.output_text.delta
      → response.output_text.done → response.content_part.done
      → response.output_item.done → response.done → [DONE]
    """
    resp_id = "resp_mock_001"
    item_id = "item_001"

    def _ev(data: dict) -> bytes:
        return ("data: " + json.dumps(data) + "\n\n").encode()

    # 1. response.created
    yield _ev({"type": "response.created", "response": {
        "id": resp_id, "object": "realtime.response",
        "status": "in_progress", "output": [],
    }})

    # 2. response.in_progress
    yield _ev({"type": "response.in_progress", "response": {
        "id": resp_id, "object": "realtime.response",
        "status": "in_progress", "output": [],
    }})

    # 3. response.output_item.added — establishes item_id
    yield _ev({"type": "response.output_item.added", "response_id": resp_id,
               "output_index": 0, "item": {
                   "id": item_id, "object": "realtime.item", "type": "message",
                   "status": "in_progress", "role": "assistant", "content": [],
               }})

    # 4. response.content_part.added
    yield _ev({"type": "response.content_part.added", "response_id": resp_id,
               "item_id": item_id, "output_index": 0, "content_index": 0,
               "part": {"type": "output_text", "text": ""}})

    # 5. response.output_text.delta — streaming text
    yield _ev({"type": "response.output_text.delta", "response_id": resp_id,
               "item_id": item_id, "output_index": 0, "content_index": 0,
               "delta": response_text})

    # 6. response.output_text.done
    yield _ev({"type": "response.output_text.done", "response_id": resp_id,
               "item_id": item_id, "output_index": 0, "content_index": 0,
               "text": response_text})

    # 7. response.content_part.done
    yield _ev({"type": "response.content_part.done", "response_id": resp_id,
               "item_id": item_id, "output_index": 0, "content_index": 0,
               "part": {"type": "output_text", "text": response_text}})

    # 8. response.output_item.done
    yield _ev({"type": "response.output_item.done", "response_id": resp_id,
               "output_index": 0, "item": {
                   "id": item_id, "object": "realtime.item", "type": "message",
                   "status": "completed", "role": "assistant",
                   "content": [{"type": "output_text", "text": response_text}],
               }})

    # 9. response.done
    yield _ev({"type": "response.done", "response": {
        "id": resp_id, "object": "realtime.response", "status": "completed",
        "output": [{"id": item_id, "object": "realtime.item", "type": "message",
                    "status": "completed", "role": "assistant",
                    "content": [{"type": "output_text", "text": response_text}]}],
    }})

    yield b"data: [DONE]\n\n"


@app.post("/v1/responses")
async def responses_api(request: Request):
    """OpenAI Responses API streaming endpoint.

    Used by opencode v1.16.0 for all model calls. Returns the canned
    {"actions":[...]} content for the main model; a short title for gpt-5-nano.
    """
    body = await request.json()
    model = body.get("model", "unknown")
    log.info("POST /v1/responses model=%s", model)

    # gpt-5-nano is used by opencode for session title generation — return minimal text
    is_title_gen = "gpt-5" in model
    response_text = "Hello title" if is_title_gen else CANNED_CONTENT

    return StreamingResponse(
        _responses_stream(response_text),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Legacy Chat Completions endpoint — fallback if opencode uses older API."""
    body = await request.json()
    model = body.get("model", "unknown")
    log.info("POST /v1/chat/completions model=%s (legacy endpoint)", model)

    response_text = CANNED_CONTENT

    async def _stream():
        chunk = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {"content": response_text}, "finish_reason": None}],
        }
        yield ("data: " + json.dumps(chunk) + "\n\n").encode()
        stop = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        }
        yield ("data: " + json.dumps(stop) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache"})


@app.get("/v1/models")
async def list_models():
    """Minimal models list for opencode startup model enumeration."""
    return JSONResponse({
        "object": "list",
        "data": [
            {"id": "gpt-4o-mini", "object": "model", "created": 1700000000, "owned_by": "mock"},
            {"id": "gpt-4o", "object": "model", "created": 1700000000, "owned_by": "mock"},
        ],
    })


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok"}
