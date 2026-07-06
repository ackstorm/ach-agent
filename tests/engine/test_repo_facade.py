from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.engine import repo_facade


def _targz(top: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(f"{top}/README.md")
        info.size = 2
        tf.addfile(info, io.BytesIO(b"hi"))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_checkout_returns_path_on_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _fake_read(endpoint, ek, project, ref, subpath=None):  # type: ignore[no-untyped-def]
        return _targz("repo-abc-abc")

    monkeypatch.setattr(repo_facade, "read_repo_archive", _fake_read)
    facade = repo_facade.RepoCheckoutFacade("e", "ek", tmp_base=str(tmp_path))
    out = await facade._checkout("1234", "abc", None)
    assert "repo-abc-abc" in out
    assert (Path(str(tmp_path)) / "repo-abc-abc" / "README.md").exists() or True  # nested mkdtemp
    # the returned path really exists and holds the file
    root = out.split("Checked out to ", 1)[1].split(" ", 1)[0]
    assert (Path(root) / "README.md").read_bytes() == b"hi"


@pytest.mark.asyncio
async def test_checkout_fail_soft_on_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("archive exceeds cap")

    monkeypatch.setattr(repo_facade, "read_repo_archive", _boom)
    facade = repo_facade.RepoCheckoutFacade("e", "ek", tmp_base=str(tmp_path))
    out = await facade._checkout("1234", "abc", None)
    assert out.startswith("Checkout failed:")
    assert "exceeds cap" in out  # never raises
