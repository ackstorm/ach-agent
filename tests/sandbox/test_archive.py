from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.sandbox.archive import ArchiveTooLarge, extract, pack, write_capped


def test_pack_extract_roundtrip(tmp_path: Path) -> None:
    src = tmp_path / "home"
    (src / "workspace" / "repo").mkdir(parents=True)
    (src / "workspace" / "repo" / "f.txt").write_text("hi")
    (src / "link").symlink_to("workspace/repo/f.txt")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000_000)
    assert (dest / "workspace" / "repo" / "f.txt").read_text() == "hi"
    assert (dest / "link").is_symlink()


def test_pack_over_cap_raises_and_removes(tmp_path: Path) -> None:
    src = tmp_path / "home"
    src.mkdir()
    (src / "big").write_bytes(b"\0" * 10_000)
    archive = tmp_path / "a.tar.gz"
    with pytest.raises(ArchiveTooLarge):
        pack(src, archive, max_bytes=10)
    assert not archive.exists()


def _evil(tmp_path: Path, name: str) -> Path:
    archive = tmp_path / "evil.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo(name)
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    return archive


@pytest.mark.parametrize("name", ["../escape", "/etc/passwd"])
def test_extract_rejects_traversal(tmp_path: Path, name: str) -> None:
    with pytest.raises(tarfile.TarError):
        extract(_evil(tmp_path, name), tmp_path / "out", max_expanded_bytes=100)


def test_extract_rejects_bomb(tmp_path: Path) -> None:
    src = tmp_path / "home"
    src.mkdir()
    (src / "zeros").write_bytes(b"\0" * 1_000_000)
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    with pytest.raises(ArchiveTooLarge):
        extract(archive, tmp_path / "out", max_expanded_bytes=1000)


async def test_write_capped(tmp_path: Path) -> None:
    async def chunks():
        for _ in range(3):
            yield b"abcd"

    dest = tmp_path / "x"
    assert await write_capped(chunks(), dest, max_bytes=12) == 12
    with pytest.raises(ArchiveTooLarge):
        await write_capped(chunks(), tmp_path / "y", max_bytes=11)
    assert not (tmp_path / "y").exists()


def test_home_roundtrip_keeps_absolute_symlink(tmp_path: Path) -> None:
    src = tmp_path / "home"
    (src / ".venv" / "bin").mkdir(parents=True)
    (src / ".venv" / "bin" / "python").symlink_to("/usr/bin/python3")
    archive = tmp_path / "a.tar.gz"
    pack(src, archive, max_bytes=10_000_000)
    dest = tmp_path / "out"
    extract(archive, dest, max_expanded_bytes=10_000_000, filter="tar")
    assert (dest / ".venv" / "bin" / "python").readlink() == Path("/usr/bin/python3")
    with pytest.raises(tarfile.TarError):
        extract(archive, tmp_path / "strict", max_expanded_bytes=10_000_000)
