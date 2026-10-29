import time
from typing import Any

import pytest

from ach_runtime.engine import opencode

_SKEW = 0.01  # timestamp capture skew between stub and predicate, not a behaviour tolerance


class _ReadinessStub:
    """`_await_native_readiness` only calls `request` and `_kill` on the session."""

    def __init__(self, mcp_states: list[str], *, system: str | None = "s") -> None:
        self._mcp_states = mcp_states
        self._system = system
        self.mcp_at: list[float] = []
        self.killed = False

    async def request(self, method: str, path: str, **_: Any) -> tuple[int, Any]:
        if path == "/api/config":
            return 200, []
        if path == "/api/model":
            return 200, {"data": [{"providerID": "p", "id": "m"}]}
        if path.startswith("/api/agent/"):
            agent = {"id": "a", "steps": 2}
            if self._system is not None:
                agent["system"] = self._system
            return 200, {"data": agent}
        self.mcp_at.append(time.monotonic())
        state = self._mcp_states[min(len(self.mcp_at), len(self._mcp_states)) - 1]
        return 200, {"data": [{"name": "srv", "status": {"status": state}}]}

    async def _kill(self, *, confirm_timeout: float) -> None:
        self.killed = True


async def _ready(
    stub: _ReadinessStub, *, budget: float, required_mcp: list[str] | None = None, **extra: Any
) -> None:
    await opencode._await_native_readiness(
        stub,
        wid="/w",
        managed_agent="a",
        required_mcp=required_mcp if required_mcp is not None else ["srv"],
        provider_id="p",
        model_id="m",
        max_steps=2,
        deadline=time.monotonic() + budget,
        **extra,
    )


async def test_mcp_readiness_holds_connected_for_native_tool_reload_window() -> None:
    stub = _ReadinessStub(["connected"])
    await _ready(stub, budget=5.0)
    assert len(stub.mcp_at) >= 2
    assert stub.mcp_at[-1] - stub.mcp_at[0] >= opencode.NATIVE_MCP_TOOL_SETTLE_SECONDS - _SKEW


async def test_mcp_settle_restarts_when_connection_drops() -> None:
    stub = _ReadinessStub(["connected", "failed", "connected"])
    await _ready(stub, budget=5.0)
    assert len(stub.mcp_at) >= 4
    assert stub.mcp_at[-1] - stub.mcp_at[2] >= opencode.NATIVE_MCP_TOOL_SETTLE_SECONDS - _SKEW


async def test_mcp_settle_never_outlives_absolute_deadline() -> None:
    stub = _ReadinessStub(["connected"])
    with pytest.raises(opencode.NativeLaunchFailed, match="/api/mcp"):
        await _ready(stub, budget=0.15)
    assert stub.killed


async def test_no_mcp_skips_settle_poll() -> None:
    stub = _ReadinessStub(["connected"])
    await _ready(stub, budget=0.15, required_mcp=[])
    assert stub.mcp_at == []
    assert stub.killed is False


async def test_agent_without_system_prompt_is_ready_when_none_is_expected() -> None:
    """An agent with no prompt.system has no native `system`; readiness must not demand one."""
    stub = _ReadinessStub(["connected"], system=None)
    await _ready(stub, budget=0.5, required_mcp=[], expect_system=False)
    assert stub.killed is False


async def test_missing_system_prompt_still_fails_when_one_is_expected() -> None:
    stub = _ReadinessStub(["connected"], system=None)
    with pytest.raises(opencode.NativeLaunchFailed, match="/api/agent/"):
        await _ready(stub, budget=0.15, required_mcp=[])


@pytest.mark.parametrize(
    "fixture,expected",
    [
        ({}, False),  # no prompt.system: OpenCode's default prompt
        ({"systemPrompt": "p", "promptCompose": "append"}, False),  # appended by a session hook, never agent.system
        ({"systemPrompt": "p", "promptCompose": "replace"}, True),
        ({"promptCompose": "replace"}, False),
    ],
)
def test_native_system_is_expected_only_when_replace_sets_it(fixture: dict[str, Any], expected: bool) -> None:
    assert opencode._expects_native_system(fixture) is expected
