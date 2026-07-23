# SPDX-License-Identifier: Apache-2.0
"""Real Pi subprocess e2e with a hermetic OpenAI-compatible model proxy."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import aiohttp.web
import pytest

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.driver import PiDriver

PI = shutil.which("pi")
if PI is None:
    pytest.skip("pi binary not installed", allow_module_level=True)


async def _chat_completions(_request: aiohttp.web.Request) -> aiohttp.web.StreamResponse:
    response = aiohttp.web.StreamResponse(
        status=200,
        headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"},
    )
    await response.prepare(_request)
    chunks = ["hello", " from real pi"]
    for index, chunk in enumerate(chunks):
        delta: dict[str, str] = {"content": chunk}
        if index == 0:
            delta["role"] = "assistant"
        payload = {
            "id": "stub-completion",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": delta, "finish_reason": None}],
        }
        await response.write(f"data: {json.dumps(payload)}\n\n".encode())
    done = {
        "id": "stub-completion",
        "object": "chat.completion.chunk",
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    await response.write(f"data: {json.dumps(done)}\n\n".encode())
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def _mcp_health(_request: aiohttp.web.Request) -> aiohttp.web.Response:
    return aiohttp.web.json_response({"jsonrpc": "2.0", "id": 1, "result": {}})


async def _start_stub_server() -> tuple[aiohttp.web.AppRunner, str]:
    app = aiohttp.web.Application()
    app.router.add_post("/v1/chat/completions", _chat_completions)
    app.router.add_post("/mcp", _mcp_health)
    runner = aiohttp.web.AppRunner(app)
    await runner.setup()
    site = aiohttp.web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    sockets = site._server.sockets if site._server is not None else []
    port = sockets[0].getsockname()[1]
    return runner, f"http://127.0.0.1:{port}"


async def test_pi_turn_and_ek_never_on_disk_or_in_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_e2e_secret_marker")
    monkeypatch.setenv("ACH_API_KEY", "ek_api_secret_marker")
    adapter_candidates = [
        os.environ.get("PI_MCP_ADAPTER_PATH", ""),
        "/opt/pi-mcp-adapter",
        str(Path.home() / ".pi/agent/npm/node_modules/pi-mcp-adapter"),
    ]
    adapter_path = next((path for path in adapter_candidates if path and Path(path).is_dir()), None)
    if adapter_path is None:
        pytest.skip("pi-mcp-adapter not installed or pinned in the image")
    runner, base_url = await _start_stub_server()
    server: Any = None
    try:
        cfg = EngineConfig(
            engine_type="pi",
            binary_path=PI or "pi",
            home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "workspace"),
            model="stub-model",
            model_type="openai",
            model_base_url=f"{base_url}/v1",
            mcp_servers=[f"{base_url}/mcp"],
            pi_mcp_adapter_path=adapter_path,
        )
        driver = PiDriver()
        server = await driver.launch(cfg, "e2e-key")
        result = await driver.run_turn(
            server,
            conv_key="e2e-key",
            prompt="say hi",
            reuse=True,
            sessions={},
            on_text=None,
            on_tool=None,
            max_tool_calls=0,
            stats={},
        )
        assert result.text

        agent_dir = server.ephemeral_home
        for name in ("models.json", "settings.json", "mcp.json"):
            blob = (agent_dir / name).read_text(encoding="utf-8")
            assert "ek_e2e_secret_marker" not in blob, f"ek leaked into {name}"
            assert "ek_api_secret_marker" not in blob, f"ek leaked into {name}"

        process = server._process
        assert process is not None
        environ = Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
        assert all(b"ACH_TOKEN" not in item for item in environ)
        assert all(b"ACH_API_KEY" not in item for item in environ)
        assert all(b"ek_e2e_secret_marker" not in item for item in environ)
        assert all(b"ek_api_secret_marker" not in item for item in environ)
    finally:
        if server is not None:
            await PiDriver().stop(server)
        await runner.cleanup()
