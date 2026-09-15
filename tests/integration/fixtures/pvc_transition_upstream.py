"""Credential-free model fixture for the standalone-to-distributed acceptance."""

from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
counts = {"hydrate": 0, "model": 0, "tool_turn": 0, "prior_history_seen": 0}
message_sizes: list[int] = []
marker_results: list[str] = []


def _auth(request: Request) -> bool:
    return request.headers.get("x-ach-key") == "pvc-transition-key"


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"counts": counts, "message_sizes": message_sizes, "marker_results": marker_results}


@app.post("/platform/hydrate")
async def hydrate(request: Request) -> JSONResponse:
    if not _auth(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    counts["hydrate"] += 1
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "environment": "pvc-transition",
            "runtime": {"models": [{"id": "test-model", "endpoint": f"{base}/v1"}]},
        }
    )


def _has_tool_result(body: dict[str, Any]) -> tuple[bool, str]:
    messages = body.get("messages", [])
    if not isinstance(messages, list):
        messages = body.get("input", [])
    if not isinstance(messages, list):
        messages = []
    last_user = max(
        (
            i
            for i, item in enumerate(messages)
            if isinstance(item, dict) and item.get("role") == "user"
        ),
        default=-1,
    )
    last_tool = max(
        (
            i
            for i, item in enumerate(messages)
            if isinstance(item, dict) and item.get("role") == "tool"
        ),
        default=-1,
    )
    if last_tool > last_user:
        for item in reversed(messages):
            if not isinstance(item, dict) or item.get("role") != "tool":
                continue
            result = json.dumps(item)
            for marker in ("standalone-marker", "distributed-marker"):
                if marker in result:
                    return True, marker
            return True, "unknown"
    return False, ""


def _chat_response(*, tool: bool, marker: str) -> StreamingResponse:
    if tool:
        message: dict[str, Any] = {
            "role": "assistant",
            "tool_calls": [
                {
                    "id": "pvc-read-marker",
                    "type": "function",
                    "function": {"name": "bash", "arguments": '{"command":"cat active-marker"}'},
                }
            ],
        }
    else:
        message = {
            "role": "assistant",
            "content": json.dumps({"action": "none", "text": f"marker={marker}"}),
        }

    async def stream() -> Any:
        yield (
            "data: "
            + json.dumps(
                {
                    "id": "pvc-transition",
                    "object": "chat.completion.chunk",
                    "choices": [
                        {
                            "index": 0,
                            "delta": message,
                            "finish_reason": "tool_calls" if tool else "stop",
                        }
                    ],
                }
            )
            + "\n\n"
        ).encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> StreamingResponse | JSONResponse:
    if not _auth(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    body = await request.json()
    counts["model"] += 1
    messages = body.get("messages", [])
    message_sizes.append(len(messages) if isinstance(messages, list) else 0)
    has_result, marker = _has_tool_result(body)
    if "standalone-marker" in json.dumps(messages):
        counts["prior_history_seen"] += 1
    if has_result:
        counts["tool_turn"] += 1
        if marker:
            marker_results.append(marker)
    return _chat_response(tool=not has_result, marker=marker or "unknown")


@app.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    if not _auth(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return JSONResponse({"object": "list", "data": [{"id": "test-model", "object": "model"}]})
