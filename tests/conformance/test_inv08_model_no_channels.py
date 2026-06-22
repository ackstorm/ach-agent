"""CONTRACT §6.8: Model never talks to channels invariant (authoritative conformance test).

Invariant: model never talks to channels — adapters execute only accepted,
validated actions. The seam import boundary prevents engine ↔ channel coupling.
"""
from __future__ import annotations

import re
from pathlib import Path


def test_inv08_model_no_channels() -> None:
    """§6.8: model never talks to channels (seam import boundary) — authoritative conformance.

    CONTRACT perspective: no module under engine/ imports from channels/, and
    no module under channels/ imports from engine/. The named seam (channels/seam.py)
    is the only allowed cross-boundary reference — and only channels import it,
    not engine. This import boundary prevents engine ↔ channel coupling (§15).
    """
    src_root = Path(__file__).parent.parent.parent / "src" / "ach_agent"

    # Match actual import statements, not docstring mentions.
    _import_pattern = re.compile(
        r"^\s*(?:import\s+ach_agent\.(\w+)|from\s+ach_agent\.(\w+))", re.MULTILINE
    )

    def _check_no_cross_import(
        source_dir: Path,
        forbidden_submodule: str,
        *,
        exemption_file: str | None = None,
    ) -> list[str]:
        """Return list of files under source_dir that import forbidden_submodule."""
        violations = []
        for py_file in source_dir.rglob("*.py"):
            if exemption_file and py_file.name == exemption_file:
                continue
            source = py_file.read_text(encoding="utf-8")
            for match in _import_pattern.finditer(source):
                imported = match.group(1) or match.group(2)
                if imported == forbidden_submodule:
                    violations.append(str(py_file.relative_to(src_root.parent.parent)))
        return violations

    # engine must not import channels (model must never talk to channels).
    engine_imports_channels = _check_no_cross_import(
        src_root / "engine",
        "channels",
    )
    assert not engine_imports_channels, (
        "§6.8 violated — engine modules import channels (model must never talk to channels):\n"
        + "\n".join(f"  {v}" for v in engine_imports_channels)
    )

    # channels must not import engine (channels dispatch via the named seam, not engine directly).
    channels_imports_engine = _check_no_cross_import(
        src_root / "channels",
        "engine",
    )
    assert not channels_imports_engine, (
        "§6.8 violated — channel modules import engine (seam boundary violated):\n"
        + "\n".join(f"  {v}" for v in channels_imports_engine)
    )
