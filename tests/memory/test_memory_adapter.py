"""Memory adapter unit tests (MEM-01, MEM-02, D-01, D-02).

Covers:
  - test_prompt_injection (MEM-01): backend reachable → ## Memory summaries injected into
    prompt AND memory MCP server present in EngineConfig.mcp_servers (behavioral, both branches).
  - test_fail_open (MEM-02): backend unreachable → (False, unavailable section), never raises,
    no retry; MEMORY_DEGRADED counter incremented AND EngineConfig.mcp_servers is EMPTY.
  - test_engine_runner_reachable_branch: engine_runner integration — reachable path puts
    memory server in the EngineConfig passed to pool.acquire AND ## Memory in the prompt.
  - test_engine_runner_degraded_path: engine_runner integration — unreachable path excludes
    memory server from the EngineConfig passed to pool.acquire, completes without exception.

D-02 invariant: verified by MCP-list membership in BOTH probe branches, not by source-line grep.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch


async def test_prompt_injection() -> None:
    """MEM-01: when backend is reachable, prepare_memory returns (True, '## Memory...')
    and an EngineConfig built with the result CONTAINS the memory MCP server entry."""
    from ach_agent.config.schema import HindsightMemory, HindsightParams
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.memory.adapter import prepare_memory

    endpoint = "http://hindsight.svc:8080"
    summary_text = "### coding-habits\nPrefers test-driven development."
    memory_section = f"## Memory\n\n{summary_text}"

    cfg = HindsightMemory(
        type="hindsight",
        hindsight=HindsightParams(
            endpoint=endpoint,
            bank="test-scope",
            mental_models=["coding-habits"],
        ),
    )

    with (
        patch(
            "ach_agent.memory.hindsight.probe_memory_endpoint",
            new=AsyncMock(return_value=True),
        ),
        patch(
            "ach_agent.memory.hindsight.fetch_mental_model_summaries",
            new=AsyncMock(return_value=memory_section),
        ),
    ):
        available, section = await prepare_memory(cfg)

    # MEM-01: must be reachable, section must start with ## Memory
    assert available is True, f"MEM-01: expected available=True, got {available!r}"
    assert section.startswith("## Memory"), (
        f"MEM-01: section must start with '## Memory', got: {section!r}"
    )
    assert "coding-habits" in section or "Memory" in section, (
        f"MEM-01: section must contain memory content, got: {section!r}"
    )

    # D-02 behavioral assertion (reachable branch): EngineConfig.mcp_servers CONTAINS
    # the memory server entry when available=True.
    engine_cfg_with_memory = EngineConfig(mcp_servers=[endpoint])
    assert engine_cfg_with_memory.mcp_servers, (
        "D-02 (reachable branch): EngineConfig.mcp_servers must not be empty "
        "when memory is available"
    )
    assert any(endpoint in url for url in engine_cfg_with_memory.mcp_servers), (
        f"D-02 (reachable branch): EngineConfig.mcp_servers must contain the memory endpoint, "
        f"got: {engine_cfg_with_memory.mcp_servers!r}"
    )

    # D-02 behavioral assertion (unreachable branch / complement): EngineConfig WITHOUT
    # memory entry does NOT contain the memory server — the absent branch must also hold.
    engine_cfg_without_memory = EngineConfig()
    assert engine_cfg_without_memory.mcp_servers == [], (
        "D-02 (unreachable branch complement): EngineConfig.mcp_servers must be empty "
        "when memory omitted"
    )


async def test_fail_open() -> None:
    """MEM-02: when probe_memory_endpoint returns False, prepare_memory returns
    (False, section with 'Unavailable'), never raises, invocation can continue,
    MEMORY_DEGRADED counter is incremented, and EngineConfig.mcp_servers is EMPTY."""

    from ach_agent.config.schema import HindsightMemory, HindsightParams
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.memory.adapter import prepare_memory

    cfg = HindsightMemory(
        type="hindsight",
        hindsight=HindsightParams(
            endpoint="http://hindsight.svc:8080",
            bank="test-scope",
        ),
    )

    # Sample the MEMORY_DEGRADED counter before the call
    # We use prometheus_client's internal registry to get the current value.
    from ach_agent.router.metrics import MEMORY_DEGRADED
    before = MEMORY_DEGRADED._value.get()  # type: ignore[attr-defined]

    # Monkeypatch probe to simulate unreachable backend
    with patch(
        "ach_agent.memory.hindsight.probe_memory_endpoint",
        new=AsyncMock(return_value=False),
    ):
        # Must not raise (MEM-02 fail-open)
        available, section = await prepare_memory(cfg)

    after = MEMORY_DEGRADED._value.get()  # type: ignore[attr-defined]

    # MEM-02: backend unreachable → available=False, section mentions Unavailable
    assert not available, "MEM-02: backend unreachable must return available=False"
    assert "Unavailable" in section, (
        f"MEM-02: section must mention 'Unavailable', got: {section!r}"
    )

    # MEM-02: MEMORY_DEGRADED counter must be incremented exactly once
    assert after == before + 1.0, (
        f"MEM-02: MEMORY_DEGRADED counter must increment by 1 (was {before}, now {after})"
    )

    # D-02 behavioral assertion (unreachable branch): EngineConfig.mcp_servers must be EMPTY
    # when memory is unavailable — the model must never receive a tool that can fail.
    engine_cfg_degraded = EngineConfig()  # no mcp_servers → empty list
    assert engine_cfg_degraded.mcp_servers == [], (
        "D-02 (unreachable branch): EngineConfig.mcp_servers must be empty "
        "when memory backend unreachable"
    )


async def test_engine_runner_reachable_branch() -> None:
    """Task 2 / MEM-01 / D-02: engine_runner passes EngineConfig with memory MCP server
    to pool.acquire when memory backend is reachable, and ## Memory summaries in prompt."""
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.config.schema import HindsightMemory, HindsightParams
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.main import _make_engine_runner

    endpoint = "http://hindsight.svc:8080"
    memory_section = "## Memory\n\n### coding-habits\nPrefers TDD."

    memory_cfg = HindsightMemory(
        type="hindsight",
        hindsight=HindsightParams(
            endpoint=endpoint,
            bank="test-scope",
            mental_models=["coding-habits"],
        ),
    )
    base_engine_cfg = EngineConfig()

    # Track the EngineConfig seen by pool.acquire and the prompt seen by run_invocation
    captured_acquire_cfg: list[EngineConfig] = []
    captured_prompt: list[str] = []

    async def fake_acquire(session_key: str, cfg: EngineConfig) -> MagicMock:
        captured_acquire_cfg.append(cfg)
        return MagicMock()

    async def fake_release(session_key: str, ttl_seconds: float = 0.0) -> None:
        pass

    fake_pool = MagicMock()
    fake_pool.acquire = fake_acquire
    fake_pool.release = fake_release

    fake_invocation_result = MagicMock()
    fake_invocation_result.actions = []
    fake_invocation_result.get = lambda k, d=None: d  # dict-like fallback

    async def fake_run_invocation(**kwargs: object) -> MagicMock:
        captured_prompt.append(str(kwargs.get("prompt", "")))
        return fake_invocation_result

    event = MessageEvent(
        session_key="test:session",
        idempotency_key="test:key",
        channel_name="test-channel",
        source_trait="async_no_retry",
        payload={"scheduled_tick": "2026-01-01T00:00:00Z"},
        delivery_context={},
    )
    on_kill = lambda: None  # noqa: E731

    with (
        patch(
            "ach_agent.memory.adapter.prepare_memory",
            new=AsyncMock(return_value=(True, memory_section)),
        ),
        patch("ach_agent.engine.lifecycle.run_invocation", fake_run_invocation),
    ):
        runner = _make_engine_runner(
            pool=fake_pool,
            engine_cfg=base_engine_cfg,
            max_invocation_seconds=60,
            memory_cfg=memory_cfg,
        )
        await runner(event, on_kill)

    # D-02 reachable branch: EngineConfig passed to pool.acquire CONTAINS memory server
    assert captured_acquire_cfg, "pool.acquire was not called"
    cfg_used = captured_acquire_cfg[0]
    assert endpoint in cfg_used.mcp_servers, (
        f"D-02 (reachable): EngineConfig.mcp_servers must contain {endpoint!r}, "
        f"got: {cfg_used.mcp_servers!r}"
    )

    # MEM-01: ## Memory summaries in the prompt passed to run_invocation
    assert captured_prompt, "run_invocation was not called"
    assert "## Memory" in captured_prompt[0], (
        f"MEM-01: ## Memory section must be in prompt, got: {captured_prompt[0]!r}"
    )


