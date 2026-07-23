# SPDX-License-Identifier: Apache-2.0
"""Pi implementation of the EngineDriver protocol."""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.engine.base.driver import EngineConfig, TurnResult
from ach_agent.engine.pi import events as pe
from ach_agent.engine.pi.config import build_pi_env, build_pi_settings
from ach_agent.engine.pi.mcp_json import build_mcp_json
from ach_agent.engine.pi.models_json import build_models_json
from ach_agent.engine.pi.protocol import (
    CMD_ABORT,
    CMD_GET_STATE,
    CMD_NEW_SESSION,
    CMD_PROMPT,
    CMD_SWITCH_SESSION,
    EV_AGENT_END,
    EV_EOF,
    EV_SESSION_CREATED,
    F_SESSION_PATH,
)
from ach_agent.engine.pi.rpc import PiRpcClient, PiRpcError

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer

log = structlog.get_logger(__name__)
_DEFAULT_PI_MCP_ADAPTER = "/opt/pi-mcp-adapter"


class PiDriver:
    engine_type = "pi"

    def skills_dir(self, home: Path) -> Path:
        return home / "pi" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        from ach_agent.engine.lifecycle import ManagedServer, _key_suffix

        agent_dir = Path(cfg.home) / "pi" / _key_suffix(session_key)
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "sessions").mkdir(exist_ok=True)

        models_doc, provider = build_models_json(cfg)
        adapter_path = cfg.pi_mcp_adapter_path or _DEFAULT_PI_MCP_ADAPTER
        settings_doc = build_pi_settings(self.skills_dir(Path(cfg.home)), adapter_path)
        mcp_doc = build_mcp_json(cfg)
        (agent_dir / "models.json").write_text(json.dumps(models_doc, indent=2), encoding="utf-8")
        (agent_dir / "settings.json").write_text(
            json.dumps(settings_doc, indent=2), encoding="utf-8"
        )
        (agent_dir / "mcp.json").write_text(json.dumps(mcp_doc, indent=2), encoding="utf-8")

        binary = shutil.which(cfg.binary_path)
        if not binary:
            raise RuntimeError(f"pi binary not found: {cfg.binary_path!r}")
        work_dir = Path(cfg.work_dir)
        work_dir.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--mode",
            "rpc",
            "--provider",
            provider,
            "--model",
            cfg.model,
            "--session-dir",
            str(agent_dir / "sessions"),
            cwd=str(work_dir),
            env=build_pi_env(agent_dir, cfg),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        server = ManagedServer(port=0, ephemeral_home=agent_dir)
        server._process = proc
        server._client = PiRpcClient(proc)
        asyncio.create_task(self._drain_stderr(proc, server))
        await asyncio.sleep(0)
        if proc.returncode is not None:
            raise RuntimeError(f"pi exited immediately (rc={proc.returncode})")
        return server

    @staticmethod
    async def _drain_stderr(proc: Any, server: ManagedServer) -> None:
        stderr = proc.stderr
        if stderr is None:
            return
        with contextlib.suppress(Exception):
            while True:
                line = await stderr.readline()
                if not line:
                    return
                server._stderr_tail.append(line.decode("utf-8", "replace").rstrip("\n"))

    async def health(self, server: ManagedServer) -> bool:
        return server.is_alive()

    async def _new_session(self, client: Any) -> str:
        await client.send({"type": CMD_NEW_SESSION})
        while True:
            event = await client.recv()
            if event.get("type") == EV_EOF:
                raise PiRpcError("pi ended before session_created")
            if event.get("type") == EV_SESSION_CREATED:
                return str(event.get(F_SESSION_PATH, "") or "")
            if event.get("type") == "response" and event.get("command") == CMD_NEW_SESSION:
                await client.send({"type": CMD_GET_STATE})
                continue
            if event.get("type") == "response" and event.get("command") == CMD_GET_STATE:
                data = event.get("data") or {}
                session_path = data.get("sessionFile") if isinstance(data, dict) else None
                if session_path:
                    return str(session_path)

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],
        session_ref: str | None = None,
        on_text: Callable[[str], None] | None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None,
        max_tool_calls: int,
        stats: dict[str, Any],
    ) -> TurnResult:
        client: Any = server._client

        try:
            if session_ref is not None:
                ref = session_ref
                await client.send({"type": CMD_SWITCH_SESSION, F_SESSION_PATH: ref})
            elif reuse:
                cached = sessions.get(conv_key)
                if cached is None:
                    ref = await self._new_session(client)
                    sessions[conv_key] = ref
                else:
                    ref = cached
                    await client.send({"type": CMD_SWITCH_SESSION, F_SESSION_PATH: ref})
            else:
                ref = await self._new_session(client)
            await client.send({"type": CMD_PROMPT, "message": prompt})
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(client.send({"type": CMD_ABORT}))
            raise
        stats["session_ref"] = ref

        text_parts: list[str] = []
        tool_ids: set[str] = set()
        aborted = False
        try:
            while True:
                event = await client.recv()
                if event.get("type") == EV_EOF:
                    raise PiRpcError("pi ended before agent_settled")
                delta = pe.pi_text_delta(event)
                if delta:
                    text_parts.append(delta)
                    if on_text is not None:
                        on_text(delta)
                    continue
                tool_update = pe.pi_tool_update(event, ref)
                if tool_update is not None:
                    if on_tool is not None:
                        on_tool(tool_update)
                    if tool_update.state.status == "running":
                        tool_ids.add(tool_update.call_id)
                        if max_tool_calls > 0 and not aborted and len(tool_ids) >= max_tool_calls:
                            aborted = True
                            log.warning("pi: max_tool_calls reached", limit=max_tool_calls)
                            await client.send({"type": CMD_ABORT})
                    continue
                usage = pe.pi_usage(event, ref)
                if usage is not None:
                    stats["usage"] = usage
                    continue
                if pe.is_settled(event) or event.get("type") == EV_AGENT_END:
                    break
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await asyncio.shield(client.send({"type": CMD_ABORT}))
            raise

        stats["aborted"] = aborted
        return TurnResult(text="".join(text_parts), session_ref=ref, aborted=aborted)

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        try:
            Path(session_ref).unlink(missing_ok=True)
        except OSError:
            log.warning("pi: session file delete failed", session_ref=session_ref, exc_info=True)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        log.info(
            "pi: compact is a no-op (automatic); rely on Pi's built-in compaction",
            session_ref=session_ref,
        )

    async def stop(self, server: ManagedServer) -> None:
        await server.stop()
