# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from ach_runtime.channels.event import Event
from ach_runtime.channels.gitlab import normalize_gitlab
from ach_runtime.config.schema import ChannelConfig, ScriptBlock
from ach_runtime.harness import scripts
from ach_runtime.harness.scripts import run_webhook_script


def _event(**context: object) -> Event:
    return Event(
        event_id="evt-1",
        channel="gitlab-pr-review",
        type="merge_request",
        source="gitlab",
        payload={"text": "hello", "surrogate": "\ud800"},
        context={"gitlab": context},
    )


def _script(script: str, *, env: dict[str, str] | None = None,
            secret_env: dict[str, object] | None = None, timeout: int = 5) -> ScriptBlock:
    return ScriptBlock.model_validate({
        "script": script,
        "env": env or {},
        "secretEnv": secret_env or {},
        "timeoutSeconds": timeout,
    })


def test_script_stdin_env_ack_and_cleanup(tmp_path: Path) -> None:
    async def scenario() -> None:
        output = tmp_path / "result"
        cfg = _script(
            'IFS= read -r payload; printf "%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n%s\\n" "$payload" "$ACH_EVENT_PROJECT_PATH" "$ACH_EVENT_MR_IID" "$GITLAB_TOKEN" "${UNSELECTED_SECRET-unset}" "$HOME" "$ACH_WORKSPACE" > "$OUT"; printf "%s|%s|%s|%s\\n" "$ACH_EVENT_HEAD_SHA" "$ACH_EVENT_KIND" "$ACH_EVENT_TARGET_TYPE" "${ACH_SESSION_KEY-unset}" >> "$OUT"; printf "%s|%s|%s|%s|%s|%s" "$ACH_EVENT_ID" "$ACH_CHANNEL" "${ACH_SESSION_KEY-unset}" "$ACH_WORKSPACE" "$ACH_HANDOFF_DIR" "$HOME" >> "$OUT"',
            env={"OUT": str(output), "PATH": "/usr/bin:/bin"},
            secret_env={"GITLAB_TOKEN": {"env": "SELECTED_TOKEN"}},
        )
        cfg.env["ACH_EVENT_MR_IID"] = "operator-value"
        cfg.env.update({
            "ACH_EVENT_ID": "operator-event",
            "ACH_CHANNEL": "operator-channel",
            "ACH_WORKSPACE": "/operator-workspace",
            "ACH_HANDOFF_DIR": "/operator-handoff",
            "HOME": "/operator-home",
        })
        started = asyncio.Event()
        await run_webhook_script(
            cfg,
            _event(projectId="42", projectPath="team/service", mergeRequestIid="7", headSha="abc123"),
            datetime.now(UTC) + timedelta(seconds=10),
            environ={"PATH": "/bin:/usr/bin", "SELECTED_TOKEN": "selected-secret", "UNSELECTED_SECRET": "must-not-leak"},
            on_started=started.set,
        )
        assert started.is_set()
        lines = output.read_text().splitlines()
        assert lines[0] == json.dumps({"text": "hello", "surrogate": "\ud800"}, separators=(",", ":"))
        assert lines[1:5] == ["team/service", "7", "selected-secret", "unset"]
        assert not Path(lines[5]).exists(), "private HOME should be removed after completion"
        assert not Path(lines[6]).exists(), "script workspace should be removed after completion"
        assert lines[7] == "abc123|merge_request|mr|unset"
        identity = lines[8].split("|")
        assert identity[:3] == ["evt-1", "gitlab-pr-review", "unset"]
        assert identity[3] == identity[4]
        assert identity[5] == lines[5]

    asyncio.run(scenario())


