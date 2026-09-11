from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from ach_agent.engine.base.driver import TurnResult


@dataclass
class FakeServer:
    proxy_token: str = ""
    stopped: bool = False

    def is_alive(self) -> bool:
        return not self.stopped


class FakeDriver:
    engine_type = "opencode"

    def __init__(self) -> None:
        self.servers: list[FakeServer] = []
        self.resolved_conversations: list[tuple[str, bool]] = []
        self.turn_session_refs: list[str | None] = []
        self.turn_barrier = None
        self.turn_barriers_by_conversation: dict[str, asyncio.Event] = {}
        self.resolve_barrier = None
        self.compact_barrier = None
        self.compact_started = asyncio.Event()
        self.compact_cancelled = asyncio.Event()
        self.run_error = None
        self.stop_error = None
        self.stop_started = asyncio.Event()
        self.stop_barrier = None
        self.suppress_stop_cancellation = False
        self.stopped = False
        self.stopped_servers: list[FakeServer] = []
        self.usage = None
        self.text_chunks: list[str] = []
        self.text_chunks_by_conversation: dict[str, list[str]] = {}
        self.yield_between_text_chunks = False

    def skills_dir(self, home):
        return home

    async def launch(self, cfg, session_key):
        server = FakeServer(proxy_token=f"proxy-{len(self.servers)}")
        self.servers.append(server)
        return server

    async def health(self, server):
        return server.is_alive()

    async def resolve_session(self, server, *, conv_key, reuse, sessions, stats):
        if self.resolve_barrier is not None:
            await self.resolve_barrier.wait()
        self.resolved_conversations.append((conv_key, reuse))
        ref = sessions.get(conv_key) or "native-ref"
        sessions[conv_key] = ref
        stats["session_ref"] = ref
        return ref

    async def run_turn(self, server, **kwargs):
        self.turn_session_refs.append(kwargs["session_ref"])
        if self.usage is not None:
            kwargs["stats"]["usage"] = self.usage
        barrier = self.turn_barriers_by_conversation.get(kwargs["conv_key"], self.turn_barrier)
        if barrier is not None:
            await barrier.wait()
        if self.run_error is not None:
            raise self.run_error
        chunks = self.text_chunks_by_conversation.get(kwargs["conv_key"], self.text_chunks)
        for text in chunks or ["reply"]:
            kwargs["on_text"](text)
            if self.yield_between_text_chunks:
                await asyncio.sleep(0)
        return TurnResult(
            text='{"action":"none","text":"reply"}', session_ref=kwargs["session_ref"]
        )

    async def discard_session(self, server, session_ref):
        self.discarded = session_ref

    async def compact_session(self, server, session_ref):
        self.compact_started.set()
        if self.compact_barrier is not None:
            try:
                await self.compact_barrier.wait()
            except asyncio.CancelledError:
                self.compact_cancelled.set()
                raise
        self.compacted = session_ref

    async def stop(self, server):
        self.stop_started.set()
        if self.stop_error is not None:
            raise self.stop_error
        if self.stop_barrier is not None:
            if self.suppress_stop_cancellation:
                while not self.stop_barrier.is_set():
                    try:
                        await self.stop_barrier.wait()
                    except asyncio.CancelledError:
                        continue
            else:
                await self.stop_barrier.wait()
        server.stopped = True
        self.stopped_servers.append(server)
        self.stopped = True


@pytest.fixture
def fake_driver():
    return FakeDriver()
