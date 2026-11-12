# SPDX-License-Identifier: Apache-2.0
"""Standalone skills: download each configured archive and lay out `skills/<name>/`."""

from __future__ import annotations

import os
import shutil
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ach_runtime.config.schema import StandaloneSkill
from ach_runtime.storage.archive import extract

_MAX_ARCHIVE_BYTES = 512 * 1024**2
_MAX_REDIRECTS = 5
_TIMEOUT_SECONDS = 60.0


class SkillsError(RuntimeError):
    """A configured skills archive could not be downloaded or holds no usable skill."""


async def _download(client: httpx.AsyncClient, url: str, headers: Mapping[str, str]) -> bytes:
    origin = httpx.URL(url).host
    for _ in range(_MAX_REDIRECTS + 1):
        # A provider redirects the archive to a storage host: the credential stays with the first host.
        sent = headers if httpx.URL(url).host == origin else {}
        response = await client.get(url, headers=dict(sent), timeout=_TIMEOUT_SECONDS, follow_redirects=False)
        if response.is_redirect and "location" in response.headers:
            url = urljoin(url, response.headers["location"])
            continue
        if response.status_code != 200:
            raise SkillsError(f"download answered {response.status_code}")
        if len(response.content) > _MAX_ARCHIVE_BYTES:
            raise SkillsError("archive exceeds 512 MiB")
        return response.content
    raise SkillsError("too many redirects")


def _skill_dirs(root: Path) -> list[Path]:
    found: list[Path] = []
    for current, dirs, files in os.walk(root):
        if "SKILL.md" in files:
            found.append(Path(current))
            dirs.clear()  # a skill's own tree is not searched for nested skills
        else:
            dirs.sort()
    return found


async def fetch_skills(
    skills: Iterable[StandaloneSkill], env: Mapping[str, str], root: Path, *,
    excluded: Iterable[str], client: httpx.AsyncClient,
) -> list[str]:
    """Install every skill of every archive under `root/skills/`; returns their names."""
    from ach_runtime.harness.control import resolve_headers

    skip, installed = set(excluded), []
    target_root = root / "skills"
    target_root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        headers, _ = resolve_headers(skill.headers, env)
        try:
            content = await _download(client, skill.archive_url(), headers)
        except httpx.HTTPError as exc:
            raise SkillsError(f"skills {skill.name}: {type(exc).__name__}") from exc
        except SkillsError as exc:
            raise SkillsError(f"skills {skill.name}: {exc}") from exc
        with tempfile.TemporaryDirectory(prefix="ach-skills-") as scratch:
            archive = Path(scratch) / "archive.tar.gz"
            archive.write_bytes(content)
            staged = Path(scratch) / "payload"
            try:
                extract(archive, staged, max_expanded_bytes=4 * 1024**3, max_members=10_000)
            except Exception as exc:  # noqa: BLE001 - any unreadable archive stops the agent
                raise SkillsError(f"skills {skill.name}: archive could not be extracted") from exc
            found = _skill_dirs(staged)
            if not found:
                raise SkillsError(f"skills {skill.name}: archive has no SKILL.md")
            for directory in found:
                name = skill.name if directory == staged else directory.name
                if name in skip:
                    continue
                if (target_root / name).exists():
                    raise SkillsError(f"skills {skill.name}: duplicate skill name {name}")
                shutil.copytree(directory, target_root / name)
                installed.append(name)
    return installed
