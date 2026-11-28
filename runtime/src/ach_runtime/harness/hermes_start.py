# SPDX-License-Identifier: Apache-2.0
"""Runs in a HermesAgent's sandbox at every start, under Hermes' own Python, stdlib only: the
credential proxy ships it in /hermes/bundle as $HERMES_HOME/.ach/start.py.

It puts the ACH Environment's instructions into Hermes' SOUL.md between markers. SOUL.md is the one
context file Hermes loads for chat and cron turns alike and trusts (its injection scanner warns
instead of blocking). The block is ACH's and is rewritten every start; the text around it is Hermes'
or the user's and is kept.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

BEGIN = "<!-- ach:instructions: rewritten at every start, edits inside are lost -->"
END = "<!-- /ach:instructions -->"
_BLOCK = re.compile(r"<!-- ach:instructions[^>]*-->.*?<!-- /ach:instructions -->\n?", re.S)


def merge_soul(soul: str, instructions: str) -> str:
    """`soul` without its ACH block, then the block holding `instructions` when there are any."""
    text = re.sub(r"\n{3,}", "\n\n", _BLOCK.sub("", soul)).strip()
    if not instructions.strip():
        return text + "\n" if text else ""
    return f"{text}\n\n{BEGIN}\n{instructions.strip()}\n{END}\n".lstrip()


def _hermes_default_soul() -> str:
    """Hermes' own first-run SOUL.md, which it seeds only when the file is missing."""
    sys.path.insert(0, "/opt/hermes")
    try:
        from hermes_cli.default_soul import DEFAULT_SOUL_MD  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001 - a Hermes without it keeps only the ACH block
        return ""
    return DEFAULT_SOUL_MD


def main() -> None:
    home = Path(os.environ["HERMES_HOME"])
    soul_path = home / "SOUL.md"
    source = home / ".ach" / "instructions.md"
    instructions = source.read_text(encoding="utf-8") if source.is_file() else ""
    if soul_path.is_file():
        soul = soul_path.read_text(encoding="utf-8")
    elif instructions.strip():
        soul = _hermes_default_soul()
    else:
        return  # nothing to add: Hermes seeds its own SOUL.md
    merged = merge_soul(soul, instructions)
    if merged != soul:
        soul_path.write_text(merged, encoding="utf-8")


if __name__ == "__main__":
    main()
