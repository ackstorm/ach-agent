# SPDX-License-Identifier: Apache-2.0
"""Hindsight client seam: auth headers, the single call_hindsight seam, tool-name constants.

NOTE (SDK deviation): the installed mcp SDK's ``streamable_http_client`` takes no ``headers=``
kwarg — auth is injected by building an httpx client via ``create_mcp_http_client(headers=...)``
and passing it as ``http_client=``. The test therefore asserts the Authorization header lands
on the http_client, not on a streamable_http_client kwarg.
"""

from contextlib import asynccontextmanager

import pytest

from ach_agent.memory import hindsight as hs


def test_auth_headers_bearer():
    assert hs.hindsight_auth_headers("sekret") == {"Authorization": "Bearer sekret"}


def test_auth_headers_empty_when_no_secret():
    assert hs.hindsight_auth_headers(None) == {}  # internal URL — no auth


@pytest.mark.asyncio
async def test_call_hindsight_passes_headers_and_returns_text(monkeypatch):
    seen = {}

    class _Content:
        text = "OK-BODY"

    class _Result:
        content = [_Content()]

    class _Session:
        def __init__(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool, args):
            seen["tool"] = tool
            seen["args"] = args
            return _Result()

    @asynccontextmanager
    async def _fake_client(url, *, http_client=None, **k):
        seen["url"] = url
        # auth is carried on the pre-built httpx client, not a kwarg on this call.
        seen["auth"] = http_client.headers.get("Authorization") if http_client is not None else None
        yield (object(), object(), object())

    monkeypatch.setattr(hs, "streamable_http_client", _fake_client)
    monkeypatch.setattr(hs, "ClientSession", lambda read, write: _Session())

    out = await hs.call_hindsight("https://hs/mcp", "sekret", hs.HINDSIGHT_RECALL, {"query": "q"})
    assert out == "OK-BODY"
    assert seen["url"] == "https://hs/mcp"
    assert seen["auth"] == "Bearer sekret"
    assert seen["tool"] == "hindsight_recall"
    assert seen["args"] == {"query": "q"}


@pytest.mark.asyncio
async def test_call_hindsight_raises_on_tool_error(monkeypatch):
    """A tool-level error (isError=True) must raise, not return the error text as data."""

    class _Content:
        text = "Unknown tool: 'hindsight_recall'"

    class _Result:
        content = [_Content()]
        isError = True

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def initialize(self):
            pass

        async def call_tool(self, tool, args):
            return _Result()

    @asynccontextmanager
    async def _fake_client(url, *, http_client=None, **k):
        yield (object(), object(), object())

    monkeypatch.setattr(hs, "streamable_http_client", _fake_client)
    monkeypatch.setattr(hs, "ClientSession", lambda read, write: _Session())

    with pytest.raises(RuntimeError, match="Unknown tool"):
        await hs.call_hindsight("https://hs/mcp", None, hs.HINDSIGHT_RECALL, {"query": "q"})


def test_build_tool_aliases_prefixed_identity():
    published = [
        "hindsight_recall",
        "hindsight_reflect",
        "hindsight_retain",
        "hindsight_get_mental_model",
        "hindsight_create_bank",
        "hindsight_create_mental_model",
        "hindsight_refresh_mental_model",
        "hindsight_delete_bank",  # extra tool — ignored
    ]
    aliases = hs.build_tool_aliases(published)
    assert aliases[hs.HINDSIGHT_RECALL] == "hindsight_recall"
    assert aliases[hs.HINDSIGHT_GET_MENTAL_MODEL] == "hindsight_get_mental_model"
    assert len(aliases) == 7


def test_build_tool_aliases_unprefixed():
    published = ["recall", "reflect", "retain", "get_mental_model", "create_bank",
                 "create_mental_model", "refresh_mental_model"]
    aliases = hs.build_tool_aliases(published)
    assert aliases[hs.HINDSIGHT_RECALL] == "recall"
    assert aliases[hs.HINDSIGHT_GET_MENTAL_MODEL] == "get_mental_model"
    # the three *_mental_model tools must not collide
    assert aliases[hs.HINDSIGHT_CREATE_MENTAL_MODEL] == "create_mental_model"
    assert aliases[hs.HINDSIGHT_REFRESH_MENTAL_MODEL] == "refresh_mental_model"


def test_build_tool_aliases_missing_dropped():
    aliases = hs.build_tool_aliases(["recall"])  # only one published
    assert aliases == {hs.HINDSIGHT_RECALL: "recall"}
