# SPDX-License-Identifier: Apache-2.0
"""channel.prepare — schema guards, env construction (the trust boundary), and execution."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
from pydantic import ValidationError
from structlog.testing import capture_logs

from ach_agent.boot.prepare import (
    HandoffFailed,
    WebhookScriptFailed,
    build_prepare_env,
    prepare_workspace,
    run_handoff,
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


def test_handoff_allowed_on_any_channel_type() -> None:
    """A cron channel may want a workspace too — handoff is outside the type↔block check."""
    ch = ChannelConfig.model_validate(
        {
            "name": "nightly",
            "type": "cron",
            "cron": {"schedule": "0 8 * * *"},
            "handoff": {"script": "true"},
        }
    )
    assert ch.handoff is not None
    assert ch.handoff.scope == "event"


# ------------------------------------------------------------------- env / trust boundary


def test_event_scalars_become_env_but_callables_do_not() -> None:
    """Non-serializable delivery metadata is never copied into the environment."""
    env = build_prepare_env(
        _block(),
        _event(
            project_id=42, project_path="group/sub/proj", head_sha="abc", marker=lambda *_: None
        ),
        workspace_dir("/w", "42:7"),
    )
    assert env["ACH_EVENT_PROJECT_ID"] == "42"
    assert env["ACH_EVENT_PROJECT_PATH"] == "group/sub/proj"
    assert "ACH_EVENT_MARKER" not in env


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
    assert env["HOME"] != env["ACH_WORKSPACE"]
    assert env["HOME"].startswith("/tmp/ach-hook-home-")
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
    await run_handoff(_block('echo "$ACH_EVENT_MR_IID" > marker'), _event(mr_iid=7), ws)
    assert (ws / "marker").read_text().strip() == "7"


async def test_prepare_home_is_private_from_engine_workspace(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "42:7")
    marker = tmp_path / "locations"
    cfg = _block(
        'printf "%s\\n%s\\n%s" "$PWD" "$HOME" "$ACH_WORKSPACE" > "$LOCATIONS"; '
        'printf engine > "$HOME/engine-cannot-own-hook-home"',
        env={"LOCATIONS": str(marker)},
    )

    await run_handoff(cfg, _event(), ws)

    cwd, hook_home, workspace = marker.read_text().splitlines()
    assert Path(cwd) == ws
    assert Path(workspace) == ws
    assert Path(hook_home) != ws
    assert Path(hook_home).is_dir()
    assert Path(hook_home).stat().st_mode & 0o777 == 0o700
    assert (Path(hook_home) / "engine-cannot-own-hook-home").read_text() == "engine"
    assert not (ws / "engine-cannot-own-hook-home").exists()


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


async def test_credentialed_webhook_script_uses_private_scratch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace_file = tmp_path / "workspace.txt"
    monkeypatch.setenv("WEBHOOK_TOKEN", "synthetic-token")
    cfg = _block(
        'printf "%s" "$ACH_WORKSPACE" > "$WORKSPACE_FILE"',
        env={"WORKSPACE_FILE": str(workspace_file)},
        secretEnv={"TOKEN": {"env": "WEBHOOK_TOKEN"}},
    )
    await run_webhook_script(cfg, _event(), str(tmp_path / "engine-work"))
    assert workspace_file.read_text().startswith(str(tmp_path / "engine-work"))


@pytest.mark.parametrize("credentialed", [False, True])
async def test_all_webhook_scripts_use_private_cwd_and_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, credentialed: bool
) -> None:
    """Webhook scripts use a short-lived cwd and harness-private HOME."""
    locations = tmp_path / ("locations-secret" if credentialed else "locations-public")
    engine_work = tmp_path / "engine-work"
    block_args: dict[str, object] = {
        "env": {"LOCATIONS": str(locations)},
    }
    if credentialed:
        monkeypatch.setenv("WEBHOOK_TOKEN", "synthetic-token")
        block_args["secretEnv"] = {"TOKEN": {"env": "WEBHOOK_TOKEN"}}
    cfg = _block(
        'printf "%s\\n%s\\n%s" "$PWD" "$HOME" "$ACH_WORKSPACE" > "$LOCATIONS"',
        **block_args,
    )

    await run_webhook_script(cfg, _event(), str(engine_work))

    cwd, home, workspace = locations.read_text().splitlines()
    assert Path(cwd).is_relative_to(engine_work)
    assert Path(home).name.startswith("ach-hook-home-")
    assert Path(home).parent == Path("/tmp")
    assert not Path(home).is_relative_to(engine_work)
    assert Path(workspace).is_relative_to(engine_work)
    assert not Path(cwd).exists()


async def test_webhook_script_nonzero_fails_without_an_engine(tmp_path: Path) -> None:
    with pytest.raises(WebhookScriptFailed, match="exited 9"):
        await run_webhook_script(_block("exit 9"), _event(), str(tmp_path / "work"))


async def test_webhook_script_payload_is_newline_terminated(tmp_path: Path) -> None:
    """`read` returns 1 at EOF-without-newline, and `sh -e` then aborts the whole script."""
    out = tmp_path / "line.json"
    event = _event()
    event.payload = {"event_name": "push", "project_id": 42}
    cfg = _block('read -r line; printf "%s" "$line" > "$OUT"', env={"OUT": str(out)})

    await run_webhook_script(cfg, event, str(tmp_path / "work"))

    assert json.loads(out.read_text()) == event.payload


async def test_webhook_script_survives_an_unpaired_surrogate(tmp_path: Path) -> None:
    """json.loads accepts a lone surrogate (a truncated emoji); encoding one as UTF-8 raises.

    That raise used to happen after mkdtemp, leaking one workspace per delivery.
    """
    work = tmp_path / "work"
    event = _event()
    event.payload = json.loads(r'{"title": "\ud83d truncated"}')

    await run_webhook_script(_block("cat > /dev/null"), event, str(work))

    assert work.is_dir()
    assert not list(work.iterdir())


async def test_webhook_script_text_is_not_in_proc_cmdline(tmp_path: Path) -> None:
    """/proc/<pid>/cmdline is world-readable; the co-resident agent must not read the script."""
    out = tmp_path / "cmdline"
    marker = "SECRET_MARKER_IN_SCRIPT_TEXT"
    cfg = _block(f'# {marker}\ncat /proc/$$/cmdline > "$OUT"', env={"OUT": str(out)})

    await run_webhook_script(cfg, _event(), str(tmp_path / "work"))

    cmdline = out.read_bytes().replace(b"\0", b" ").decode()
    assert marker not in cmdline
    assert "ACH_SCRIPT" in cmdline  # the trampoline, not the script


async def test_webhook_script_failure_message_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tail is embedded in the exception, and log.exception renders it AFTER redaction."""
    token = "glpat-not-for-tracebacks"
    monkeypatch.setenv("GITLAB_TOKEN", token)
    cfg = _block(f'printf "clone failed: {token}" >&2; exit 1')  # as a script would echo it

    with pytest.raises(WebhookScriptFailed) as exc:
        await run_webhook_script(cfg, _event(), str(tmp_path / "work"))

    assert token not in str(exc.value)
    assert "[REDACTED]" in str(exc.value)


