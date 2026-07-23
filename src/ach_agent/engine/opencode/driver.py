# SPDX-License-Identifier: Apache-2.0
"""OpencodeDriver — the opencode implementation of EngineDriver (SP1 §4).

launch/health/discard/compact/stop delegate to the existing lifecycle helpers; run_turn is
the session-select + SSE-consume half of the old run_invocation, returning a TurnResult. The
terminal contract (extract/repair/wrap-up) lives once in engine/base/terminal.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any
from collections.abc import Callable, MutableMapping

import aiohttp

from ach_agent.engine.base.driver import EngineConfig, TurnResult
from ach_agent.engine.opencode.client import OpenCodeClient

if TYPE_CHECKING:
    from ach_agent.engine.base.events import OpenCodeToolUpdate
    from ach_agent.engine.lifecycle import ManagedServer


class OpencodeDriver:
    engine_type = "opencode"

    def skills_dir(self, home: Path) -> Path:
        # Opencode scans <home>/.config/opencode/skills (see engine/context.fetch_context).
        return home / ".config" / "opencode" / "skills"

    async def launch(self, cfg: EngineConfig, session_key: str) -> ManagedServer:
        import ach_agent.engine.lifecycle as oc
        from ach_agent.engine.opencode.client import find_free_port

        home = Path(cfg.home)
        home.mkdir(parents=True, exist_ok=True)
        port = find_free_port()
        server = await oc.launch(port, home, cfg, session_key)
        await oc.poll_ready(server, cfg.startup_timeout_seconds)
        return server

    async def health(self, server: ManagedServer) -> bool:
        client = server._client
        if isinstance(client, OpenCodeClient):
            try:
                return bool(await client.check_health())
            except Exception:  # noqa: BLE001
                return server.is_alive()
        return server.is_alive()

    async def run_turn(
        self,
        server: ManagedServer,
        *,
        conv_key: str,
        prompt: str,
        reuse: bool,
        sessions: MutableMapping[str, str],
        session_ref: str | None = None,
        on_text: Callable[[str], None] | None = None,
        on_tool: Callable[[OpenCodeToolUpdate], None] | None = None,
        max_tool_calls: int = 0,
        stats: dict[str, Any] | None = None,
    ) -> TurnResult:
        import ach_agent.engine.lifecycle as oc  # namespace call keeps test patch targets valid

        client = server._client
        if not isinstance(client, OpenCodeClient):
            raise RuntimeError("ManagedServer has no client")
        stats = stats if stats is not None else {}

        async def _consume(oc_session_id: str) -> str:
            return await oc.consume_sse_after_send(
                client, oc_session_id, prompt,
                on_text=on_text, on_tool=on_tool, is_alive=server.is_alive,
                max_tool_calls=max_tool_calls, stats=stats,
            )

        # Repair/wrap-up: continue EXACTLY the given session; bypass the map + reuse (§4.3).
        if session_ref is not None:
            stats["session_ref"] = session_ref
            stats["oc_session_id"] = session_ref
            text = await _consume(session_ref)
            return TurnResult(text=text, session_ref=session_ref, aborted=bool(stats.get("aborted")))

        # First send: resolve conv_key → oc session id (create/reuse), 404-recreate retry.
        reused = False
        if reuse:
            cached = sessions.get(conv_key)
            if cached is None:
                oc_session_id = await oc._create_oc_session(client)
                sessions[conv_key] = oc_session_id
            else:
                oc_session_id, reused = cached, True
        else:
            oc_session_id = await oc._create_oc_session(client)
        stats["session_ref"] = oc_session_id
        stats["oc_session_id"] = oc_session_id
        try:
            text = await _consume(oc_session_id)
        except aiohttp.ClientResponseError as exc:
            if not (reused and exc.status == 404):
                raise
            oc_session_id = await oc._create_oc_session(client)
            sessions[conv_key] = oc_session_id
            stats["session_ref"] = oc_session_id
            stats["oc_session_id"] = oc_session_id
            text = await _consume(oc_session_id)
        return TurnResult(text=text, session_ref=oc_session_id, aborted=bool(stats.get("aborted")))

    async def discard_session(self, server: ManagedServer, session_ref: str) -> None:
        import ach_agent.engine.lifecycle as oc

        await oc.discard_oc_session(server, session_ref)

    async def compact_session(self, server: ManagedServer, session_ref: str) -> None:
        import ach_agent.engine.lifecycle as oc

        await oc.compact_oc_session(server, session_ref)

    async def stop(self, server: ManagedServer) -> None:
        await server.stop()
