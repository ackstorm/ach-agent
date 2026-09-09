# SPDX-License-Identifier: Apache-2.0
"""Live checks for the ach-memory backend against a running service.

Unit tests monkeypatch `call_ach_memory`; this drives the real one. Everything here is a
claim about the *service contract* that a mock cannot hold honest: the MCP path, the typed
retain shape, whether the facade's project override actually reaches storage.

    ./bootstrap.sh && uv run python check_memory.py

Reads the endpoint from the host side (127.0.0.1:8000 is in ach-memory's default Host
allowlist), and the minted user key from ./.env.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ach_agent.config.schema import AchMemoryMemory  # noqa: E402
from ach_agent.memory.ach_memory import (  # noqa: E402
    call_ach_memory,
    fetch_context,
    prepare_ach_memory,
    resolve_project,
)
from ach_agent.memory.ach_memory_facade import AchMemoryFacade  # noqa: E402

# The COMPLETE MCP endpoint — the harness appends nothing. REST calls use the root below.
ROOT = os.environ.get("MEMORY_URL", "http://127.0.0.1:8000")
ENDPOINT = f"{ROOT}/mcp/"
PROJECT = "testbed-memory-probe"  # what resolve_project() derives in the container
FOREIGN = "someone-else"

failures: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {name}{f' — {detail}' if detail else ''}")
    if not ok:
        failures.append(name)


def _cfg(endpoint: str) -> AchMemoryMemory:
    return AchMemoryMemory.model_validate({"type": "ach-memory", "achMemory": {"endpoint": endpoint}})


def _leaves(exc: BaseException) -> list[str]:
    """Flatten an anyio ExceptionGroup — the real cause is always a leaf, and the group's
    own str() is the useless 'unhandled errors in a TaskGroup'."""
    subs = getattr(exc, "exceptions", None)
    if not subs:
        return [f"{type(exc).__name__}: {exc}"]
    return [msg for sub in subs for msg in _leaves(sub)]


def _secret() -> str:
    for line in Path(__file__).with_name(".env").read_text().splitlines():
        if line.startswith("ACH_SECRET_MEMORY_ACHMEMORY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no ACH_SECRET_MEMORY_ACHMEMORY in ./.env — run ./bootstrap.sh")


async def main() -> int:
    secret = _secret()
    headers = {"Authorization": f"Bearer {secret}"}
    facade = AchMemoryFacade(ENDPOINT, headers, PROJECT)

    # 1. reachability, via the call the boot path actually makes. There is no health probe
    # any more: load_context IS the test, so a dead endpoint degrades exactly like an outage.
    dead = await prepare_ach_memory(
        _cfg("http://127.0.0.1:1/mcp/"), PROJECT, {}
    )
    check("a dead endpoint is fail-open, not an exception", dead[0] is False and "Unavailable" in dead[1])

    # 2. project derivation — the container sets POD_NAMESPACE=testbed
    os.environ["POD_NAMESPACE"] = "testbed"
    derived = resolve_project(type("P", (), {"project": ""})(), "memory-probe")
    check("project slug derivation", derived == PROJECT, derived)

    # 3. the exposed surface: five tools, no scope, no project_slug
    tools = {t.name: t for t in await facade._mcp.list_tools()}
    check("exactly five agent-facing tools", len(tools) == 5, ", ".join(sorted(tools)))
    leak = [n for n, t in tools.items() if {"scope", "project_slug"} & set(t.inputSchema.get("properties", {}))]
    check("no tool exposes scope/project_slug", not leak, str(leak))

    # 4. a real typed retain over the wire — the shape the service actually enforces
    marker = "EdDSA is the only accepted signature algorithm in the testbed auth module"
    retained = await facade._invoke("retain", {
        "content": marker,
        "memory_type": "convention",
        "basis": "agent_verified",
        "trigger": "user_requested",
        "evidence": [{"kind": "artifact_excerpt", "raw": marker, "source_ref": "testbed"}],
    })
    check("live retain accepted", "unavailable" not in retained.lower(), retained[:160])

    # 5. containment: the agent naming another project must NOT win
    stolen = await facade._invoke("retain", {
        "content": "containment probe — must not land in " + FOREIGN,
        "memory_type": "convention",
        "basis": "agent_verified",
        "trigger": "user_requested",
        "evidence": [{"kind": "artifact_excerpt", "raw": "probe", "source_ref": "testbed"}],
        "project_slug": FOREIGN,
        "scope": "user",
    })
    check("override survives a hostile project_slug", "unavailable" not in stolen.lower(), stolen[:160])
    try:
        foreign = await call_ach_memory(ENDPOINT, headers, "recall",
                                        {"scope": "project", "project_slug": FOREIGN, "query": "containment probe"})
    except BaseException as exc:  # PROJECT_NOT_FOUND is the strongest possible proof
        foreign = " | ".join(_leaves(exc))
    check("nothing landed in the foreign project",
          "containment probe" not in foreign or "PROJECT_NOT_FOUND" in foreign,
          "PROJECT_NOT_FOUND" if "PROJECT_NOT_FOUND" in foreign else foreign[:120])

    # 6. recall reads back what retain wrote — bounded, because retain is NOT read-your-writes:
    # the service extracts facts asynchronously, so an immediate recall legitimately misses.
    deadline, got = asyncio.get_event_loop().time() + 20, ""
    while asyncio.get_event_loop().time() < deadline:
        got = await facade._invoke("recall", {"query": "signature algorithm"})
        if "EdDSA" in got:
            break
        await asyncio.sleep(2)
    check("live recall returns the retained claim (within 20s)", "EdDSA" in got, got[:80].replace("\n", " "))

    # 7. the boot path end to end
    ok, note = await prepare_ach_memory(_cfg(ENDPOINT), PROJECT, headers)
    check("prepare_ach_memory loads context without a probe", ok is True, note[:80].replace("\n", " "))
    ctx = await fetch_context(ENDPOINT, headers, PROJECT)
    check("load_context returns a ## Memory block", ctx.startswith("## Memory"), ctx[:120].replace("\n", " "))

    print()
    print(f"{len(failures)} failed" if failures else "all checks passed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
