# SPDX-License-Identifier: Apache-2.0
"""Live checks for the ach-memory backend against a running service.

Unit tests monkeypatch `call_ach_memory`; this drives the real one. Everything here is a
claim about the *service contract* that a mock cannot hold honest: the MCP path, the typed
retain shape, whether the facade's project override actually reaches storage.

    ./bootstrap.sh && uv run python check_memory.py

Reads the endpoint from the host side (127.0.0.1:8000 is in ach-memory's default Host
allowlist), and the identity token from ./.env.

ach-memory mints no credentials: identity is delegated. On the compose stack the
dev-identity sidecar echoes the bearer token back as the user id, so the token IS the
identity and any name is a person with their own bank. In production the same shape is
answered by LiteLLM, or the token is a JWT the service verifies offline.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from ach_agent.config.schema import (  # noqa: E402
    AchMemoryAuthAch,
    AchMemoryAuthBearer,
    AchMemoryMemory,
)
from ach_agent.memory.ach_memory import (  # noqa: E402
    call_ach_memory,
    fetch_context,
    prepare_ach_memory,
    resolve_ach_memory_auth,
    resolve_project,
)
from ach_agent.memory.ach_memory_facade import AchMemoryFacade  # noqa: E402

# Two routes, and the second is the one production will use:
#
#   direct  (default)  ENDPOINT=http://127.0.0.1:8000/mcp/  + a Bearer identity token
#   via ACH            MEMORY_ENDPOINT=https://api.ackstorm.ai/mcp/ach-memory
#                      ACH_TOKEN=ek-...   → sent as `x-ach-key`, principal resolved by
#                      ACH/LiteLLM. No user key, no ./.env needed.
#
# The COMPLETE MCP endpoint either way — the harness appends nothing. ROOT is only for the
# REST calls (bootstrap, foreign-project probe) that the direct route uses.
ROOT = os.environ.get("MEMORY_URL", "http://127.0.0.1:8000")
ENDPOINT = os.environ.get("MEMORY_ENDPOINT", f"{ROOT}/mcp/")
VIA_ACH = bool(os.environ.get("ACH_TOKEN")) and "MEMORY_ENDPOINT" in os.environ
# Which header the token rides — the `auth.header` arm. `Authorization` reaches ach-memory's
# JWT provider, and is also what this compose stack reads because it points its platform
# provider at `authorization`. Set MEMORY_AUTH_HEADER=x-litellm-api-key to drive the other
# arm (and restart the api with MEMORY_AUTH_PLATFORM_INCOMING_HEADER to match).
AUTH_HEADER = os.environ.get("MEMORY_AUTH_HEADER", "Authorization")
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


async def _project_status(headers: dict[str, str], project: str) -> object:
    """`load_context`'s project_status: None (no slug), 'ready', or 'absent'.

    'absent' deliberately collapses "never existed" with "belongs to someone else" — that
    collapse is what makes the field safe to expose at all, so it is the strongest thing a
    facade can learn and still not be an existence oracle.
    """
    raw = await call_ach_memory(ENDPOINT, headers, "load_context", {"project_slug": project})
    return json.loads(raw).get("project_status") if raw else None


async def main() -> int:
    # Through resolve_ach_memory_auth, not hand-built: the header a deployment needs is
    # exactly what the config arm decides, so a check that builds its own would pass while
    # the harness sent something else.
    if VIA_ACH:
        auth: object = AchMemoryAuthAch(type="ach")
        print(f"route: via ACH — {ENDPOINT}, credential is the ek_ as x-ach-key\n")
    else:
        os.environ["ACH_SECRET_MEMORY_ACHMEMORY"] = _secret()
        auth = AchMemoryAuthBearer(
            type="bearer", env="ACH_SECRET_MEMORY_ACHMEMORY", header=AUTH_HEADER
        )
        print(f"route: direct — {ENDPOINT}, identity token on {AUTH_HEADER}\n")
    ok_auth, headers = resolve_ach_memory_auth(auth, os.environ.get("ACH_TOKEN"))
    check(f"auth resolves to exactly one header ({AUTH_HEADER if not VIA_ACH else 'x-ach-key'})",
          ok_auth and len(headers) == 1, ", ".join(headers))

    # Before anything else: is this credential accepted ON THIS HEADER? Naming the wrong one
    # is the whole failure mode `auth.header` exists for, and ach-memory answers it with a
    # flat refusal that says nothing about which header it was looking at. Everything below
    # would then fail for a reason none of it is testing, so stop here and say it once.
    try:
        await _project_status(headers, PROJECT)
        check("the credential is accepted on this header", True)
    except BaseException as exc:
        check("the credential is accepted on this header", False, " | ".join(_leaves(exc))[:200])
        print(f"\n{len(failures)} failed")
        return 1
    facade = AchMemoryFacade(ENDPOINT, headers, PROJECT)

    # 0. COLD START — the acceptance test for the whole backend, and the one check that
    # must never be made to pass by a setup step. Nothing bootstraps this project: an agent
    # with an identity nobody has seen, naming a slug nobody has ever named, must end up
    # with working memory on its own. `retain` is the one place allowed to mint a project;
    # every read reports an absent one as empty so the agent's FIRST call (always a read,
    # never a write) does not teach it that memory is broken.
    #
    # A fresh identity per run on the direct route, so the per-user hourly project-creation
    # ceiling (10) is never the reason a rerun fails. Through ACH the principal is the ek_,
    # so reruns do spend that budget.
    stamp = f"{int(time.time())}-{os.getpid()}"
    cold_project = f"testbed-cold-{stamp}"
    cold_token = f"testbed-cold-{stamp}"
    cold_headers = headers if VIA_ACH else {
        AUTH_HEADER: f"Bearer {cold_token}" if AUTH_HEADER.lower() == "authorization" else cold_token
    }

    check("cold start: an unknown project reads as absent, not as an error",
          await _project_status(cold_headers, cold_project) == "absent")
    cold_section = await fetch_context(ENDPOINT, cold_headers, cold_project)
    check("cold start: the harness still gets a usable ## Memory section",
          cold_section.startswith("## Memory") and "Unavailable" not in cold_section,
          cold_section[:70].replace("\n", " "))
    cold_written = await AchMemoryFacade(ENDPOINT, cold_headers, cold_project)._invoke("retain", {
        "content": "The testbed provisions nothing: the first retain is what creates the bank.",
        "memory_type": "fact",
        "basis": "agent_verified",
        "trigger": "agent_proactive",
        "evidence": [{"kind": "artifact_excerpt", "raw": "cold start", "source_ref": "testbed"}],
    })
    check("cold start: the first retain is accepted", "unavailable" not in cold_written.lower(),
          cold_written[:120])
    check("cold start: the project is ready afterwards, with no manual step",
          await _project_status(cold_headers, cold_project) == "ready")

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
    # A read against a foreign or absent project no longer raises — it returns the tool's
    # own empty shape, deliberately identical in both cases so no read is an existence
    # oracle. So the proof is the ABSENCE of the probe text, not an error code.
    try:
        foreign = await call_ach_memory(ENDPOINT, headers, "recall",
                                        {"scope": "project", "project_slug": FOREIGN, "query": "containment probe"})
    except BaseException as exc:
        foreign = " | ".join(_leaves(exc))
    check("nothing landed in the foreign project",
          "containment probe" not in foreign, foreign[:120].replace("\n", " "))

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
