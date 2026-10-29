# SPDX-License-Identifier: MIT
"""SessionRegistry path safety (review finding #13) and hook process-group supervision
(finding #12)."""

from __future__ import annotations

import asyncio

import pytest

from ach_runtime.config.schema import HookBlock
from ach_runtime.execution.sessions import (
    MAX_HOOK_OUTPUT_BYTES,
    SessionHookFailed,
    SessionRegistry,
    redact_facade_tokens,
    run_gate_hook,
    run_hook,
)


def test_traversal_and_absolute_refs_cannot_escape_root(tmp_path) -> None:
    registry = SessionRegistry(tmp_path)
    evil = registry.get_or_create("../../../etc/passwd")
    assert tmp_path in evil.home.parents
    assert evil.home.is_relative_to(tmp_path)

    absolute = registry.get_or_create("/etc/shadow")
    assert absolute.home.is_relative_to(tmp_path)


def test_distinct_refs_never_collide_on_the_same_home(tmp_path) -> None:
    registry = SessionRegistry(tmp_path)
    a = registry.get_or_create("session-a")
    b = registry.get_or_create("session-b")
    assert a.home != b.home


def test_same_ref_always_maps_to_the_same_home(tmp_path) -> None:
    registry = SessionRegistry(tmp_path)
    first = registry.get_or_create("same-session")
    second = registry.get_or_create("same-session")
    assert first.home == second.home


async def test_hook_timeout_kills_the_whole_process_group(tmp_path) -> None:
    marker = tmp_path / "escaped"
    hook = HookBlock(
        script=f"(sleep 5; touch {marker}) & disown; sleep 30",
        timeoutSeconds=1,
    )
    with pytest.raises(SessionHookFailed):
        await run_hook(hook, cwd=tmp_path, env={})
    await asyncio.sleep(1.5)
    assert not marker.exists(), "a backgrounded grandchild must not outlive a rejected hook"


async def test_hook_output_is_bounded(tmp_path) -> None:
    hook = HookBlock(script="yes | head -c 10000000; exit 1", timeoutSeconds=10)
    with pytest.raises(SessionHookFailed) as excinfo:
        await run_hook(hook, cwd=tmp_path, env={})
    assert len(str(excinfo.value)) < 200_000


async def test_gate_hook_returns_exit_code_and_stdout(tmp_path) -> None:
    result = await run_gate_hook(HookBlock(script="echo hi; echo oops >&2; exit 78"), cwd=tmp_path, env={})
    assert (result.returncode, result.stdout, result.stderr) == (78, "hi\n", "oops\n")


async def test_gate_hook_rejects_oversized_stdout(tmp_path) -> None:
    script = f"head -c {MAX_HOOK_OUTPUT_BYTES + 1} /dev/zero | tr '\\0' x"
    with pytest.raises(SessionHookFailed, match="exceeds"):
        await run_gate_hook(HookBlock(script=script), cwd=tmp_path, env={})


async def test_gate_hook_accepts_stdout_at_the_cap(tmp_path) -> None:
    script = f"head -c {MAX_HOOK_OUTPUT_BYTES} /dev/zero | tr '\\0' x"
    result = await run_gate_hook(HookBlock(script=script), cwd=tmp_path, env={})
    assert len(result.stdout) == MAX_HOOK_OUTPUT_BYTES


async def test_gate_hook_timeout_fails(tmp_path) -> None:
    with pytest.raises(SessionHookFailed, match="timed out"):
        await run_gate_hook(HookBlock(script="sleep 30", timeoutSeconds=1), cwd=tmp_path, env={})


async def test_plain_hook_tolerates_large_stdout(tmp_path) -> None:
    script = f"head -c {MAX_HOOK_OUTPUT_BYTES * 2} /dev/zero"
    await run_hook(HookBlock(script=script), cwd=tmp_path, env={})



def test_facade_tokens_are_redacted() -> None:
    text = "GET http://ctl/facades/mcp/t/abc.DEF-123/mcp/srv failed; /t/xyz/"
    assert redact_facade_tokens(text) == (
        "GET http://ctl/facades/mcp/t/<redacted>/mcp/srv failed; /t/<redacted>/"
    )


async def test_hook_failure_message_redacts_facade_tokens(tmp_path) -> None:
    with pytest.raises(SessionHookFailed) as excinfo:
        await run_hook(HookBlock(script="echo 'http://f/mcp/t/secret-token/mcp/x' >&2; exit 2"),
                       cwd=tmp_path, env={})
    assert "secret-token" not in str(excinfo.value)
    assert "hook exited 2" in str(excinfo.value)


async def test_hook_timeout_covers_a_process_that_closed_its_pipes(tmp_path) -> None:
    # Closing stdout/stderr ends the drain early; the process itself must still be bounded.
    started_at = asyncio.get_running_loop().time()
    with pytest.raises(SessionHookFailed, match="timed out"):
        await run_hook(
            HookBlock(script="exec >/dev/null 2>&1; sleep 30", timeoutSeconds=1),
            cwd=tmp_path,
            env={},
        )
    assert asyncio.get_running_loop().time() - started_at < 10
