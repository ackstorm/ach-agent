import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.boot.paths import ach_state_dir, link_ach_state
from ach_agent.engine.context import fetch_context, migrate_legacy_workspace
from ach_agent.engine.hydrate import Context, ContextItem


def _make_skill_targz() -> bytes:
    """A skill tarball whose top dir is the bare skill name (mirrors ACH content)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"---\nname: frontend-design\n---\nbody\n"
        info = tarfile.TarInfo("frontend-design/SKILL.md")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


@pytest.mark.asyncio
async def test_skills_extract_flat_into_skills_dir(tmp_path: Path, monkeypatch) -> None:
    blob = _make_skill_targz()

    async def fake_get_bytes(url: str, ek: str) -> bytes:
        return blob

    monkeypatch.setattr("ach_agent.engine.context._get_bytes", fake_get_bytes)

    ctx = Context(
        skills=[ContextItem(name="frontend-design@anthropics-skills", downloadUrl="http://x")]
    )
    root = tmp_path / "state"
    skills_dir = tmp_path / "home" / ".config" / "opencode" / "skills"

    await fetch_context(ctx, "ek-test", root, skills_dir)

    # opencode-scannable layout: skills_dir/<bare>/SKILL.md (one level, no item.name wrapper).
    assert (skills_dir / "frontend-design" / "SKILL.md").is_file()
    # The registry-qualified item.name must NOT appear as a wrapper directory.
    assert not (skills_dir / "frontend-design@anthropics-skills").exists()


@pytest.mark.asyncio
async def test_fetch_context_rejects_traversal(tmp_path: Path, monkeypatch) -> None:
    """fetch_context rejects a tar member that escapes the destination dir.

    Protection is now native to tarfile.extractall(..., filter="data") (py3.12+):
    a member path like "../evil" raises tarfile.OutsideDestinationError instead of
    extracting outside the destination.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        body = b"evil"
        info = tarfile.TarInfo("../evil")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    blob = buf.getvalue()

    async def fake_get_bytes(url: str, ek: str) -> bytes:
        return blob

    monkeypatch.setattr("ach_agent.engine.context._get_bytes", fake_get_bytes)

    ctx = Context(skills=[ContextItem(name="malicious@anthropics-skills", downloadUrl="http://x")])
    root = tmp_path / "state"
    skills_dir = tmp_path / "home" / ".config" / "opencode" / "skills"

    with pytest.raises(tarfile.TarError):
        await fetch_context(ctx, "ek-test", root, skills_dir)


@pytest.mark.asyncio
async def test_skills_dir_reconciled_drops_stale_skills(tmp_path: Path, monkeypatch) -> None:
    """A persistent skills_dir is wiped before re-hydration so revoked/removed skills
    do not linger and get loaded by opencode (governance gate must hold across restarts)."""
    blob = _make_skill_targz()

    async def fake_get_bytes(url: str, ek: str) -> bytes:
        return blob

    monkeypatch.setattr("ach_agent.engine.context._get_bytes", fake_get_bytes)

    skills_dir = tmp_path / "home" / ".config" / "opencode" / "skills"
    # A skill extracted on a previous boot, now revoked (absent from the current manifest).
    stale = skills_dir / "send-email"
    stale.mkdir(parents=True)
    (stale / "SKILL.md").write_text("revoked")

    ctx = Context(
        skills=[ContextItem(name="frontend-design@anthropics-skills", downloadUrl="http://x")]
    )
    await fetch_context(ctx, "ek-test", tmp_path / "state", skills_dir)

    # The current manifest's skill is present; the stale revoked one is gone.
    assert (skills_dir / "frontend-design" / "SKILL.md").is_file()
    assert not (skills_dir / "send-email").exists()


def test_ach_state_dir_under_home(tmp_path):
    assert ach_state_dir(str(tmp_path)) == tmp_path / ".ach-state"


def test_link_ach_state_symlinks_workdir(tmp_path):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    real = link_ach_state(str(home), str(work))
    assert real == home / ".ach-state"
    assert real.is_dir()
    link = work / ".ach-state"
    assert link.is_symlink()
    assert link.resolve() == real.resolve()


def test_link_ach_state_no_symlink_when_workdir_equals_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    real = link_ach_state(str(home), str(home))
    assert real == home / ".ach-state"
    assert not (home / ".ach-state" / ".ach-state").exists()


def test_legacy_workspace_migrates_once_and_preserves_later_changes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    old = home / "workspace"
    target = tmp_path / "workspace"
    old.mkdir(parents=True)
    (old / ".ach-state").symlink_to(home / ".ach-state", target_is_directory=True)
    (old / "repo").mkdir()
    (old / "repo" / "shared-link").symlink_to("../checkout.txt")
    (old / "linked-repo").symlink_to(old / "repo", target_is_directory=True)
    (old / "checkout.txt").write_text("original")

    migrate_legacy_workspace(home, target)
    assert (target / "checkout.txt").read_text() == "original"
    assert (target / "repo" / "shared-link").is_symlink()
    assert (target / "linked-repo").is_symlink()
    assert (home / ".ach-workspace-migrated").is_file()
    (target / "checkout.txt").write_text("new-workspace-state")

    migrate_legacy_workspace(home, target)
    assert (target / "checkout.txt").read_text() == "new-workspace-state"
    assert old.is_dir()


def test_legacy_workspace_rejects_nonempty_unmarked_target(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / "workspace").mkdir(parents=True)
    target = tmp_path / "workspace"
    target.mkdir()
    (target / "existing").write_text("keep")
    with pytest.raises(ValueError, match="nonempty"):
        migrate_legacy_workspace(home, target)
