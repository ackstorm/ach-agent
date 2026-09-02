# SPDX-License-Identifier: Apache-2.0
"""channel.prepare — schema guards, env construction (the trust boundary), and execution."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from ach_agent.boot.prepare import (
    PrepareFailed,
    WebhookScriptFailed,
    build_prepare_env,
    prepare_workspace,
    run_cleanup,
    run_prepare,
    run_webhook_script,
    workspace_dir,
)
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ChannelConfig, PrepareBlock


def _event(**dc: object) -> MessageEvent:
    return MessageEvent(
        idempotency_key="evt-1",
        session_key="42:7",
        channel_name="gitlab-mr-review",
        delivery_context=dict(dc),
    )


def _block(script: str = "true", **kw: object) -> PrepareBlock:
    return PrepareBlock.model_validate({"script": script, **kw})


# --------------------------------------------------------------------------- schema


def test_reserved_env_names_rejected() -> None:
    """The harness pins these last; a config entry would be silently discarded."""
    for name in ("ACH_WORKSPACE", "HOME", "ACH_EVENT_PROJECT_PATH"):
        with pytest.raises(ValidationError, match="reserved"):
            _block(env={name: "x"})


def test_empty_script_and_env_clash_rejected() -> None:
    with pytest.raises(ValidationError, match="must not be empty"):
        _block("   ")
    with pytest.raises(ValidationError, match="both env and secretEnv"):
        _block(env={"T": "a"}, secretEnv={"T": {"env": "ACH_SECRET_T"}})


def test_prepare_allowed_on_any_channel_type() -> None:
    """A cron channel may want a workspace too — prepare is outside the type↔block check."""
    ch = ChannelConfig.model_validate(
        {
            "name": "nightly",
            "type": "cron",
            "cron": {"schedule": "0 8 * * *"},
            "prepare": {"script": "true"},
        }
    )
    assert ch.prepare is not None


def test_cleanup_uses_prepare_shape() -> None:
    ch = ChannelConfig.model_validate(
        {
            "name": "review",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
            "cleanup": {
                "script": "rm -rf -- \"$ACH_WORKSPACE\"",
                "env": {"MODE": "review"},
                "secretEnv": {"TOKEN": {"env": "CLEANUP_TOKEN"}},
                "timeoutSeconds": 30,
            },
        }
    )
    assert ch.cleanup is not None
    assert ch.cleanup.env == {"MODE": "review"}
    assert ch.cleanup.secret_env["TOKEN"].env == "CLEANUP_TOKEN"
    assert ch.cleanup.timeout_seconds == 30


def test_cleanup_requires_prepare() -> None:
    with pytest.raises(ValidationError, match="cleanup.*requires.*prepare"):
        ChannelConfig.model_validate(
            {
                "name": "review",
                "type": "cron",
                "cron": {"schedule": "* * * * *"},
                "cleanup": {"script": "true"},
            }
        )


# ------------------------------------------------------------------- env / trust boundary


def test_event_scalars_become_env_but_callables_do_not() -> None:
    """delivery_context also carries the on_complete/on_fail callables — never stringify them."""
    env = build_prepare_env(
        _block(),
        _event(
            project_id=42, project_path="group/sub/proj", head_sha="abc", on_fail=lambda *_: None
        ),
        workspace_dir("/w", "42:7"),
    )
    assert env["ACH_EVENT_PROJECT_ID"] == "42"
    assert env["ACH_EVENT_PROJECT_PATH"] == "group/sub/proj"
    assert "ACH_EVENT_ON_FAIL" not in env


@pytest.mark.parametrize(
    "bad",
    [
        "https://evil.test/x",  # a scheme would redirect the credential off-host
        "oauth2:token@evil.test/x",  # userinfo
        "../../etc/passwd",  # traversal
        "group/../../evil",  # traversal mid-path
        "group/proj\nrm -rf /",  # newline
    ],
)
def test_malformed_repo_paths_are_dropped(bad: str) -> None:
    """Origin comes from config; the payload supplies only a path. Anything else is dropped,
    and `sh -u` then aborts the script rather than cloning from an attacker's host."""
    env = build_prepare_env(_block(), _event(project_path=bad), workspace_dir("/w", "k"))
    assert "ACH_EVENT_PROJECT_PATH" not in env


