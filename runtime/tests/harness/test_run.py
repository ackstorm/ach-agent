# SPDX-License-Identifier: MIT
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

# Stands in for `opencode2 mini`: proves the attach target answers with its password.
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
    manifest.write_text("ach: {environment: engineering-prod}\nmodel: {name: gemini.gemini-flash-latest, type: gemini}\n")
    workspace = tmp_path / "project"
    workspace.mkdir()
    rc = await run_local(
        manifest, workspace=workspace, name="smoke", data_dir=tmp_path / "data",
        tui=[sys.executable, "-c", FAKE_TUI], ach_transport=httpx.MockTransport(_catalog),
    )
    assert rc == 0
    assert workspace.is_dir()
