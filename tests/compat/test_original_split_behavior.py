"""Executable characterization of behavior inherited from original v0.16.1."""

from pathlib import Path

import pytest

from ach_agent.boot.prepare import run_cleanup, run_prepare, run_webhook_script, workspace_dir
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
                "set -eu; "
                'test "$PWD" = "$ACH_WORKSPACE"; '
                'test "$HOME" = "$ACH_WORKSPACE"; '
                'test "$TOKEN" = synthetic; test -f retained; '
                "printf ok >> runs"
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
                "set -eu; "
                'test "$PWD" = "$(dirname "$ACH_WORKSPACE")"; '
                'test "$HOME" = "$ACH_WORKSPACE"; '
                'printf cleaned > "$ACH_WORKSPACE/cleanup-marker"'
            )
        }
    )

    await run_cleanup(block, _event(), ws)

    assert (ws / "cleanup-marker").read_text() == "cleaned"


@pytest.mark.asyncio
async def test_cleanup_is_best_effort_after_validated_sentinel(tmp_path: Path) -> None:
    ws = workspace_dir(str(tmp_path), "repo:42")
    ws.mkdir(parents=True)
    block = PrepareBlock.model_validate(
        {
            "script": (
                "set -eu; "
                'test "$PWD" = "$(dirname "$ACH_WORKSPACE")"; '
                'test "$HOME" = "$ACH_WORKSPACE"; '
                'printf attempted > "$ACH_WORKSPACE/cleanup-sentinel"; '
                "exit 7"
            )
        }
    )

    # Cleanup failures are deliberately best-effort: the sentinel proves the validated
    # commands ran, and run_cleanup must return without replacing the caller's result.
    await run_cleanup(block, _event(), ws)

    assert (ws / "cleanup-sentinel").read_text() == "attempted"


@pytest.mark.asyncio
async def test_script_only_payload_uses_configured_workdir_and_is_removed(tmp_path: Path) -> None:
    work_dir = tmp_path / "script-work"
    cwd_file = tmp_path / "cwd"
    payload_file = tmp_path / "payload"
    block = PrepareBlock.model_validate(
        {
            "script": ('set -eu; pwd > "$CWD_FILE"; cat > "$PAYLOAD_FILE"'),
            "env": {"CWD_FILE": str(cwd_file), "PAYLOAD_FILE": str(payload_file)},
        }
    )
    event = MessageEvent(
        idempotency_key="e1",
        session_key="repo:42",
        channel_name="webhook-script",
        payload={"change": "synthetic"},
    )

    await run_webhook_script(block, event, str(work_dir))

    script_cwd = Path(cwd_file.read_text().strip())
    assert script_cwd.parent == work_dir
    assert script_cwd.name.startswith("webhook-script-")
    assert payload_file.read_bytes().endswith(b"\n")
    assert not script_cwd.exists()
    assert list(work_dir.iterdir()) == []


def test_workspace_mapping_is_stable_and_keyed_by_session() -> None:
    first = workspace_dir("/work", "repo:42")
    assert first == Path("/work/repo-42-a45d87f4")
    assert first == workspace_dir("/work", "repo:42")
    assert workspace_dir("/work", "repo:43") == Path("/work/repo-43-082624e0")
    assert first != workspace_dir("/work", "repo:43")
    assert first.parent == Path("/work")