def test_script_started_signal_precedes_gated_process_exit(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "after-release"
        gate = tmp_path / "gate"
        cfg = _script(f'while [ ! -e "{gate}" ]; do sleep 0.01; done; : > "{marker}"')
        started = asyncio.Event()
        task = asyncio.create_task(run_webhook_script(
            cfg, _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
            datetime.now(UTC) + timedelta(seconds=10), environ={"PATH": "/bin:/usr/bin"},
            on_started=started.set,
        ))
        await asyncio.wait_for(started.wait(), 2)
        assert not task.done()
        gate.touch()
        await task
        assert marker.exists()

    asyncio.run(scenario())


def test_script_deadline_cancellation_kills_child_and_cleans_workspace(tmp_path: Path) -> None:
    async def scenario() -> None:
        child_file = tmp_path / "child.pid"
        workspace_file = tmp_path / "workspace.path"
        cfg = _script(f'printf "%s" "$ACH_WORKSPACE" > "$WORKSPACE_FILE"; sleep 30 & echo $! > "{child_file}"; wait',
                      env={"WORKSPACE_FILE": str(workspace_file)}, timeout=30)
        started = asyncio.Event()
        task = asyncio.create_task(run_webhook_script(
            cfg, _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
            datetime.now(UTC) + timedelta(seconds=0.2), environ={"PATH": "/bin:/usr/bin"},
            on_started=started.set,
        ))
        await asyncio.wait_for(started.wait(), 2)
        with pytest.raises(RuntimeError, match="timed out"):
            await task
        for _ in range(100):
            if child_file.exists():
                break
            await asyncio.sleep(0.01)
        child = int(child_file.read_text())
        for _ in range(100):
            try:
                state = Path(f"/proc/{child}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("script descendant remained alive after deadline cleanup")
        assert not Path(workspace_file.read_text()).exists()

    asyncio.run(scenario())


def test_script_spawn_failure_never_signals_started(monkeypatch, tmp_path: Path) -> None:
    async def fail_spawn(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fail_spawn)

    async def scenario() -> None:
        started = False

        def mark_started() -> None:
            nonlocal started
            started = True

        with pytest.raises(RuntimeError):
            await run_webhook_script(
                _script(":"), _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC) + timedelta(seconds=10), environ={"PATH": "/bin"},
                on_started=mark_started,
            )
        assert not started

    asyncio.run(scenario())


def test_script_pre_spawn_expiry_never_signals_started(tmp_path: Path) -> None:
    async def scenario() -> None:
        started = False

        def mark_started() -> None:
            nonlocal started
            started = True

        with pytest.raises(TimeoutError):
            await run_webhook_script(
                _script(":"), _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC), environ={"PATH": "/bin"}, on_started=mark_started,
            )
        assert not started

    asyncio.run(scenario())


def test_script_setup_expiry_does_not_spawn_or_leave_workspace(monkeypatch, tmp_path: Path) -> None:
    created: list[Path] = []
    real_mkdtemp = scripts.tempfile.mkdtemp
    real_event_env = scripts._event_env
    spawn_calls = 0

    def capture_workspace(*args, **kwargs) -> str:
        path = real_mkdtemp(*args, **kwargs)
        created.append(Path(path))
        return path

    def delayed_event_env(event: Event) -> dict[str, str]:
        time.sleep(0.08)
        return real_event_env(event)

    real_spawn = asyncio.create_subprocess_exec

    async def count_spawn(*args, **kwargs):
        nonlocal spawn_calls
        spawn_calls += 1
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(scripts.tempfile, "mkdtemp", capture_workspace)
    monkeypatch.setattr(scripts, "_event_env", delayed_event_env)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", count_spawn)

    async def scenario() -> None:
        started = False

        def mark_started() -> None:
            nonlocal started
            started = True

        with pytest.raises(TimeoutError, match="deadline"):
            await run_webhook_script(
                _script(":"), _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC) + timedelta(seconds=0.03), environ={"PATH": "/bin"},
                on_started=mark_started,
            )
        assert not started

    asyncio.run(scenario())
    assert spawn_calls == 0, "expired setup must be rejected before subprocess creation"
    assert any(path.name.startswith("webhook-script-") for path in created)
    assert all(not path.exists() for path in created), "expired setup must clean temporary directories"


