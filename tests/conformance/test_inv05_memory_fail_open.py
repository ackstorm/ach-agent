"""CONTRACT §6.5: Memory fail-open invariant (authoritative conformance test).

Invariant: memory backend down → run without memory context, log it,
never fail the invocation (read and write).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch


async def test_inv05_memory_fail_open() -> None:
    """§6.5: memory fail-open — backend down never fails the invocation — authoritative conformance.

    CONTRACT perspective: when the memory backend is unreachable, the backend's prepare
    returns (False, <unavailable section>) and does NOT raise. The invocation continues
    (fail-open semantics per §31). The invariant is backend-independent: it was first proved
    against Hindsight and now holds for ach-memory unchanged.
    """
    from ach_agent.config.schema import AchMemoryMemory
    from ach_agent.memory.ach_memory import prepare_ach_memory

    cfg = AchMemoryMemory.model_validate(
        {"type": "ach-memory", "achMemory": {"endpoint": "http://ach-memory.svc:8000"}}
    )

    # Simulate backend unreachable: probe returns False.
    with patch(
        "ach_agent.memory.ach_memory.probe_memory_endpoint",
        new=AsyncMock(return_value=False),
    ):
        # Must not raise — fail-open invariant (§6.5 / §31)
        try:
            available, section = await prepare_ach_memory(cfg, "test-project")
        except Exception as exc:  # noqa: BLE001
            raise AssertionError(
                f"§6.5: prepare must not raise when backend is down, "
                f"got {type(exc).__name__}: {exc}"
            ) from exc

    assert available is False, (
        "§6.5: backend unreachable must return available=False (fail-open)"
    )
    assert isinstance(section, str) and section, (
        "§6.5: fail-open must return a non-empty section string (unavailable placeholder)"
    )
    # The section must convey unavailability — the model receives it in the prompt.
    assert "Unavailable" in section or "unavailable" in section, (
        f"§6.5: fail-open section must mention unavailability, got: {section!r}"
    )
