"""Executable characterization of behavior inherited from original v0.16.1.

This module deliberately records observable contracts before the split is simplified.
The credentialed workspace test is expected to fail on the current private-clone path;
it becomes the regression test for Task 3.
"""

from pathlib import Path

import pytest

from ach_agent.boot.prepare import run_cleanup, run_prepare, workspace_dir
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import PrepareBlock


def _event(**delivery_context: object) -> MessageEvent:
    return MessageEvent(
        idempotency_key="e1",
        session_key="repo:42",
        channel_name="review",
        delivery_context=delivery_context,
    )


@pytest.mark.asyncio
async def test_credentialed_prepare_keeps_original_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prepare runs in the stable shared workspace, even when it resolves a secret."""
    ws = workspace_dir(str(tmp_path), "repo:42")
    ws.mkdir()
    (ws / "retained").write_text("original checkout")
    monkeypatch.setenv("TEST_FORGE_TOKEN", "synthetic")
    block = PrepareBlock.model_validate(
        {
            "script": (
                'test "$PWD" = "$ACH_WORKSPACE"; '
                'test "$HOME" = "$ACH_WORKSPACE"; '
                'test "$TOKEN" = synthetic; test -f retained; '
                'printf ok >> runs'
            ),
            "secretEnv": {"TOKEN": {"env": "TEST_FORGE_TOKEN"}},
        }
    )
    event = _event()

    await run_prepare(block, event, ws)
    await run_prepare(block, event, ws)

    assert (ws / "retained").read_text() == "original checkout"
    assert (ws / "runs").read_text() == "okok"


@pytest.mark.asyncio
async def test_cleanup_runs_from_workspace_parent_with_original_environment(
    tmp_path: Path,
) -> None:
    ws = workspace_dir(str(tmp_path), "repo:42")
    ws.mkdir(parents=True)
    block = PrepareBlock.model_validate(
        {
            "script": (
                'test "$PWD" = "$(dirname "$ACH_WORKSPACE")"; '
                'test "$HOME" = "$ACH_WORKSPACE"; '
                'printf cleaned > "$ACH_WORKSPACE/cleanup-marker"'
            )
        }
    )

    await run_cleanup(block, _event(), ws)

    assert (ws / "cleanup-marker").read_text() == "cleaned"


def test_workspace_mapping_is_stable_and_keyed_by_session() -> None:
    first = workspace_dir("/work", "repo:42")
    assert first == workspace_dir("/work", "repo:42")
    assert first != workspace_dir("/work", "repo:43")
    assert first.parent == Path("/work")

