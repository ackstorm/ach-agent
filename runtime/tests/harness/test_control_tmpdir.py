# SPDX-License-Identifier: MIT
"""Control-process writers all resolve under TMPDIR (read-only root filesystem + one emptyDir)."""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.config.schema import ScriptBlock
from ach_runtime.harness import lifecycle
from ach_runtime.harness.admission import AdmissionTicket
from ach_runtime.harness.handoff import run_handoff
from ach_runtime.harness.scripts import run_webhook_script
from ach_runtime.storage import s3

_PROBE = 'printf "%s|%s|%s" "$(mktemp)" "$HOME" "$PWD" > "$OUT"'


def test_control_writers_stay_under_tmpdir(monkeypatch, tmp_path: Path) -> None:
    tmp, cwd, home = tmp_path / "tmp", tmp_path / "cwd", tmp_path / "home"
    for path in (tmp, cwd, home):
        path.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(tempfile, "tempdir", None)  # re-resolve from TMPDIR
    monkeypatch.chdir(cwd)
    environ = {"PATH": "/bin:/usr/bin", "TMPDIR": str(tmp), "HOME": str(home)}

    async def scenario() -> list[str]:
        loop = asyncio.get_running_loop()
        routed = RoutedEvent(Event("evt", "ch", "push", "webhook"), "key", "sess")
        ticket = AdmissionTicket("inv", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "w" * 64, "s" * 64,
                                 routed, datetime.now(UTC) + timedelta(seconds=20),
                                 loop.create_future(), loop.create_future())
        # control._run_handoff hands run_handoff a workspace inside a TemporaryDirectory.
        with tempfile.TemporaryDirectory(prefix="ach-handoff-") as temporary:
            handoff_out = Path(temporary) / "out"
            cfg = SimpleNamespace(script=_PROBE, env={"OUT": str(handoff_out)}, secret_env={},
                                  timeout_seconds=5, destination="handoff", scope="event")
            await run_handoff(cfg, routed.event, ticket, Path(temporary) / "workspace", environ=environ)
            handoff = handoff_out.read_text()
        script_out = tmp / "script-out"
        script = ScriptBlock.model_validate({"script": _PROBE, "env": {"OUT": str(script_out)}, "timeoutSeconds": 5})
        gitlab = Event("evt", "ch", "merge_request", "gitlab", {}, {"gitlab": {
            "projectId": "42", "projectPath": "team/service", "mergeRequestIid": "7", "headSha": "abc"}})
        await run_webhook_script(script, gitlab, datetime.now(UTC) + timedelta(seconds=10),
                                 environ=environ, on_started=lambda: None)
        return handoff.split("|") + script_out.read_text().split("|")

    written = asyncio.run(scenario())
    written += [str(lifecycle._new_close_archive()), str(s3._new_staging_path())]
    with tempfile.TemporaryDirectory(prefix="ach-context-") as temporary:  # control._transfer_context
        written.append(temporary)

    for path in written:
        assert Path(path).is_relative_to(tmp), path
    assert os.listdir(cwd) == [] and os.listdir(home) == []
