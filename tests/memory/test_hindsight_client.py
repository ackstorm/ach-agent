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