async def test_payload_text_cannot_escape_into_the_shell(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The injection test: a payload field that looks like shell stays inert, because it is
    only ever an env VALUE — the script text itself is static config."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    await run_handoff(
        _block('printf "%s" "$ACH_EVENT_TITLE" > out'),
        _event(title='x"; touch pwned; #'),
        ws,
    )
    assert not (ws / "pwned").exists()
    assert (ws / "out").read_text() == 'x"; touch pwned; #'


async def test_nonzero_exit_fails_closed(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(HandoffFailed, match="exited 3"):
        await run_handoff(_block("echo boom >&2; exit 3"), _event(), ws)


async def test_unset_var_aborts_the_script(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """`sh -u`: a missing credential is loud, never a half-working anonymous clone."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(HandoffFailed):
        await run_handoff(_block('git clone "$MISSING_TOKEN"'), _event(), ws)


async def test_timeout_kills_the_process_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    with pytest.raises(HandoffFailed, match="timed out"):
        await run_handoff(_block("sleep 30", timeoutSeconds=1), _event(), ws)


async def test_cancellation_kills_the_process_group(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Lane timeout/shutdown cancellation must not orphan the prepare command."""
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    task = asyncio.create_task(run_handoff(_block("echo $$ > pid; exec sleep 30"), _event(), ws))
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
                    "handoff": {
                        "script": "true",
                        "secretEnv": {"GITLAB_TOKEN": {"env": "ACH_SECRET_CLONE"}},
                    },
                }
            ],
        }
    )
    assert "ACH_SECRET_CLONE" in collect_secret_env_names(cfg)
    assert strip_forwarded_secrets(cfg) == ["SSL_CERT_FILE"]


async def test_prepare_debug_log_contains_script_output(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with capture_logs() as logs:
        await run_handoff(_block("printf ready; printf warning >&2"), _event(), ws)

    output = next(e for e in logs if e["event"] == "handoff: script output")
    assert output["log_level"] == "debug"
    assert output["stdout"] == "ready"
    assert output["stderr"] == "warning"


async def test_prepare_logs_safe_start_and_success(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    secret = "not-for-logs"
    cfg = _block('printf "%s" "$SECRET_VALUE"', env={"SECRET_VALUE": secret})

    with capture_logs() as logs:
        await run_handoff(cfg, _event(), ws)

    start, success = (entry for entry in logs if entry["log_level"] == "info")
    assert start == {
        "event": "handoff: script running",
        "log_level": "info",
        "session_key": "42:7",
        "workspace": str(ws),
        "timeout_seconds": 120,
    }
    assert success["event"] == "handoff: workspace ready"
    assert success["log_level"] == "info"
    assert success["session_key"] == "42:7"
    assert success["workspace"] == str(ws)
    assert success["returncode"] == 0
    assert isinstance(success["duration_ms"], int)
    assert secret not in str((start, success))
    assert cfg.script not in str(logs)
