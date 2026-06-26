import io, tarfile, pytest
from pathlib import Path
from ach_agent.engine.context import fetch_context, _safe_extract
from ach_agent.engine.hydrate import Context, ContextItem

def _make_targz(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as t:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name); info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return buf.getvalue()

async def test_fetch_context_extracts(monkeypatch, tmp_path):
    blob = _make_targz({"SKILL.md": "x"})
    async def fake_get(url, ek): return blob
    monkeypatch.setattr("ach_agent.engine.context._get_bytes", fake_get)
    ctx = Context(skills=[ContextItem(name="fd", id="fd", downloadUrl="https://ach/skill/fd")])
    await fetch_context(ctx, "ek", tmp_path)
    assert (tmp_path / "skills" / "fd" / "SKILL.md").read_text() == "x"

def test_safe_extract_rejects_traversal(tmp_path):
    info = tarfile.TarInfo("../evil")
    with pytest.raises(ValueError):
        _safe_extract([info], tmp_path)
