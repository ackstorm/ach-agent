# SPDX-License-Identifier: MIT
"""Model facade: only the configured model on the allow-listed paths reaches upstream."""

from __future__ import annotations

import httpx
import pytest

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.tokens import facade_token

KEY = b"k" * 32
MODEL = "anthropic/claude-sonnet-5.5"
GEMINI = "gemini-flash-latest"


async def _owned(claim: str) -> bool:
    return claim == "ws"


async def _send(api: str, model: str, method: str, path: str, **kwargs) -> tuple[int, int]:
    hits = 0

    def upstream(request: httpx.Request) -> httpx.Response:
        nonlocal hits
        hits += 1
        return httpx.Response(200, json={"ok": True})

    facade = ModelFacade(
        endpoint="https://up.example/v1", headers={"authorization": "Bearer real"}, api=api, model=model,
        key=KEY, owned_workspace=_owned, identity=ProcessIdentity(agent="a", environment="e"),
        client=httpx.AsyncClient(transport=httpx.MockTransport(upstream)),
    )
    token = facade_token(KEY, "ws")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=facade.app()), base_url="http://f") as client:
        response = await client.request(method, f"/t/{token}/{path}", **kwargs)
    return response.status_code, hits


@pytest.mark.parametrize(("api", "path"), [
    ("openai", "chat/completions"), ("openai", "responses"),
    ("anthropic", "messages"), ("anthropic", "messages/count_tokens"),
])
async def test_allowed_body_model_paths_reach_upstream(api, path):
    assert await _send(api, MODEL, "POST", path, json={"model": MODEL}) == (200, 1)


@pytest.mark.parametrize("action", ["generateContent", "streamGenerateContent", "countTokens"])
async def test_allowed_gemini_paths_reach_upstream(action):
    assert await _send("gemini", GEMINI, "POST", f"models/{GEMINI}:{action}?alt=sse", json={}) == (200, 1)


@pytest.mark.parametrize(("api", "method", "path", "kwargs", "status"), [
    ("openai", "POST", "chat/completions", {"json": {"model": "other"}}, 400),
    ("openai", "POST", "chat/completions", {"json": {}}, 400),
    ("openai", "POST", "chat/completions", {"content": b"not json"}, 400),
    ("openai", "POST", "chat/completions",
     {"json": {"model": MODEL}, "headers": {"content-encoding": "gzip"}}, 400),
    ("openai", "POST", "chat/completions?x=1", {"json": {"model": MODEL}}, 400),
    ("openai", "POST", "embeddings", {"json": {"model": MODEL}}, 404),
    ("openai", "GET", "models", {}, 405),
    ("openai", "POST", "chat%2Fcompletions", {"json": {"model": MODEL}}, 400),
    ("anthropic", "POST", "messages", {"json": {"model": "claude-other"}}, 400),
    ("openai", "POST", "chat/completions", {"content": b'{"model": "other", "model": "%s"}' % MODEL.encode()}, 400),
    ("openai", "POST", "chat/completions",
     {"json": {"model": MODEL}, "headers": [("content-encoding", "identity"), ("content-encoding", "gzip")]}, 400),
    ("openai", "POST", "chat/completions",
     {"json": {"model": MODEL}, "headers": {"content-encoding": "identity, gzip"}}, 400),
    *[("openai", "POST", "chat/completions", {"json": {"model": MODEL, field: "x"}}, 400)
      for field in ("fallbacks", "user_config", "api_base", "base_url", "api_key")],
    *[("anthropic", "POST", "messages", {"json": {"model": MODEL, field: "x"}}, 400)
      for field in ("fallbacks", "user_config", "api_base", "base_url", "api_key")],
    ("gemini", "POST", "models/other:generateContent", {"json": {}}, 400),
    ("gemini", "POST", f"models/{GEMINI}:generateContent?key=x", {"json": {}}, 400),
    ("gemini", "POST", f"models/{GEMINI}:embedContent", {"json": {}}, 404),
    ("gemini", "POST", f"models/{GEMINI}:generateContent", {"content": b"not json"}, 400),
    ("gemini", "POST", f"models/{GEMINI}:generateContent", {"content": b'{"a": 1, "a": 2}'}, 400),
    ("gemini", "POST", f"models/{GEMINI}:generateContent", {"content": b"[]"}, 400),
    *[("gemini", "POST", f"models/{GEMINI}:generateContent", {"json": {field: "x"}}, 400)
      for field in ("fallbacks", "user_config", "api_base", "base_url", "api_key")],
    ("gemini", "POST", "models/pub/other:generateContent", {"json": {}}, 400),
])
async def test_rejections_never_reach_upstream(api, method, path, kwargs, status):
    model = GEMINI if api == "gemini" else MODEL
    assert await _send(api, model, method, path, **kwargs) == (status, 0)


@pytest.mark.parametrize("model", ["publishers/google/models/gemini-x", "tunedModels/my-model"])
async def test_gemini_model_ids_with_slashes_match_exactly(model):
    assert await _send("gemini", model, "POST", f"models/{model}:generateContent", json={}) == (200, 1)


@pytest.mark.parametrize("path", ["models/a//b:generateContent", "models/a/./b:generateContent",
                                  "models/a/../b:generateContent", "models//a:generateContent"])
async def test_gemini_model_ids_reject_empty_and_dot_segments(path):
    model = path.removeprefix("models/").removesuffix(":generateContent")
    status, hits = await _send("gemini", model, "POST", path, json={})
    assert hits == 0 and status in (400, 404)
