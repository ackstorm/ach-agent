"""Stop-event cleanup registry coverage.

The old private checkout and Git bundle implementation was removed; hook execution is
covered by ``tests/test_prepare.py`` and the shared-workspace compatibility tests.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ach_agent.boot.private_prepare import PrivateCleanupRegistry, PrivatePrepareFailed
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock
from ach_agent.execution.wire import WorkspaceStoppedEvent


def _event(number: int = 1) -> MessageEvent:
    return MessageEvent(
        idempotency_key=f"evt-{number}",
        session_key="group/project:7",
        channel_name="gitlab-mr-review",
        delivery_context={"project_path": "group/project"},
    )


def _stopped(event: MessageEvent, invocation_id: str) -> WorkspaceStoppedEvent:
    return WorkspaceStoppedEvent(
        controller_id="controller",
        instance_id="instance",
        session_key=event.session_key,
        event_id=event.idempotency_key,
        invocation_id=invocation_id,
        workspace="/work/group-project-7",
    )


async def _record_ack(acks: list[str], event: WorkspaceStoppedEvent) -> None:
    acks.append(event.invocation_id)


@pytest.mark.asyncio
async def test_registry_correlates_stop_event_and_acknowledges(tmp_path: Path) -> None:
    event = _event()
    registry = PrivateCleanupRegistry(max_contexts=2)
    cfg = PrepareBlock.model_validate({"script": "true"})
    await registry.register("invocation", event, tmp_path / "workspace", cfg)

    mismatched = _stopped(event, "invocation").model_copy(update={"event_id": "wrong"})
    acknowledgements: list[str] = []
    assert not await registry.handle_event(
        mismatched, lambda value: _record_ack(acknowledgements, value)
    )

    assert await registry.handle_event(
        _stopped(event, "invocation"), lambda value: _record_ack(acknowledgements, value)
    )
    for _ in range(100):
        if acknowledgements:
            break
        await asyncio.sleep(0.01)
    assert acknowledgements == ["invocation"]
    await registry.close()


@pytest.mark.asyncio
async def test_registry_dispatches_bounded_callbacks(tmp_path: Path) -> None:
    cfg = PrepareBlock.model_validate({"script": "true"})
    registry = PrivateCleanupRegistry(max_contexts=64)
    acknowledgements: list[str] = []
    events = []
    for number in range(10):
        event = _event(number + 1)
        invocation_id = f"invocation-{number}"
        await registry.register(
            invocation_id, event, tmp_path / "workspace", cfg
        )
        events.append(_stopped(event, invocation_id))
    for event in events:
        assert await registry.handle_event(
            event, lambda value: _record_ack(acknowledgements, value)
        )
    for _ in range(100):
        if len(acknowledgements) == len(events):
            break
        await asyncio.sleep(0.01)
    assert sorted(acknowledgements) == sorted(event.invocation_id for event in events)
    await registry.close()


@pytest.mark.asyncio
async def test_registry_rejects_duplicate_and_overflow_contexts(tmp_path: Path) -> None:
    cfg = PrepareBlock.model_validate({"script": "true"})
    registry = PrivateCleanupRegistry(max_contexts=1)
    event = _event()
    await registry.register("one", event, tmp_path / "workspace", cfg)
    with pytest.raises(PrivatePrepareFailed, match="already registered"):
        await registry.register("one", event, tmp_path / "workspace", cfg)
    with pytest.raises(PrivatePrepareFailed, match="limit reached"):
        await registry.register("two", event, tmp_path / "workspace", cfg)
    await registry.close()
