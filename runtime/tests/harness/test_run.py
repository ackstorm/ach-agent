# SPDX-License-Identifier: Apache-2.0
import json
import shutil
import sys

import httpx
import pytest

from ach_runtime.engine.opencode import OPENCODE_BIN
from ach_runtime.harness.run import run_local

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which(OPENCODE_BIN) is None, reason="needs the opencode binary"),
]

# Stands in for the opencode TUI: proves the attach target answers with its password.
FAKE_TUI = r"""
import base64, os, sys, urllib.request
args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
token = base64.b64encode(f"opencode:{os.environ['OPENCODE_SERVER_PASSWORD']}".encode()).decode()
req = urllib.request.Request(f"{args['--server']}/api/session/{args['--session']}",
                             headers={"authorization": f"Basic {token}"})
sys.exit(0 if urllib.request.urlopen(req, timeout=10).status == 200 else 1)
"""


def _catalog(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "http://127.0.0.1:9/v1"}],
                        "mcpServers": [], "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


async def test_run_attaches_a_client_to_the_tui_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("ACH_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ACH_URL", "https://ach.example")
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, api: gemini}\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    rc = await run_local(
        manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data",
        tui=[sys.executable, "-c", FAKE_TUI], ach_transport=httpx.MockTransport(_catalog),
    )
    assert rc == 0
    assert workspace.is_dir()


async def test_run_reports_why_the_warmup_run_failed(tmp_path, monkeypatch) -> None:
    # A run that fails before opencode starts (an environment MCP server ACH refuses) must
    # surface its error, not "timed out waiting for the TUI session to start".
    from ach_runtime.harness.run import RunError

    async def unavailable(_catalog):
        raise RuntimeError("mcp tools unavailable: mcp-x (HTTPStatusError: 403 Forbidden)")

    monkeypatch.setattr("ach_runtime.execution.service._require_mcp_tools", unavailable)
    monkeypatch.setenv("ACH_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ACH_URL", "https://ach.example")
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, api: gemini}\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(RunError, match="mcp tools unavailable: mcp-x"):
        await run_local(
            manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data",
            tui=[sys.executable, "-c", "raise SystemExit(0)"], ach_transport=httpx.MockTransport(_catalog),
        )


async def test_run_gives_a_clean_env_tui_only_the_engines_env(tmp_path, monkeypatch) -> None:
    from dataclasses import replace

    from ach_runtime.engine.opencode import OpenCodeEngine

    real = OpenCodeEngine.tui_command

    async def clean(self, session_ref):
        command = await real(self, session_ref)
        return None if command is None else replace(command, env={"ENGINE_ONLY": "1"}, clean_env=True)

    monkeypatch.setattr(OpenCodeEngine, "tui_command", clean)
    monkeypatch.setenv("ACH_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ACH_URL", "https://ach.example")
    monkeypatch.setenv("MANIFEST_SECRET", "s3cret-value")
    manifest = tmp_path / "agent.yaml"
    manifest.write_text(
        "ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, api: gemini}\n"
        "mcpServers: [{name: extra, url: 'http://127.0.0.1:9/mcp', headers: {X-Key: {env: MANIFEST_SECRET}}}]\n"
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    dump = tmp_path / "env.json"
    script = f"import json, os; json.dump(dict(os.environ), open({str(dump)!r}, 'w'))"
    rc = await run_local(
        manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data",
        tui=[sys.executable, "-c", script], ach_transport=httpx.MockTransport(_catalog),
    )
    assert rc == 0
    seen = json.loads(dump.read_text())
    # The interpreter may add its own locale variables; nothing of the harness's environment passes.
    assert {k: v for k, v in seen.items() if not k.startswith("LC_")} == {"ENGINE_ONLY": "1"}


def _fake_admit(monkeypatch, state: str, *, text: str | None = None, error: str | None = None) -> list[str]:
    """Replace admission with a ticket that is already finished, and record the request text."""
    import asyncio

    from ach_runtime.execution.wire import RunStatus
    from ach_runtime.harness.admission import Admission

    seen: list[str] = []

    class Ticket:
        def __init__(self) -> None:
            loop = asyncio.get_running_loop()
            self.started, self.completed = loop.create_future(), loop.create_future()
            self.started.set_result(None)
            self.completed.set_result(RunStatus.model_validate({
                "invocationId": "inv-1", "channel": "tui", "workspaceRef": "w", "sessionRef": "s",
                "runId": "r", "configVersion": "c", "deadlineAt": "2030-01-01T00:00:00Z",
                "state": state, "error": error, "result": None if text is None else {"text": text},
            }))

    def admit(self, routed):
        seen.append(routed.event.payload["text"])
        return Ticket()

    monkeypatch.setattr(Admission, "admit", admit)
    return seen


async def test_run_event_prints_only_the_reply(tmp_path, monkeypatch, capfd) -> None:
    seen = _fake_admit(monkeypatch, "completed", text="pong")
    monkeypatch.setenv("ACH_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ACH_URL", "https://ach.example")
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, api: gemini}\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    rc = await run_local(
        manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data", log_path=tmp_path / "run.log",
        event_text="ping", ach_transport=httpx.MockTransport(_catalog),
    )
    assert rc == 0
    assert seen == ["ping"]
    assert capfd.readouterr().out == "pong\n"  # no access log, no harness line


async def test_run_event_raises_the_run_error(tmp_path, monkeypatch, capfd) -> None:
    from ach_runtime.harness.run import RunError

    _fake_admit(monkeypatch, "failed", error="model refused")
    monkeypatch.setenv("ACH_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("ACH_URL", "https://ach.example")
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, api: gemini}\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    with pytest.raises(RunError, match="the run failed: model refused"):
        await run_local(
            manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data", log_path=tmp_path / "run.log",
            event_text="ping", ach_transport=httpx.MockTransport(_catalog),
        )
    assert capfd.readouterr().out == ""
