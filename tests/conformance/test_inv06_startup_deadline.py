"""CONTRACT §6.6: Startup deadline invariant (authoritative conformance test).

Invariant: engine not ready within startupTimeoutSeconds → typed launch failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest


async def test_inv06_startup_deadline_is_typed() -> None:
    """§6.6: startup deadline — typed failure for service cleanup.

    CONTRACT perspective: if the engine health check never returns True before
    startupTimeoutSeconds elapses, the harness must exit the process with a
    non-zero code. The process must not hang indefinitely or swallow the error.
    """
    from ach_agent.engine.client import OpenCodeClient
    from ach_agent.engine.lifecycle import ManagedServer, NativeLaunchFailed, poll_ready

    server = ManagedServer(port=19900)
    mock_client = AsyncMock(spec=OpenCodeClient)
    # Health check always fails — startup deadline must fire.
    mock_client.check_health = AsyncMock(return_value=False)
    server._client = mock_client

    with pytest.raises(NativeLaunchFailed):
        # Very short timeout to keep the test fast.
        await poll_ready(server, startup_timeout_seconds=1)
