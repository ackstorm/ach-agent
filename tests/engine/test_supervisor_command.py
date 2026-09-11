from __future__ import annotations

import asyncio
import os
import sys

import pytest


def test_supervisor_command_uses_absolute_stdlib_script() -> None:
    from ach_agent.engine.process_supervisor import command

    args = command(["/bin/true"])
    assert args[0] == sys.executable
    assert args[1].endswith("/ach_agent/engine/process_supervisor.py")
    assert "-m" not in args


@pytest.mark.asyncio
async def test_supervisor_script_starts_without_pythonpath() -> None:
    from ach_agent.engine.process_supervisor import command

    env = {"PATH": os.environ.get("PATH", "")}
    proc = await asyncio.create_subprocess_exec(
        *command([sys.executable, "-c", "print('ready')"]),
        cwd="/tmp",
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=3)
    assert proc.returncode == 0, stderr.decode()
    assert stdout == b"ready\n"
