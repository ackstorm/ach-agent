# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import io
import os
import shutil
import tarfile
from pathlib import Path

import httpx

from ach_agent.engine.hydrate import Context

_KINDS = ("skills", "prompts", "artifacts")
_BATCH_PREFIX = ".ach-harness-shared-files-"
_TRANSFER_ROOT_NAMES = frozenset({"transfer", "ach-agent-transfer"})


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
    work_dir: str | Path | None = None,
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
    _link_directory(home / ".ach-state", root, create_target=create_public, replace_managed=True)
    if work_dir is not None and Path(work_dir).resolve() != home.resolve():
        work = Path(work_dir)
        work.mkdir(parents=True, exist_ok=True)
        _link_directory(
            work / ".ach-state", root, create_target=create_public, replace_managed=True
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


def _safe_batch(path: str | Path) -> Path:
    """Validate one disposable transfer batch and all paths below it."""
    batch = Path(path)
    if (
        not batch.is_absolute()
        or not batch.name.startswith(_BATCH_PREFIX)
        or batch.parent.name not in _TRANSFER_ROOT_NAMES
    ):
        raise ValueError("hydrationDir must identify an absolute transfer batch")
    if batch.is_symlink() or not batch.is_dir():
        raise ValueError("hydrationDir must be a real directory")
    resolved = batch.resolve(strict=True)
    if resolved != batch.absolute():
        raise ValueError("hydrationDir must not contain symlinked path components")
    for child in resolved.rglob("*"):
        if child.is_symlink():
            try:
                child.resolve(strict=True).relative_to(resolved)
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError("hydration batch symlink escapes its transfer root") from exc
    return resolved


def _copy_tree_contents(source: Path, destination: Path) -> None:
    """Replace one managed directory while leaving unrelated HOME data intact."""
    if source.exists() and not source.is_dir():
        raise ValueError(f"hydration input is not a directory: {source.name}")
    if destination.is_symlink():
        destination.unlink()
    elif destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if source.exists():
        for item in source.iterdir():
            target = destination / item.name
            if item.is_dir():
                shutil.copytree(item, target, symlinks=False)
            else:
                shutil.copy2(item, target)


def install_hydration(hydration_dir: str | Path, engine_home: str | Path, skills_dir: Path) -> None:
    """Copy managed hydration into engine-owned HOME; no links into staging."""
    batch = _safe_batch(hydration_dir)
    home = Path(engine_home)
    home.mkdir(parents=True, exist_ok=True)
    state = home / ".ach-state"
    if state.is_symlink():
        state.unlink()
    state.mkdir(parents=True, exist_ok=True)
    _copy_tree_contents(batch / "skills", skills_dir)
    _copy_tree_contents(batch / "prompts", state / "prompts")
    _copy_tree_contents(batch / "artifacts", state / "artifacts")


def delete_hydration_batch(hydration_dir: str | Path) -> None:
    """Delete exactly a validated batch, never its transfer mount."""
    shutil.rmtree(_safe_batch(hydration_dir))


def install_legacy_codemem(hydration_dir: str | Path, target: str | Path) -> None:
    """Import H's legacy SQLite backup once into E-owned storage.

    A marker beside the target makes repeated H restarts idempotent while refusing
    to overwrite an E database whose provenance is unknown.
    """
    batch = _safe_batch(hydration_dir)
    source = batch / "codemem.db"
    if not source.exists():
        return
    destination = Path(target)
    marker = destination.with_name(destination.name + ".ach-migrated")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        if marker.is_file():
            return
        raise ValueError(f"codemem target already exists without migration marker: {destination}")
    temporary = destination.with_name(destination.name + ".ach-import-tmp")
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, destination)
        marker.write_text("legacy codemem imported\n", encoding="utf-8")
    finally:
        temporary.unlink(missing_ok=True)


def migrate_legacy_workspace(engine_home: str | Path, work_dir: str | Path) -> None:
    """Copy an old ``<home>/workspace`` into the new sibling workspace once."""
    source = Path(engine_home) / "workspace"
    target = Path(work_dir)
    if source.resolve(strict=False) == target.resolve(strict=False) or not source.exists():
        return
    marker = Path(engine_home) / ".ach-workspace-migrated"
    if source.is_symlink():
        raise ValueError("legacy workspace must be a real directory")
    if not source.is_dir():
        raise ValueError("legacy workspace must be a real directory")
    if target.exists():
        if not target.is_dir():
            raise ValueError("new workspace target is not a directory")
        if marker.is_file():
            return
        if any(target.iterdir()):
            raise ValueError("legacy workspace migration target is nonempty")
    else:
        target.mkdir(parents=True)
    for item in source.iterdir():
        # The old layout commonly linked this managed context back into HOME.
        # The new boot recreates the workspace link after migration.
        if item.name == ".ach-state":
            continue
        destination = target / item.name
        if item.is_symlink():
            destination.symlink_to(item.readlink(), target_is_directory=item.is_dir())
        elif item.is_dir():
            shutil.copytree(item, destination, symlinks=True)
        else:
            shutil.copy2(item, destination)
    marker.write_text("legacy workspace migrated\n", encoding="utf-8")


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
