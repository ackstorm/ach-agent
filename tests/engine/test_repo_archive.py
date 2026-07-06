from __future__ import annotations

import base64
from contextlib import asynccontextmanager
from typing import Any

import pytest

from ach_agent.engine import repo_archive


def test_build_uri_whole_repo() -> None:
    assert repo_archive.build_archive_uri("1234", "9af2c1e0", None) == "gitlab://1234/archive/9af2c1e0"


def test_build_uri_subpath_keeps_slashes() -> None:
    assert (
        repo_archive.build_archive_uri("1234", "9af2c1e0", "src/app")
        == "gitlab://1234/archive/9af2c1e0/src/app"
    )


def test_build_uri_encodes_specials_in_subpath() -> None:
    # a space must be encoded; slashes must survive
    assert (
        repo_archive.build_archive_uri("1234", "9af2c1e0", "my dir/x")
        == "gitlab://1234/archive/9af2c1e0/my%20dir/x"
    )


@pytest.mark.asyncio
async def test_read_repo_archive_decodes_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = b"\x1f\x8b\x08fake-gzip-bytes"

    class _Blob:
        blob = base64.b64encode(raw).decode()

    class _Result:
        contents = [_Blob()]

    class _Session:
        async def read_resource(self, uri: Any) -> _Result:
            assert str(uri) == "gitlab://1234/archive/abc"
            return _Result()

    @asynccontextmanager
    async def _fake_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
        assert endpoint == "https://mcp.example/gitlab"
        assert ek == "ek_test"
        yield _Session()

    monkeypatch.setattr(repo_archive, "_archive_session", _fake_session)
    out = await repo_archive.read_repo_archive("https://mcp.example/gitlab", "ek_test", "1234", "abc")
    assert out == raw


@pytest.mark.asyncio
async def test_read_repo_archive_propagates_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Session:
        async def read_resource(self, uri: Any) -> Any:
            raise RuntimeError("archive exceeds cap")

    @asynccontextmanager
    async def _fake_session(endpoint: str, ek: str):  # type: ignore[no-untyped-def]
        yield _Session()

    monkeypatch.setattr(repo_archive, "_archive_session", _fake_session)
    with pytest.raises(RuntimeError, match="exceeds cap"):
        await repo_archive.read_repo_archive("e", "k", "1234", "abc")
