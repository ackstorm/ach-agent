"""Small authenticated ACH/model fixture used by the real Compose acceptance.

This server is deliberately test infrastructure.  It records only bounded counters
and message sizes so the acceptance can prove that H performed hydration and injected
the synthetic key at the model boundary.  No production code imports this module.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
from collections import Counter
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

EXPECTED_KEY = "split-acceptance-key"
REPLY = "PHASE1_SPLIT_REPLY"

app = FastAPI()
counts: Counter[str] = Counter()
message_sizes: list[int] = []
prompts: list[str] = []
cancel_started = 0


def _authorized(request: Request) -> bool:
    if request.headers.get("x-ach-key") == EXPECTED_KEY:
        counts["authorized"] += 1
        return True
    counts["unauthorized"] += 1
    return False


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "counts": dict(counts),
        "message_sizes": list(message_sizes),
        "prompts": list(prompts[-8:]),
        "cancel_started": cancel_started,
    }


@app.post("/platform/hydrate")
async def hydrate(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    counts["hydrate"] += 1
    base = str(request.base_url).rstrip("/")
    return JSONResponse(
        {
            "environment": "split-acceptance",
            "runtime": {"models": [{"id": "test-model", "endpoint": f"{base}/v1"}]},
            "context": {
                "skills": [
                    {
                        "name": "split-fixture-skill",
                        "id": "split-fixture-skill",
                        "downloadUrl": f"{base}/content/skill/split-fixture-skill",
                    }
                ]
            },
        }
    )


@app.get("/content/skill/split-fixture-skill")
async def skill_fixture(request: Request) -> Response:
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:gz") as archive:
        content = (
            b"# Split fixture skill\n\n"
            b"Use this installed skill to verify startup hydration.\n"
        )
        info = tarfile.TarInfo("split-fixture-skill/SKILL.md")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    return Response(payload.getvalue(), media_type="application/gzip")


@app.get("/v1/models")
async def models(request: Request) -> JSONResponse:
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return JSONResponse({"object": "list", "data": [{"id": "test-model", "object": "model"}]})


@app.post("/v1/chat/completions", response_model=None)
async def chat(request: Request) -> Response:
    if not _authorized(request):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    counts["model"] += 1
    body = await request.json()
    messages = body.get("messages", [])
    message_sizes.append(len(messages) if isinstance(messages, list) else 0)
    if isinstance(messages, list):
        for item in messages:
            if isinstance(item, dict) and item.get("role") == "user":
                content = item.get("content", "")
                prompts.append(str(content)[:512])
    # A cancellation probe asks the fixture to hold the response.  The acceptance
    # script kills E during this window and verifies H records a terminal failure.
    if any("CANCEL_ME" in str(item) for item in messages):
        global cancel_started
        cancel_started += 1
        await asyncio.sleep(60)

    async def stream() -> Any:
        for delta, reason in (
            ({"role": "assistant", "content": '{"action":"none","text":"'}, None),
            ({"content": REPLY + '"}'}, None),
            ({}, "stop"),
        ):
            item = {
                "id": "split-acceptance-chat",
                "object": "chat.completion.chunk",
                "model": body.get("model", "test-model"),
                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}
                if reason == "stop"
                else None,
            }
            yield ("data: " + json.dumps(item) + "\n\n").encode()
        yield b"data: [DONE]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")
