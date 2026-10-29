# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

import ach_runtime.mcpcall as mcpcall


class _Session:
    def __init__(self, tools: list[str], text: str = "", is_error: bool = False) -> None:
        self.tools = tools
        self.text = text
        self.is_error = is_error
        self.calls: list[tuple[str, dict]] = []

    async def list_tools(self):
        return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in self.tools])

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[SimpleNamespace(text=self.text)], isError=self.is_error)


@pytest.fixture
def session(monkeypatch) -> _Session:
    fake = _Session(["mcp-k8sgpt-ro.analyze", "mcp-k8sgpt-ro.get-logs"], text='{"problems": 0}')
    urls: list[str] = []

    @asynccontextmanager
    async def fake_mcp_session(url, headers):
        urls.append(url)
        yield fake

    monkeypatch.setattr(mcpcall, "mcp_session", fake_mcp_session)
    monkeypatch.setenv("ACH_MCP_URLS", json.dumps({"mcp-k8sgpt-ro": "http://facade/mcp/k8sgpt"}))
    fake.urls = urls
    return fake


def test_call_resolves_a_prefixed_tool_and_prints_text(session, capsys) -> None:
    assert mcpcall.main(["call", "mcp-k8sgpt-ro", "analyze", '{"explain": false}']) == 0
    assert capsys.readouterr().out == '{"problems": 0}\n'
    assert session.calls == [("mcp-k8sgpt-ro.analyze", {"explain": False})]
    assert session.urls == ["http://facade/mcp/k8sgpt"]


def test_hyphenated_bare_tool_resolves_and_arguments_default_to_empty(session) -> None:
    assert mcpcall.main(["call", "mcp-k8sgpt-ro", "get-logs"]) == 0
    assert session.calls == [("mcp-k8sgpt-ro.get-logs", {})]


@pytest.mark.parametrize(
    "advertised,tool,expected",
    [
        ({"analyze"}, "analyze", "analyze"),
        ({"srv.analyze", "analyze"}, "analyze", "analyze"),
        ({"srv.analyze"}, "analyze", "srv.analyze"),
        ({"a.analyze", "b.analyze"}, "analyze", "analyze"),
        ({"srv.list-resources"}, "resources", "resources"),
    ],
)
def test_resolve_tool(advertised, tool, expected) -> None:
    assert mcpcall.resolve_tool(advertised, tool) == expected


def test_tool_error_exits_1(session, capsys) -> None:
    session.is_error, session.text = True, "boom"
    assert mcpcall.main(["call", "mcp-k8sgpt-ro", "analyze"]) == 1
    assert "boom" in capsys.readouterr().err


def test_transport_failure_exits_1(monkeypatch, capsys) -> None:
    @asynccontextmanager
    async def broken(url, headers):
        raise ConnectionError("facade down")
        yield

    monkeypatch.setattr(mcpcall, "mcp_session", broken)
    monkeypatch.setenv("ACH_MCP_URLS", json.dumps({"srv": "http://facade/mcp/srv"}))
    assert mcpcall.main(["call", "srv", "analyze"]) == 1
    assert "facade down" in capsys.readouterr().err


def test_transport_failure_never_prints_the_facade_token(monkeypatch, capsys) -> None:
    @asynccontextmanager
    async def broken(url, headers):
        raise ConnectionError(f"401 for url {url}")
        yield

    monkeypatch.setattr(mcpcall, "mcp_session", broken)
    monkeypatch.setenv("ACH_MCP_URLS", json.dumps({"srv": "http://c/facades/mcp/t/tok-9/mcp/srv"}))
    assert mcpcall.main(["call", "srv", "analyze"]) == 1
    err = capsys.readouterr().err
    assert "tok-9" not in err and "/t/<redacted>/" in err


def test_unknown_server_exits_2(session, capsys) -> None:
    assert mcpcall.main(["call", "nope", "analyze"]) == 2
    assert "mcp-k8sgpt-ro" in capsys.readouterr().err
    assert session.calls == []


@pytest.mark.parametrize("raw", ["not json", "[1, 2]"])
def test_invalid_arguments_exit_2(session, raw) -> None:
    assert mcpcall.main(["call", "mcp-k8sgpt-ro", "analyze", raw]) == 2
    assert session.calls == []
