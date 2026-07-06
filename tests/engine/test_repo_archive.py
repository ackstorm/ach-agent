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


import io
import os
import tarfile
import time
from pathlib import Path


def _make_targz(top: str, files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(content)
            tf.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def test_extract_returns_repo_root(tmp_path: Path) -> None:
    data = _make_targz("myrepo-9af2-9af2", {"README.md": b"hi", "src/app.py": b"x=1"})
    root = repo_archive.extract_archive(data, str(tmp_path), "1234", "9af2c1e0")
    assert Path(root).name == "myrepo-9af2-9af2"
    assert (Path(root) / "README.md").read_bytes() == b"hi"
    assert (Path(root) / "src" / "app.py").exists()


def test_extract_blocks_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo("../escape.txt")
        payload = b"evil"
        info.size = len(payload)
        tf.addfile(info, io.BytesIO(payload))
    # filter="data" blocks traversal — Python 3.12 RAISES (OutsideDestinationError, a
    # FilterError) rather than writing outside the dest; the facade fail-softs on it.
    with pytest.raises(tarfile.FilterError):
        repo_archive.extract_archive(buf.getvalue(), str(tmp_path), "1234", "sha")
    assert not (tmp_path.parent / "escape.txt").exists()


def test_sweep_removes_stale_keeps_fresh(tmp_path: Path) -> None:
    base = tmp_path / "gitlab"
    base.mkdir()
    old = base / "old"
    old.mkdir()
    fresh = base / "fresh"
    fresh.mkdir()
    now = 1_000_000.0
    os.utime(old, (now - 7200, now - 7200))  # 2h old
    os.utime(fresh, (now - 60, now - 60))  # 1min old
    removed = repo_archive.sweep_stale(str(base), 3600.0, now)
    assert removed == 1
    assert not old.exists()
    assert fresh.exists()


def test_sweep_missing_base_is_noop() -> None:
    assert repo_archive.sweep_stale("/tmp/does-not-exist-xyz", 3600.0, time.time()) == 0
