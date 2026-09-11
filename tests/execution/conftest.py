from __future__ import annotations

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
        self.server = FakeServer()
        self.resolved_conversations: list[tuple[str, bool]] = []
        self.turn_session_refs: list[str | None] = []
        self.turn_barrier = None
        self.resolve_barrier = None
        self.compact_barrier = None
        self.run_error = None
        self.stop_error = None
        self.stopped = False

    def skills_dir(self, home):
        return home

    async def launch(self, cfg, session_key):
        return self.server

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
        barrier = self.turn_barrier
        if barrier is not None:
            await barrier.wait()
        if self.run_error is not None:
            raise self.run_error
        kwargs["on_text"]("reply")
        return TurnResult(
            text='{"action":"none","text":"reply"}', session_ref=kwargs["session_ref"]
        )

    async def discard_session(self, server, session_ref):
        self.discarded = session_ref

    async def compact_session(self, server, session_ref):
        if self.compact_barrier is not None:
            await self.compact_barrier.wait()
        self.compacted = session_ref

    async def stop(self, server):
        if self.stop_error is not None:
            raise self.stop_error
        server.stopped = True
        self.stopped = True


@pytest.fixture
def fake_driver():
    return FakeDriver()