def test_script_late_spawn_handle_is_killed_without_started(monkeypatch, tmp_path: Path) -> None:
    real_spawn = asyncio.create_subprocess_exec
    late_processes: list[asyncio.subprocess.Process] = []
    child_pid_file = tmp_path / "late-child.pid"
    script = f'echo $$ > "{child_pid_file}"; sleep 30'

    async def cancellation_resistant_delayed_spawn(*args, **kwargs):
        try:
            await asyncio.sleep(0.10)
        except asyncio.CancelledError:
            # Model an OS spawn call that returns its handle after the deadline.
            pass
        proc = await real_spawn(*args, **kwargs)
        late_processes.append(proc)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", cancellation_resistant_delayed_spawn)

    async def scenario() -> None:
        started = False

        def mark_started() -> None:
            nonlocal started
            started = True

        with pytest.raises((TimeoutError, RuntimeError)):
            await run_webhook_script(
                _script(script), _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC) + timedelta(seconds=0.04), environ={"PATH": "/bin:/usr/bin"},
                on_started=mark_started,
            )
        assert not started, "a process handle acquired after expiry must not signal Started"

    asyncio.run(scenario())
    assert len(late_processes) == 1
    assert late_processes[0].returncode is not None, "late process group must be killed and joined"


def test_script_spawn_time_reduces_communication_budget(monkeypatch) -> None:
    real_spawn = asyncio.create_subprocess_exec

    async def delayed_spawn(*args, **kwargs):
        await asyncio.sleep(0.18)
        return await real_spawn(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", delayed_spawn)

    async def scenario() -> None:
        started = asyncio.Event()
        with pytest.raises(RuntimeError, match="timed out"):
            await run_webhook_script(
                _script("sleep 0.30", timeout=2),
                _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC) + timedelta(seconds=0.36), environ={"PATH": "/bin:/usr/bin"},
                on_started=started.set,
            )
        assert started.is_set(), "on-time spawn must preserve immediate Started"

    asyncio.run(scenario())


