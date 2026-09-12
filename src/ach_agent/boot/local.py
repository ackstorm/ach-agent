# SPDX-License-Identifier: Apache-2.0
"""Local role launcher for the split execution path.

The local launcher keeps source adapters in the parent process, but starts the
mini-harness as a real child and talks to it through the same HTTP execution
client used by the separated deployment.  Role artifacts contain only the
allowlisted projections produced by :mod:`ach_agent.boot.roles`.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from pydantic import JsonValue


@dataclass(frozen=True, slots=True)
class RoleArtifactPaths:
    channels: Path
    engine: Path


class RoleArtifacts:
    """Write role projections into a private, task-owned artifact directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _write(self, name: str, value: dict[str, JsonValue]) -> Path:
        target = self.root / name
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{name}.", dir=self.root)
        temporary_path = Path(temporary)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(encoded)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, target)
            target.chmod(0o600)
        finally:
            temporary_path.unlink(missing_ok=True)
        return target

    def write(
        self,
        channels: dict[str, JsonValue],
        engine: dict[str, JsonValue],
    ) -> RoleArtifactPaths:
        return RoleArtifactPaths(
            channels=self._write("channels.json", channels),
            engine=self._write("engine.json", engine),
        )


def load_artifact(path: str | Path) -> dict[str, JsonValue]:
    """Load one role artifact and reject non-object JSON at the process boundary."""
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"role artifact must contain a JSON object: {path}")
    return value


async def wait_engine_ready(
    base_url: str,
    *,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Wait for the engine endpoint without requiring a native process."""
    deadline = asyncio.get_running_loop().time() + timeout
    owns_client = client is None
    http = client or httpx.AsyncClient(base_url=base_url.rstrip("/"), timeout=1.0)
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

    def __init__(self, process: asyncio.subprocess.Process, artifacts: RoleArtifactPaths) -> None:
        self.process = process
        self.artifacts = artifacts

    @classmethod
    async def start(
        cls,
        artifacts: RoleArtifactPaths,
        *,
        host: str = "127.0.0.1",
        port: int = 8081,
        env: dict[str, str] | None = None,
    ) -> LocalEngineProcess:
        package_root = Path(__file__).resolve().parents[2]
        child_env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONUNBUFFERED": "1",
        }
        # Deployment-provided engine-only values are explicit.  The parent’s
        # harness token/configuration is never copied into the child environment.
        for name, value in (env or {}).items():
            child_env[name] = value
        if (package_root / "ach_agent").is_dir():
            child_env["PYTHONPATH"] = str(package_root)
        # Role control values are launcher-owned and cannot be overridden by
        # an operator environment projection.
        child_env.update(
            {
                "ACH_ENGINE_HOST": host,
                "ACH_ENGINE_PORT": str(port),
                "ACH_ENGINE_CONFIG_PATH": str(artifacts.engine),
            }
        )
        supervisor = Path(__file__).resolve().parents[1] / "engine" / "process_supervisor.py"
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(supervisor),
            "--",
            sys.executable,
            "-m",
            "ach_agent.main",
            "--role",
            "engine",
            env=child_env,
            start_new_session=True,
        )
        return cls(process, artifacts)

    async def wait_ready(self, base_url: str, *, timeout: float = 30.0) -> dict[str, Any]:
        return await wait_engine_ready(base_url, timeout=timeout)

    async def close(self, *, timeout: float = 10.0) -> None:
        if self.process.returncode is not None:
            return
        try:
            self.process.send_signal(signal.SIGTERM)
            await asyncio.wait_for(self.process.wait(), timeout=timeout)
        except (ProcessLookupError, TimeoutError):
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
        shutil.rmtree(self.artifacts.channels.parent, ignore_errors=True)
