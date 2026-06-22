"""Seam discipline tests — RTR-06 import-boundary enforcement.

Verifies that no module under src/ach_agent/{router,channels,actions,engine}/
imports hermes_agent at module-level (direct or transitive text scan).
"""
from __future__ import annotations

from pathlib import Path


def test_no_hermes_import_in_router() -> None:
    """RTR-06: router/channels/actions/engine must never import hermes_agent.

    Walks every .py file under the four module directories and asserts none
    contains 'import hermes_agent' or 'from hermes_agent'.
    """
    src_root = Path(__file__).parent.parent.parent / "src" / "ach_agent"
    dirs_to_check = [
        src_root / "router",
        src_root / "channels",
        src_root / "actions",
        src_root / "engine",
    ]

    import re

    # Match actual import statements, not docstring mentions of the constraint
    _import_pattern = re.compile(
        r"^\s*(?:import\s+hermes_agent|from\s+hermes_agent)", re.MULTILINE
    )

    violations: list[str] = []
    for directory in dirs_to_check:
        for py_file in directory.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            if _import_pattern.search(source):
                violations.append(str(py_file.relative_to(src_root.parent.parent)))

    assert not violations, (
        "RTR-06 violated — hermes_agent imported in seam-restricted modules:\n"
        + "\n".join(f"  {v}" for v in violations)
    )
