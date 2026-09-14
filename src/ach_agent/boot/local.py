# SPDX-License-Identifier: Apache-2.0
"""Local role launcher for the split execution path.

The local launcher keeps source adapters in the parent process, but starts the
mini-harness as a real child and talks to it through the same HTTP execution
client used by the separated deployment.  Role artifacts contain only the
allowlisted projections produced by :mod:`ach_agent.boot.roles`.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from ach_agent.engine.lifecycle import ManagedServer


async def wait_engine_ready(
    base_url: str,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
    client: httpx.AsyncClient | None = None,
    socket_path: str | None = None,
) -> dict[str, Any]:
    """Wait for the engine endpoint without requiring a native process."""
    deadline = asyncio.get_running_loop().time() + timeout
    owns_client = client is None
    http = client or httpx.AsyncClient(
        base_url=base_url.rstrip("/"),
        timeout=1.0,
        transport=httpx.AsyncHTTPTransport(uds=socket_path) if socket_path else None,
    )
    try:
        while True:
            try:
                response = await http.get("/execution/v1/health")
                if response.status_code == 200:
                    payload = response.json()
                    if isinstance(payload, dict) and payload.get("instance_id"):
                        return payload
            except (httpx.HTTPError, ValueError):
                pass
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"engine endpoint did not become ready: {base_url}")
            await asyncio.sleep(poll_interval)
    finally:
        if owns_client:
            await http.aclose()


class LocalEngineProcess:
    """Own the local engine-role child and terminate it in a bounded manner."""

    def __init__(
        self,
        process: asyncio.subprocess.Process,
        artifacts: object | None,
        *,
        isolated_process_group: bool,
        process_owner: ManagedServer | None = None,
    ) -> None:
        self.process = process
        self.artifacts = artifacts
        self.isolated_process_group = isolated_process_group
        if (
            process_owner is None
            and sys.platform.startswith("linux")
            and isinstance(process, asyncio.subprocess.Process)
        ):
            from ach_agent.engine.lifecycle import ManagedServer

            process_owner = ManagedServer(port=0)
            process_owner.register_process(process, protect_root=True)
        self.process_owner = process_owner

    @classmethod
    async def start(
        cls,
        artifacts: object | None = None,
        *,
        host: str = "127.0.0.1",
        port: int = 8081,
        env: dict[str, str] | None = None,
        terminal_mode: bool = False,
    ) -> LocalEngineProcess:
        package_root = Path(__file__).resolve().parents[2]
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
            "TERM": os.environ.get("TERM", "dumb"),
            "LANG": os.environ.get("LANG", "C.UTF-8"),
            "LC_ALL": os.environ.get("LC_ALL", ""),
        }
        # Deployment-provided engine-only values are explicit.  The parent’s
        # harness token/configuration is never copied into the child environment.
        for name, value in (env or {}).items():
            child_env[name] = value
        if (package_root / "ach_agent").is_dir():
            child_env["PYTHONPATH"] = str(package_root)
        # Role control values are launcher-owned and cannot be overridden by
        # an operator environment projection.
        use_supervisor = sys.platform.startswith("linux")
        supervisor = Path(__file__).resolve().parents[1] / "engine" / "process_supervisor.py"
        command = (
            [sys.executable, str(supervisor), "--", sys.executable, "-m", "ach_agent.main"]
            if use_supervisor
            else [sys.executable, "-m", "ach_agent.main"]
        )
        command.extend(["--role", "engine"])
        if terminal_mode:
            command.append("--tui")
        process = await asyncio.create_subprocess_exec(
            *command,
            env=child_env,
            start_new_session=not terminal_mode,
        )
        return cls(
            process,
            artifacts,
            isolated_process_group=not terminal_mode,
        )

    async def wait_ready(
        self, base_url: str, *, timeout: float = 30.0, socket_path: str | None = None
    ) -> dict[str, Any]:
        return await wait_engine_ready(base_url, timeout=timeout, socket_path=socket_path)

    async def close(self, *, timeout: float = 20.0) -> None:
        try:
            if self.process_owner is not None:
                await self.process_owner.stop(timeout=timeout)
            elif self.process.returncode is None and self.isolated_process_group:
                os.killpg(self.process.pid, signal.SIGTERM)
            elif self.process.returncode is None:
                self.process.send_signal(signal.SIGTERM)
            if self.process.returncode is None:
                await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except (ProcessLookupError, TimeoutError):
            if self.process.returncode is None:
                if self.isolated_process_group:
                    os.killpg(self.process.pid, signal.SIGKILL)
                else:
                    self.process.kill()
                await self.process.wait()
        finally:
            pass
