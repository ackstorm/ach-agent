"""Shared fixtures for engine tests.

Provides:
  - FakeSlotManager: records on_kill calls (Phase 0 stub for the on_kill seam)
  - fake_ek_env: injects recognizable fake ek_ key + ACH_BASE_URL into env
  - free_port: finds a free TCP port for test use
"""
from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest


# ---------------------------------------------------------------------------
# FakeSlotManager
# ---------------------------------------------------------------------------


class FakeSlotManager:
    """Phase 0 stub for the on_kill seam — records release calls.

    Usage in tests::

        mgr = FakeSlotManager()
        result = await run_invocation(..., on_kill=mgr.on_kill)
        assert mgr.released
    """

    released: bool = False

    def on_kill(self) -> None:
        """Called by the watchdog when maxInvocationSeconds is exceeded."""
        self.released = True


@pytest.fixture()
def fake_slot_manager() -> FakeSlotManager:
    """Fresh FakeSlotManager for each test."""
    return FakeSlotManager()


# ---------------------------------------------------------------------------
# fake_ek_env
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_ek_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Inject a recognizable fake ek_ key and ACH_BASE_URL into os.environ.

    The sentinel value "ek_test_sentinel_do_not_log" must NEVER appear in
    captured log output (SEC-01 / test_ek_never_logged).
    """
    monkeypatch.setenv("ACH_API_KEY", "ek_test_sentinel_do_not_log")
    monkeypatch.setenv("ACH_BASE_URL", "http://127.0.0.1:19999/v1")
    yield


# ---------------------------------------------------------------------------
# free_port helper
# ---------------------------------------------------------------------------


def find_free_port() -> int:
    """Bind to port 0 to get a free OS-assigned port, then release it.

    There is a brief TOCTOU race between release and the caller binding, but
    this is acceptable for test fixtures. For production use, see
    engine/client.py:find_free_port() which adds a 20-retry reserved-ports loop.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def free_port() -> int:
    """Return a free TCP port number for test use."""
    return find_free_port()
