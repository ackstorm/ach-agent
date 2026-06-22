"""Shared fixtures for router conformance tests.

Provides:
  - make_event(): synthetic MessageEvent builder (analog: FakeSlotManager pattern)
  - FakeEngine: records invocations, controls timing (analog: FakeSlotManager)
  - fake_engine fixture
  - router fixture: Router with FakeEngine + InMemoryDedupStore (TODO: 01-03 finalizes)
  - fake_ek_env: injects test ek_ key + ACH_BASE_URL into env

Constraint: NEVER import from hermes_agent.* here (RTR-06).
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router import Router
from ach_agent.router.dedup import InMemoryDedupStore


def make_event(
    idempotency_key: str = "test-key",
    session_key: str = "test-session",
    channel_name: str = "test-channel",
    payload: dict[str, Any] | None = None,
    source_trait: str = "async_no_retry",
) -> MessageEvent:
    """Synthetic MessageEvent builder for router conformance tests."""
    return MessageEvent(
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel_name=channel_name,
        payload=payload or {},
        source_trait=source_trait,  # type: ignore[arg-type]
    )


class FakeEngine:
    """Records invocations; controls timing via asyncio.Event.

    Used by router conformance tests to control when invocations complete.
    hold() blocks the next invocation; release() lets it proceed.
    """

    def __init__(self) -> None:
        self.invocations: list[MessageEvent] = []
        self._hold = asyncio.Event()
        self._hold.set()  # not held by default

    async def run(
        self, event: MessageEvent, on_kill: Callable[[], None]
    ) -> None:
        self.invocations.append(event)
        await self._hold.wait()
        on_kill()

    def hold(self) -> None:
        """Block the next invocation until release() is called."""
        self._hold.clear()

    def release(self) -> None:
        """Unblock a held invocation."""
        self._hold.set()


@pytest.fixture()
def fake_engine() -> FakeEngine:
    """Fresh FakeEngine for each test."""
    return FakeEngine()


@pytest.fixture()
def router(fake_engine: FakeEngine) -> Router:
    """Router with FakeEngine + InMemoryDedupStore (parameters match 01-03 constructor).

    TODO(01-03): finalize Router constructor signature and wire lane/slots.
    The fixture is declared here so test files in this plan can import make_event
    and conftest is importable; the router fixture will xfail until 01-03 builds Router.
    """
    return Router(
        max_concurrent_invocations=2,
        max_queued_total=5,
        idempotency_window_seconds=60,
        dedup_store=InMemoryDedupStore(),
        engine_runner=fake_engine.run,
        delivery_adapter=None,  # LogDeliveryAdapter() added in 01-03/01-04
    )  # type: ignore[call-arg]


@pytest.fixture()
def fake_ek_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a recognizable fake ek_ key and ACH_BASE_URL into os.environ.

    The sentinel value "ek_test_sentinel_do_not_log" must NEVER appear in
    captured log output (SEC-01 / test_ek_never_logged).
    """
    monkeypatch.setenv("ACH_API_KEY", "ek_test_sentinel_do_not_log")
    monkeypatch.setenv("ACH_BASE_URL", "http://127.0.0.1:19999/v1")
    yield
