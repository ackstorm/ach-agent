# SPDX-License-Identifier: Apache-2.0
import io
import tarfile

import httpx
import pytest

from ach_runtime.config.schema import StandaloneSkill
from ach_runtime.harness.skills import SkillsError, fetch_skills


def _archive(files: dict[str, tuple[str, int]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, (text, mode) in files.items():
            data = text.encode()
            info = tarfile.TarInfo(path)
            info.size, info.mode = len(data), mode
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


REPO = _archive({
    "toolkit-abc123/README.md": ("x", 0o644),
    "toolkit-abc123/skills/triage/SKILL.md": ("# triage", 0o644),
    "toolkit-abc123/skills/triage/references/runbook.md": ("steps", 0o644),
    "toolkit-abc123/skills/triage/scripts/check.sh": ("#!/bin/sh\n", 0o755),
    "toolkit-abc123/skills/report/SKILL.md": ("# report", 0o644),
})


def _skill(**fields) -> StandaloneSkill:
    return StandaloneSkill.model_validate({"name": "toolkit", "url": "https://a.example/t.tar.gz", **fields})


@pytest.mark.asyncio
async def test_fetch_skills_installs_every_skill_directory_whole(tmp_path) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=REPO)))
    names = await fetch_skills([_skill()], {}, tmp_path, excluded=["report"], client=client)

    assert names == ["triage"]
    skill = tmp_path / "skills" / "triage"
    assert (skill / "SKILL.md").read_text() == "# triage"
    assert (skill / "references" / "runbook.md").read_text() == "steps"
    assert (skill / "scripts" / "check.sh").stat().st_mode & 0o100
    assert not (tmp_path / "skills" / "report").exists()


@pytest.mark.asyncio
async def test_fetch_skills_drops_headers_on_a_redirect_to_another_host(tmp_path) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("private-token")))
        if request.url.host == "gitlab.example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.net/a.tar.gz"})
        return httpx.Response(200, content=REPO)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    skill = _skill(type="gitlab", url=None, repo="platform/toolkit", baseUrl="https://gitlab.example.com",
                   headers={"PRIVATE-TOKEN": {"env": "T"}})
    await fetch_skills([skill], {"T": "secret"}, tmp_path, excluded=[], client=client)
    assert seen == [("gitlab.example.com", "secret"), ("cdn.example.net", None)]


@pytest.mark.asyncio
@pytest.mark.parametrize("content, status, match", [
    (b"", 404, "404"),
    (_archive({"x/README.md": ("x", 0o644)}), 200, "no SKILL.md"),
    (_archive({"a/triage/SKILL.md": ("1", 0o644), "b/triage/SKILL.md": ("2", 0o644)}), 200, "duplicate"),
])
async def test_fetch_skills_fails_closed(tmp_path, content, status, match) -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, content=content)))
    with pytest.raises(SkillsError, match=match):
        await fetch_skills([_skill()], {}, tmp_path, excluded=[], client=client)


@pytest.mark.asyncio
async def test_fetch_skills_rejects_a_symbolic_link(tmp_path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        data = b"# s"
        info = tarfile.TarInfo("r/s/SKILL.md")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("r/s/loop")
        link.type, link.linkname = tarfile.SYMTYPE, "."
        tar.addfile(link)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=buffer.getvalue())))
    with pytest.raises(SkillsError, match="symbolic or hard link"):
        await fetch_skills([_skill()], {}, tmp_path, excluded=[], client=client)
    assert list((tmp_path / "skills").iterdir()) == []


@pytest.mark.asyncio
async def test_fetch_skills_drops_headers_on_a_same_host_downgrade(tmp_path) -> None:
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.scheme, request.headers.get("authorization")))
        if request.url.scheme == "https":
            return httpx.Response(302, headers={"location": "http://a.example/t.tar.gz"})
        return httpx.Response(200, content=REPO)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    skill = _skill(headers={"Authorization": {"env": "T"}})
    await fetch_skills([skill], {"T": "secret"}, tmp_path, excluded=[], client=client)
    assert seen == [("https", "secret"), ("http", None)]


@pytest.mark.asyncio
async def test_fetch_skills_rejects_a_hardlink(tmp_path) -> None:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for path, data in (("r/s/SKILL.md", b"# s"), ("r/s/big.bin", b"x" * 1000)):
            info = tarfile.TarInfo(path)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("r/s/copy.bin")
        link.type, link.linkname = tarfile.LNKTYPE, "r/s/big.bin"
        tar.addfile(link)
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=buffer.getvalue())))
    with pytest.raises(SkillsError, match="link"):
        await fetch_skills([_skill()], {}, tmp_path, excluded=[], client=client)
    assert list((tmp_path / "skills").iterdir()) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("fields", [{"type": "github", "repo": "o/r"}, {"type": "gitlab", "repo": "o/r", "baseUrl": "https://g.example"}])
async def test_fetch_skills_names_a_root_skill_after_the_entry_in_a_typed_archive(tmp_path, fields) -> None:
    content = _archive({
        "repo-sha/SKILL.md": ("# root", 0o644),
        "repo-sha/references/a.md": ("ref", 0o644),
    })
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=content)))
    names = await fetch_skills([_skill(url=None, **fields)], {}, tmp_path, excluded=[], client=client)

    assert names == ["toolkit"]
    assert (tmp_path / "skills" / "toolkit" / "SKILL.md").read_text() == "# root"
    assert (tmp_path / "skills" / "toolkit" / "references" / "a.md").read_text() == "ref"
