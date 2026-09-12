# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import shutil
import tarfile
from pathlib import Path

import httpx

from ach_agent.engine.hydrate import Context

_KINDS = ("skills", "prompts", "artifacts")


def _link_directory(
    link: Path,
    target: Path,
    *,
    create_target: bool = True,
    replace_managed: bool = False,
) -> None:
    """Create a stable engine-local link without replacing existing native files."""
    if create_target:
        target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink():
        if link.resolve() != target.resolve():
            raise ValueError(f"engine context link points outside public context: {link}")
        return
    if link.exists():
        if not link.is_dir() or any(link.iterdir()):
            if not replace_managed or not link.is_dir():
                raise ValueError(f"engine context path is already occupied: {link}")
            # Older in-process boots put hydrated context directly in these managed
            # directories. Preserve it beside the new public link; native sessions
            # and all other home files remain untouched.
            backup = link.with_name(f"{link.name}.pre-split")
            if backup.exists() or backup.is_symlink():
                raise ValueError(f"cannot preserve previous engine context path: {backup}")
            link.rename(backup)
        else:
            link.rmdir()
    link.symlink_to(target, target_is_directory=True)


def link_public_context(
    engine_home: str | Path,
    public_context: str | Path,
    *,
    create_public: bool = True,
) -> None:
    """Expose hydrated public context through engine discovery paths.

    Harness hydration writes ``public_context``.  E owns ``engine_home`` and only
    creates links, keeping private native files and the public context mount distinct.
    """
    home = Path(engine_home)
    root = Path(public_context)
    home.mkdir(parents=True, exist_ok=True)
    if create_public:
        root.mkdir(parents=True, exist_ok=True)
    _link_directory(
        home / ".ach-state", root, create_target=create_public, replace_managed=True
    )
    skills = root / "skills"
    if create_public:
        skills.mkdir(parents=True, exist_ok=True)
    _link_directory(
        home / ".config" / "opencode" / "skills",
        skills,
        create_target=create_public,
        replace_managed=True,
    )
    _link_directory(
        home / "pi" / "skills", skills, create_target=create_public, replace_managed=True
    )


async def _get_bytes(url: str, ek: str) -> bytes:
    async with httpx.AsyncClient(timeout=30) as c:
        # ACH auth is the `x-ach-key` header, NOT `Authorization: Bearer` (the latter
        # returns 400 "malformed bearer key" — confirmed vs real ACH content endpoint).
        r = await c.get(url, headers={"x-ach-key": ek})
        r.raise_for_status()
        return r.content


async def fetch_context(ctx: Context, ek: str, root: Path, skills_dir: Path) -> None:
    """Download + extract hydrated context.

    Skills extract FLAT into ``skills_dir`` (= ``<home>/.config/opencode/skills``): the
    tarball already carries a ``<bare-skill-name>/`` top directory, so extracting it there
    yields ``skills_dir/<bare>/SKILL.md`` — the exact layout opencode scans (skill discovery
    is NOT configurable via opencode.json). The registry-qualified ``item.name`` is NOT used
    as a wrapper dir for skills (it caused a double-nest opencode never found).

    ``prompts``/``artifacts`` keep their ``root/<kind>/<item.name>`` layout (opencode does
    not auto-load them; they are addressable by path).

    ``skills_dir`` is RECONCILED (wiped) before extraction: the HOME is now stable and
    persistent, so a skill extracted on a previous boot would otherwise linger and be loaded
    by opencode even after it is removed upstream or added to ``capability.filter.exclude.skills``.
    Wiping first makes the on-disk skill set always equal the current (post-exclusion) manifest.
    """
    if skills_dir.exists():
        shutil.rmtree(skills_dir)
    for kind in _KINDS:
        items = getattr(ctx, kind)
        for item in items:
            data = await _get_bytes(item.download_url, ek)
            target_dir = skills_dir if kind == "skills" else root / kind / item.name
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                target_dir.mkdir(parents=True, exist_ok=True)
                tar.extractall(target_dir, filter="data")