def test_harness_vars_win_over_operator_env() -> None:
    env = build_prepare_env(
        _block(env={"REPO_BASE_URL": "https://gitlab.example.com"}),
        _event(),
        workspace_dir("/w", "42:7"),
    )
    assert env["REPO_BASE_URL"] == "https://gitlab.example.com"
    assert env["ACH_WORKSPACE"].startswith("/w/")
    assert env["HOME"] == env["ACH_WORKSPACE"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["ACH_SESSION_KEY"] == "42:7"


def test_secret_env_resolved_at_use_time(monkeypatch: pytest.MonkeyPatch) -> None:
    block = _block(secretEnv={"GITLAB_TOKEN": {"env": "ACH_SECRET_CLONE"}})
    monkeypatch.setenv("ACH_SECRET_CLONE", "glpat-rotated")
    env = build_prepare_env(block, _event(), workspace_dir("/w", "k"))
    assert env["GITLAB_TOKEN"] == "glpat-rotated"


def test_unset_secret_is_omitted_not_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail closed: `sh -u` must abort, not run an anonymous clone with an empty token."""
    monkeypatch.delenv("ACH_SECRET_CLONE", raising=False)
    block = _block(secretEnv={"GITLAB_TOKEN": {"env": "ACH_SECRET_CLONE"}})
    assert "GITLAB_TOKEN" not in build_prepare_env(block, _event(), workspace_dir("/w", "k"))


def test_workspace_is_stable_per_session_key_and_separates_keys() -> None:
    assert workspace_dir("/w", "42:7") == workspace_dir("/w", "42:7")
    assert workspace_dir("/w", "42:7") != workspace_dir("/w", "42:8")
    assert ":" not in workspace_dir("/w", "42:7").name


# ------------------------------------------------------------------------- execution


async def test_script_runs_in_the_workspace(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "42:7")
    await run_prepare(_block('echo "$ACH_EVENT_MR_IID" > marker'), _event(mr_iid=7), ws)
    assert (ws / "marker").read_text().strip() == "7"


async def test_webhook_script_receives_payload_on_stdin_and_removes_workspace(
    tmp_path: Path,
) -> None:
    payload_file = tmp_path / "payload.json"
    workspace_file = tmp_path / "workspace.txt"
    event = _event(project_id=42)
    event.payload = {"event_name": "push", "project_id": 42}
    cfg = _block(
        'cat > "$PAYLOAD_FILE"; printf "%s" "$ACH_WORKSPACE" > "$WORKSPACE_FILE"',
        env={"PAYLOAD_FILE": str(payload_file), "WORKSPACE_FILE": str(workspace_file)},
    )

    await run_webhook_script(cfg, event, str(tmp_path / "work"))

    assert json.loads(payload_file.read_text()) == event.payload
    assert not Path(workspace_file.read_text()).exists()


async def test_webhook_script_nonzero_fails_without_an_engine(tmp_path: Path) -> None:
    with pytest.raises(WebhookScriptFailed, match="exited 9"):
        await run_webhook_script(_block("exit 9"), _event(), str(tmp_path / "work"))


async def test_payload_text_cannot_escape_into_the_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The injection test: a payload field that looks like shell stays inert, because it is
    only ever an env VALUE — the script text itself is static config."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    await run_prepare(
        _block('printf "%s" "$ACH_EVENT_TITLE" > out'),
        _event(title='x"; touch pwned; #'),
        ws,
    )
    assert not (ws / "pwned").exists()
    assert (ws / "out").read_text() == 'x"; touch pwned; #'


async def test_nonzero_exit_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(PrepareFailed, match="exited 3"):
        await run_prepare(_block("echo boom >&2; exit 3"), _event(), ws)


async def test_unset_var_aborts_the_script(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`sh -u`: a missing credential is loud, never a half-working anonymous clone."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(PrepareFailed):
        await run_prepare(_block('git clone "$MISSING_TOKEN"'), _event(), ws)


async def test_timeout_kills_the_process_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(PrepareFailed, match="timed out"):
        await run_prepare(_block("sleep 30", timeoutSeconds=1), _event(), ws)


async def test_cancellation_kills_the_process_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Lane timeout/shutdown cancellation must not orphan the prepare command."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    task = asyncio.create_task(run_prepare(_block("echo $$ > pid; exec sleep 30"), _event(), ws))
    async with asyncio.timeout(2):
        while not (ws / "pid").exists():
            await asyncio.sleep(0.01)

    pid = int((ws / "pid").read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_prepare_secrets_are_stripped_from_forward_env() -> None:
    """A secretEnv name a misconfig also lists in engine.forwardEnv must never reach opencode."""
    from ach_agent.boot.secrets import collect_secret_env_names, strip_forwarded_secrets
    from ach_agent.config.schema import AgentConfig

    cfg = AgentConfig.model_validate(
        {
            "schemaVersion": "1",
            "agent": {"name": "a"},
            "model": {"name": "m", "type": "openai"},
            "capability": {"type": "ach", "ach": {"baseUrl": "https://ach.example.com"}},
            "engine": {"forwardEnv": ["ACH_SECRET_CLONE", "SSL_CERT_FILE"]},
            "channels": [
                {
                    "name": "c",
                    "type": "cron",
                    "cron": {"schedule": "0 8 * * *"},
                    "prepare": {
                        "script": "true",
                        "secretEnv": {"GITLAB_TOKEN": {"env": "ACH_SECRET_CLONE"}},
                    },
                }
            ],
        }
    )
    assert "ACH_SECRET_CLONE" in collect_secret_env_names(cfg)
    assert strip_forwarded_secrets(cfg) == ["SSL_CERT_FILE"]


async def test_cleanup_runs_from_workspace_parent_with_isolated_env(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    marker = ws.parent / "cleanup.txt"
    cfg = _block(
        'printf "%s|%s|%s" "$ACH_WORKSPACE" "$ACH_SESSION_KEY" "$ONLY_CLEANUP" '
        f'> "{marker}"',
        env={"ONLY_CLEANUP": "yes"},
    )

    await run_cleanup(cfg, _event(), ws)

    assert marker.read_text() == f"{ws}|42:7|yes"


async def test_cleanup_nonzero_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures:
        await run_cleanup(_block("echo failed >&2; exit 7"), _event(), ws)

    failures.labels.assert_called_once_with(reason="exit")
    failures.labels.return_value.inc.assert_called_once_with()


async def test_cleanup_nonzero_log_omits_env_values(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    value = "not-for-cleanup-logs"
    cfg = _block('printf "%s" "$MODE" >&2; exit 7', env={"MODE": value})

    with capture_logs() as logs:
        await run_cleanup(cfg, _event(), ws)

    warning = logs[-1]
    assert warning["event"] == "cleanup: script exited nonzero"
    assert warning["returncode"] == 7
    assert value not in str(warning)
    assert cfg.script not in str(warning)


async def test_cleanup_debug_log_contains_bounded_script_output(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    script = "printf stdout; printf stderr >&2; exit 10"

    with capture_logs() as logs:
        await run_cleanup(_block(script), _event(), ws)

    output = next(entry for entry in logs if entry["event"] == "cleanup: script output")
    assert output["log_level"] == "debug"
    assert output["stdout"] == "stdout"
    assert output["stderr"] == "stderr"
    assert output["truncated"] is False


async def test_prepare_debug_log_contains_script_output(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with capture_logs() as logs:
        await run_prepare(_block("printf ready; printf warning >&2"), _event(), ws)

    output = next(entry for entry in logs if entry["event"] == "prepare: script output")
    assert output["stdout"] == "ready"
    assert output["stderr"] == "warning"


async def test_cleanup_debug_output_keeps_only_the_tail(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    script = 'i=0; while [ "$i" -lt 5000 ]; do printf x; i=$((i + 1)); done'

    with capture_logs() as logs:
        await run_cleanup(_block(script), _event(), ws)

    output = next(entry for entry in logs if entry["event"] == "cleanup: script output")
    assert output["stdout"] == "x" * 4096
    assert output["truncated"] is True


async def test_cleanup_timeout_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures:
        await run_cleanup(_block("sleep 30", timeoutSeconds=1), _event(), ws)

    failures.labels.assert_called_once_with(reason="timeout")


async def test_cleanup_spawn_failure_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with (
        patch(
            "ach_agent.boot.prepare.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("no shell")),
        ),
        patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures,
    ):
        await run_cleanup(_block("true"), _event(), ws)

    failures.labels.assert_called_once_with(reason="spawn")


async def test_cleanup_cancellation_kills_process_group(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    pid_file = ws.parent / "cleanup-pid"
    task = asyncio.create_task(
        run_cleanup(
            _block(f'echo $$ > "{pid_file}"; exec sleep 30'),
            _event(),
            ws,
        )
    )
    async with asyncio.timeout(2):
        while not pid_file.exists():
            await asyncio.sleep(0.01)

    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.parametrize(
    ("runner", "start_event", "success_event"),
    [
        (run_prepare, "prepare: script running", "prepare: workspace ready"),
        (run_cleanup, "cleanup: script running", "cleanup: workspace hook complete"),
    ],
)
async def test_hook_logs_safe_start_and_success(
    tmp_path: Path,
    runner: object,
    start_event: str,
    success_event: str,
) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    secret = "not-for-logs"
    cfg = _block('printf "%s" "$SECRET_VALUE"', env={"SECRET_VALUE": secret})

    with capture_logs() as logs:
        await runner(cfg, _event(), ws)  # type: ignore[operator]

    start, success = (entry for entry in logs if entry["log_level"] == "info")
    assert start == {
        "event": start_event,
        "log_level": "info",
        "session_key": "42:7",
        "workspace": str(ws),
        "timeout_seconds": 120,
    }
    assert success["event"] == success_event
    assert success["log_level"] == "info"
    assert success["session_key"] == "42:7"
    assert success["workspace"] == str(ws)
    assert success["returncode"] == 0
    assert isinstance(success["duration_ms"], int)
    assert secret not in str((start, success))
    assert cfg.script not in str(logs)
