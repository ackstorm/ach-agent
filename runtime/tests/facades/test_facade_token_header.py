# SPDX-License-Identifier: Apache-2.0
"""The facade token travels only in its header: never in a URL, never upstream."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Mount

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.execution.app import _validate_configuration
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.memory import _AuthGuard
from ach_runtime.facades.model import ModelFacade
from ach_runtime.harness.tokens import FACADE_TOKEN_HEADER, facade_token

KEY = b"k" * 32
MODEL = "anthropic/claude-sonnet-5.5"
IDENTITY = ProcessIdentity(agent="a", environment="e")
TOKEN = facade_token(KEY, "ws")
FIXTURE = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


async def _owned(claim: str) -> bool:
    return claim == "ws"


def _upstream(seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _model_app(seen: list[httpx.Request]) -> Starlette:
    return ModelFacade(
        endpoint="https://up.example/v1", headers={"authorization": "Bearer real"}, api="openai",
        model=MODEL, key=KEY, owned_workspace=_owned, identity=IDENTITY, client=_upstream(seen),
    ).app()


def _mcp_app(seen: list[httpx.Request]) -> Starlette:
    return McpFacade(
        servers={"m1": "https://mcp.example/mcp"}, server_headers={"m1": {}}, key=KEY,
        owned_workspace=_owned, exclude=frozenset(), exclude_tools=frozenset(), identity=IDENTITY,
        client=_upstream(seen),
    ).app()


async def _post(app: Starlette, path: str, **kwargs) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://f") as client:
        return await client.post(path, json={"model": MODEL, "id": 1, "method": "ping"}, **kwargs)


@pytest.mark.parametrize(
    ("build", "path", "status"),
    [
        (_model_app, "/chat/completions", 403),
        (_model_app, f"/t/{TOKEN}/chat/completions", 403),
        (_mcp_app, "/mcp/m1", 403),
        (_mcp_app, f"/t/{TOKEN}/mcp/m1", 404),
    ],
)
async def test_a_request_without_the_header_is_refused_even_with_the_token_in_its_path(
    build, path: str, status: int
) -> None:
    seen: list[httpx.Request] = []

    response = await _post(build(seen), path)

    assert response.status_code == status
    assert seen == []


@pytest.mark.parametrize(
    ("build", "path"), [(_model_app, "/chat/completions"), (_mcp_app, "/mcp/m1")]
)
async def test_the_token_is_accepted_in_its_header_and_never_sent_upstream(build, path: str) -> None:
    seen: list[httpx.Request] = []

    response = await _post(build(seen), path, headers={FACADE_TOKEN_HEADER: TOKEN})

    assert response.status_code == 200
    assert len(seen) == 1
    assert FACADE_TOKEN_HEADER not in seen[0].headers
    assert TOKEN not in str(seen[0].url)
    assert all(TOKEN not in value for value in seen[0].headers.values())


@pytest.mark.parametrize(
    ("headers", "status"),
    [({}, 403), ({FACADE_TOKEN_HEADER: "garbage"}, 403), ({FACADE_TOKEN_HEADER: TOKEN}, 200)],
)
async def test_mcp_tool_facades_read_the_token_from_the_header(headers: dict, status: int) -> None:
    async def inner(scope, receive, send) -> None:
        await PlainTextResponse("ok")(scope, receive, send)

    guarded = _AuthGuard(inner, key=KEY, owned_workspace=_owned)
    app = Starlette(routes=[Mount("/memory", app=guarded)])

    assert (await _post(app, "/memory/", headers=headers)).status_code == status


def _catalog(public, *, provider_headers, mcp_headers) -> SimpleNamespace:
    provider = {"baseURL": "http://f/model", "apiKey": "local-proxy", "models": {public.model.name: {}}}
    if provider_headers is not None:
        provider["headers"] = provider_headers
    return SimpleNamespace(
        public=public,
        catalog={
            "providers": {public.model.type: provider},
            "mcp": {"m1": {"url": "http://f/mcp/mcp/m1", "headers": mcp_headers}},
        },
    )


@pytest.fixture
def public():
    config = RuntimeConfig.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    return public_config(config, engine_env=config.engine.env)


def test_execution_accepts_the_token_header_only(public) -> None:
    good = {FACADE_TOKEN_HEADER: TOKEN}
    _validate_configuration(_catalog(public, provider_headers=good, mcp_headers=good), None)


@pytest.mark.parametrize(
    "bad",
    [{}, {FACADE_TOKEN_HEADER: ""}, {"authorization": "Bearer x"}, {FACADE_TOKEN_HEADER: TOKEN, "x-other": "y"}],
)
def test_execution_rejects_any_other_headers(public, bad: dict) -> None:
    good = {FACADE_TOKEN_HEADER: TOKEN}
    with pytest.raises(ValueError, match="facade token header"):
        _validate_configuration(_catalog(public, provider_headers=bad, mcp_headers=good), None)
    with pytest.raises(ValueError, match="facade token header"):
        _validate_configuration(_catalog(public, provider_headers=good, mcp_headers=bad), None)


def test_execution_rejects_a_provider_without_headers(public) -> None:
    good = {FACADE_TOKEN_HEADER: TOKEN}
    with pytest.raises(ValueError, match="public_catalog shape"):
        _validate_configuration(_catalog(public, provider_headers=None, mcp_headers=good), None)