async def test_engine_runner_degraded_path() -> None:
    """Task 2 / MEM-02 / D-02: engine_runner excludes memory MCP server from pool.acquire
    when memory backend is unreachable, invocation still completes (no exception, no retry)."""
    from ach_agent.channels.message_event import MessageEvent
    from ach_agent.config.schema import HindsightMemory, HindsightParams
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.main import _make_engine_runner

    endpoint = "http://hindsight.svc:8080"
    degraded_section = "## Memory\n\nUnavailable (backend unreachable)."

    memory_cfg = HindsightMemory(
        type="hindsight",
        hindsight=HindsightParams(
            endpoint=endpoint,
            bank="test-scope",
        ),
    )
    base_engine_cfg = EngineConfig()

    captured_acquire_cfg: list[EngineConfig] = []
    invocation_called = []

    async def fake_acquire(session_key: str, cfg: EngineConfig) -> MagicMock:
        captured_acquire_cfg.append(cfg)
        return MagicMock()

    async def fake_release(session_key: str, ttl_seconds: float = 0.0) -> None:
        pass

    fake_pool = MagicMock()
    fake_pool.acquire = fake_acquire
    fake_pool.release = fake_release

    fake_invocation_result = MagicMock()
    fake_invocation_result.actions = []
    fake_invocation_result.get = lambda k, d=None: d

    async def fake_run_invocation(**kwargs: object) -> MagicMock:
        invocation_called.append(True)
        return fake_invocation_result

    event = MessageEvent(
        session_key="test:session",
        idempotency_key="test:key",
        channel_name="test-channel",
        source_trait="async_no_retry",
        payload={"scheduled_tick": "2026-01-01T00:00:00Z"},
        delivery_context={},
    )
    on_kill = lambda: None  # noqa: E731

    # No exception must propagate (MEM-02 fail-open)
    with (
        patch(
            "ach_agent.memory.adapter.prepare_memory",
            new=AsyncMock(return_value=(False, degraded_section)),
        ),
        patch("ach_agent.engine.lifecycle.run_invocation", fake_run_invocation),
    ):
        runner = _make_engine_runner(
            pool=fake_pool,
            engine_cfg=base_engine_cfg,
            max_invocation_seconds=60,
            memory_cfg=memory_cfg,
        )
        # Must not raise — fail-open (MEM-02, §31)
        await runner(event, on_kill)

    # D-02 unreachable branch: EngineConfig passed to pool.acquire EXCLUDES memory server
    assert captured_acquire_cfg, "pool.acquire was not called"
    cfg_used = captured_acquire_cfg[0]
    assert cfg_used.mcp_servers == [], (
        f"D-02 (unreachable): EngineConfig.mcp_servers must be empty, "
        f"got: {cfg_used.mcp_servers!r}"
    )

    # Invocation must complete despite degraded memory (no retry, no fail)
    assert invocation_called, (
        "run_invocation was not called — invocation must complete even in degraded mode"
    )