def test_script_cancellation_kills_process_group_and_joins(tmp_path: Path) -> None:
    async def scenario() -> None:
        child_file = tmp_path / "child.pid"
        workspace_file = tmp_path / "workspace.path"
        cfg = _script(f'printf "%s" "$ACH_WORKSPACE" > "$WORKSPACE_FILE"; sleep 30 & echo $! > "{child_file}"; wait',
                      env={"WORKSPACE_FILE": str(workspace_file)})
        started = asyncio.Event()
        task = asyncio.create_task(run_webhook_script(
            cfg, _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
            datetime.now(UTC) + timedelta(seconds=10), environ={"PATH": "/bin:/usr/bin"},
            on_started=started.set,
        ))
        await asyncio.wait_for(started.wait(), 2)
        for _ in range(100):
            if child_file.exists():
                break
            await asyncio.sleep(0.01)
        assert child_file.exists(), "script process did not receive stdin before cancellation"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        child = int(child_file.read_text())
        for _ in range(100):
            try:
                state = Path(f"/proc/{child}/stat").read_text().split()[2]
            except FileNotFoundError:
                break
            if state == "Z":
                break
            await asyncio.sleep(0.01)
        else:
            raise AssertionError("script descendant remained alive after cancellation")
        assert not Path(workspace_file.read_text()).exists()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("payload", "kind", "target", "mr_iid", "issue_iid", "head_sha", "project_id"),
    [
        (
            {"object_kind": "merge_request", "project": {"id": 42, "path_with_namespace": "team/service"},
             "object_attributes": {"iid": 7, "last_commit": {"id": "abc123"}}, "user": {"username": "alice"}},
            "merge_request", "mr", "7", "", "abc123", "42",
        ),
        (
            {"object_kind": "note", "project": {"id": 42, "path_with_namespace": "team/service"},
             "object_attributes": {"iid": 99, "noteable_type": "MergeRequest"},
             "merge_request": {"iid": 7, "last_commit": {"id": "def456"}}, "user": {"username": "alice"}},
            "note", "mr", "7", "", "def456", "42",
        ),
        (
            {"object_kind": "issue", "project": {"id": 42, "path_with_namespace": "team/service"},
             "object_attributes": {"iid": 3}, "user": {"username": "alice"}},
            "issue", "issue", "", "3", "", "42",
        ),
        (
            {"object_kind": "push", "project_id": 42,
             "project": {"path_with_namespace": "team/service"}, "user_username": "alice"},
            "push", "", "", "", "", "42",
        ),
    ],
)
def test_real_gitlab_normalization_reaches_script_environment(
    tmp_path: Path,
    payload: dict[str, object],
    kind: str,
    target: str,
    mr_iid: str,
    issue_iid: str,
    head_sha: str,
    project_id: str,
) -> None:
    async def scenario() -> None:
        channel = ChannelConfig.model_validate({
            "name": "gitlab-script",
            "type": "webhook-script",
            "source": "gitlab",
            "webhook": {"auth": {"type": "none"}, "gitlabEvents": ["merge_request", "note", "issue", "push"]},
            "script": {"script": ":"},
        })
        event = normalize_gitlab(
            payload, {"X-Gitlab-Event-UUID": f"delivery-{kind}"}, channel,
        )
        assert event is not None
        output = tmp_path / f"{kind}-env"
        cfg = _script(
            'printf "%s|%s|%s|%s|%s|%s|%s|%s" "$ACH_EVENT_KIND" "${ACH_EVENT_TARGET_TYPE-}" "${ACH_EVENT_MR_IID-}" "${ACH_EVENT_ISSUE_IID-}" "${ACH_EVENT_HEAD_SHA-}" "$ACH_EVENT_PROJECT_ID" "$ACH_EVENT_PROJECT_PATH" "${ACH_SESSION_KEY-unset}" > "$OUT"',
            env={"OUT": str(output)},
        )
        await run_webhook_script(
            cfg, event, datetime.now(UTC) + timedelta(seconds=10),
            environ={"PATH": "/bin:/usr/bin"}, on_started=lambda: None,
        )
        assert output.read_text() == f"{kind}|{target}|{mr_iid}|{issue_iid}|{head_sha}|{project_id}|team/service|unset"

    asyncio.run(scenario())


def test_script_stderr_tail_is_bounded_and_redacts_credentials(tmp_path: Path, capfd) -> None:
    async def scenario() -> str:
        output = tmp_path / "result"
        cfg = _script(
            "printf '%5000s' '' | tr ' ' x >&2; printf 'TAIL ek_secret GITLAB_TOKEN=%s\\n' \"$GITLAB_TOKEN\" >&2; exit 17",
            env={"OUT": str(output)},
            secret_env={"GITLAB_TOKEN": {"env": "TOKEN_ALIAS"}},
        )
        with pytest.raises(RuntimeError) as caught:
            await run_webhook_script(
                cfg, _event(projectId="42", projectPath="team/service", mergeRequestIid="7"),
                datetime.now(UTC) + timedelta(seconds=10),
                environ={"PATH": "/bin:/usr/bin", "TOKEN_ALIAS": "alias-secret", "GITLAB_TOKEN": "live-token"},
                on_started=lambda: None,
            )
        return str(caught.value)

    message = asyncio.run(scenario())
    tail = message.split(": ", 1)[1]
    assert len(tail.encode()) <= 4096
    assert "TAIL" in tail
    assert "[REDACTED]" in tail
    assert "alias-secret" not in message
    assert "live-token" not in message
    captured = capfd.readouterr()
    assert "truncated=True" in captured.out
    assert "alias-secret" not in captured.out
