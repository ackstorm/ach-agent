import io
import tarfile
from pathlib import Path

import pytest

from ach_agent.engine.context import _safe_extract, fetch_context
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


def test_safe_extract_rejects_traversal(tmp_path: Path) -> None:
    """_safe_extract rejects a tar member that escapes the destination dir."""
    info = tarfile.TarInfo("../evil")
    with pytest.raises(ValueError):
        _safe_extract([info], tmp_path)
