# SPDX-License-Identifier: MIT
"""ACH-compatible stand-in for the compose example (contract v2 3.3.1). Accepts any x-ach-key.
Not for production."""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

CATALOG = Path(__file__).with_name("catalog.json")
UPSTREAM = os.environ.get("MODEL_UPSTREAM_URL", "").rstrip("/")  # e.g. http://host.docker.internal:11434
API_KEY = os.environ.get("MODEL_API_KEY", "")
client = httpx.AsyncClient(timeout=httpx.Timeout(None, connect=10))

_MODEL_PREFIXES = ("v1/", "gemini/", "anthropic/")
_DROP_RESPONSE = {"content-length", "content-encoding", "transfer-encoding", "connection"}


async def hydrate(request: Request) -> Response:
    return JSONResponse(json.loads(CATALOG.read_text()))


async def model_info(request: Request) -> Response:
    return JSONResponse({"data": []})  # limits and costs are then unknown, and reported as such


async def healthz(request: Request) -> Response:
    return JSONResponse({"status": "ok"})


async def model_traffic(request: Request) -> Response:
    path = request.path_params["path"]
    if not path.startswith(_MODEL_PREFIXES):
        return Response(status_code=404)
    if not UPSTREAM:
        return JSONResponse({"error": "set MODEL_UPSTREAM_URL"}, status_code=503)
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host", "content-length", "connection", "authorization"}
        and not k.lower().startswith("x-ach-")
    }
    if API_KEY:
        headers["authorization"] = f"Bearer {API_KEY}"
    upstream = client.build_request(
        request.method, f"{UPSTREAM}/{path}", params=request.query_params, headers=headers, content=await request.body()
    )
    try:
        response = await client.send(upstream, stream=True)
    except httpx.HTTPError as exc:
        return JSONResponse({"error": f"model upstream unreachable: {exc}"}, status_code=502)
    return StreamingResponse(
        response.aiter_bytes(), status_code=response.status_code,
        headers={k: v for k, v in response.headers.items() if k.lower() not in _DROP_RESPONSE},
        background=BackgroundTask(response.aclose),
    )


app = Starlette(routes=[
    Route("/platform/hydrate", hydrate, methods=["POST"]),
    Route("/v2/model/info", model_info, methods=["GET"]),
    Route("/healthz", healthz, methods=["GET"]),
    Route("/{path:path}", model_traffic, methods=["GET", "POST"]),
])

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9000)
