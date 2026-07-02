#!/usr/bin/env python
"""Generate the frozen JSON Schema v1 for the rendered runtime config (CONTRACT §2).

The Pydantic ``AgentConfig`` is the SINGLE source of truth; this script renders it to a
canonical JSON Schema so external tooling (the ach-runtime operator, editors, CI) can
validate a contract WITHOUT importing Python. The committed artifact is kept honest by
``tests/config/test_schema_artifact.py`` (drift guard) — never hand-edit the .json.

Usage:
    uv run python scripts/gen_schema.py            # rewrite the artifact in place
    uv run python scripts/gen_schema.py --check     # exit 1 if the artifact is stale (CI)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from ach_agent.config.schema import AgentConfig

# Committed artifact path (repo-relative). Lives UNDER docs/ (mkdocs docs_dir) so it is both the
# machine-readable half of the frozen §2 contract AND published to the docs site (gh-pages) at the
# $id URL below — the ach-runtime operator and config authors consume it there. CONTRACT_v3.md
# itself is the internal design record under the git-ignored docs/plan/.
ARTIFACT = (
    Path(__file__).resolve().parent.parent / "docs" / "schemas" / "agent-config-v1.schema.json"
)

# JSON Schema dialect Pydantic v2 emits ($defs + prefixItems). Pin it explicitly so the
# artifact self-declares its dialect for validators that key off $schema.
_DIALECT = "https://json-schema.org/draft/2020-12/schema"
# Canonical identifier = the published gh-pages URL (mike "stable" = latest tagged release). The
# file publishes to <version>/schemas/... ; "stable" is the release channel. Versioning is in the
# filename (-v1), so a future v2 is a new file, not a new mike path.
_SCHEMA_ID = "https://ackstorm.github.io/ach-agent/stable/schemas/agent-config-v1.schema.json"


def build_schema() -> dict[str, Any]:
    """Render AgentConfig to a canonical JSON Schema dict (camelCase keys via by_alias)."""
    schema = AgentConfig.model_json_schema(by_alias=True)
    # Prepend dialect + id so the artifact is a self-describing JSON Schema document.
    return {"$schema": _DIALECT, "$id": _SCHEMA_ID, **schema}


def render() -> str:
    """Canonical serialization: sorted keys + 2-space indent + trailing newline (diff-stable)."""
    return json.dumps(build_schema(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = ARTIFACT.read_text(encoding="utf-8") if ARTIFACT.exists() else ""
        if current != text:
            print(
                f"STALE: {ARTIFACT} is out of sync with AgentConfig.\n"
                "Regenerate with: uv run python scripts/gen_schema.py",
                file=sys.stderr,
            )
            return 1
        print(f"OK: {ARTIFACT} matches AgentConfig")
        return 0
    ARTIFACT.write_text(text, encoding="utf-8")
    print(f"wrote {ARTIFACT} ({len(text)} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
