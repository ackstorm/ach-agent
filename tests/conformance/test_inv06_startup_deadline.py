"""CONTRACT §6.6: Startup deadline invariant (authoritative conformance test).

Invariant: engine not ready within startupTimeoutSeconds → exit process.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


async def test_inv06_startup_deadline_exits() -> None:
    """§6.6: startup deadline — exit process when engine not ready — authoritative conformance.

    CONTRACT perspective: if the engine health check never returns True before
    startupTimeoutSeconds elapses, the harness must exit the process with a
    non-zero code. The process must not hang indefinitely or swallow the error.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, poll_ready

    server = ManagedServer(port=19900)
    mock_client = AsyncMock(spec=OpenCodeClient)
    # Health check always fails — startup deadline must fire.
    mock_client.check_health = AsyncMock(return_value=False)
    server._client = mock_client

    with pytest.raises(SystemExit) as exc_info:
        # Very short timeout to keep the test fast.
        await poll_ready(server, startup_timeout_seconds=1)

    assert exc_info.value.code != 0, (
        "§6.6: startup deadline must exit with a non-zero code — "
        "exit code 0 would suggest a successful start (spec §8.5)"
    )
