#!/usr/bin/env python
"""List the files inside a hydrated ACH prompt (to pin prompt.system.file).

Usage (ACH_TOKEN must be in your env — never pass it on the CLI):
    ACH_TOKEN=ek-... uv run python scripts/list-prompt-files.py [PROMPT_NAME]

PROMPT_NAME defaults to the first prompt. Prints each prompt's name and its tar members,
so you can set `prompt.system.file: prompts/<name>/<file>` to a path that actually exists.
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tarfile

from ach_agent.engine.context import _get_bytes
from ach_agent.engine.hydrate import hydrate

BASE = os.environ.get("ACH_BASE_URL", "https://ach.ackstorm.ai")


async def main() -> None:
    ek = os.environ.get("ACH_TOKEN") or os.environ.get("ACH_API_KEY")
    if not ek:
        sys.exit("ACH_TOKEN (ek-...) not set in env")
    want = sys.argv[1] if len(sys.argv) > 1 else None
    manifest = await hydrate(BASE, ek)
    prompts = manifest.context.prompts
    if not prompts:
        sys.exit("hydration returned no prompts")
    for item in prompts:
        if want and item.name != want:
            continue
        data = await _get_bytes(item.download_url, ek)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            files = [m.name for m in tar.getmembers() if m.isfile()]
        print(f"\nprompt: {item.name}")
        # fetch_context extracts into <.ach-state>/prompts/<item.name>/<member>, so the
        # resolvable file: path is prompts/<item.name>/<member> verbatim.
        for f in files:
            print(f"  file: prompts/{item.name}/{f}")


if __name__ == "__main__":
    asyncio.run(main())
