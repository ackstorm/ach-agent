# SPDX-License-Identifier: MIT
"""Deterministic OpenAI-compatible model for the compose CI job. Always answers "pong" and logs
`echo turns=<n>`, the number of user messages in the request."""

from __future__ import annotations

import json

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

REPLY = "pong"


def _chunk(delta: dict, finish: str | None, usage: dict | None = None) -> bytes:
    body = {"id": "echo-1", "object": "chat.completion.chunk", "created": 0, "model": "local-model",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
    if usage:
        body["usage"] = usage
    return f"data: {json.dumps(body)}\n\n".encode()


async def chat(request: Request) -> Response:
    body = await request.json()
    turns = sum(1 for m in body.get("messages", []) if m.get("role") == "user")
    print(f"echo turns={turns}", flush=True)
    usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
    if not body.get("stream"):
        return JSONResponse({
            "id": "echo-1", "object": "chat.completion", "created": 0, "model": "local-model",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": REPLY}, "finish_reason": "stop"}],
            "usage": usage,
        })

    async def events():
        yield _chunk({"role": "assistant", "content": REPLY}, None)
        yield _chunk({}, "stop", usage)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


app = Starlette(routes=[
    Route("/v1/chat/completions", chat, methods=["POST"]),
    Route("/healthz", healthz, methods=["GET"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9100)
