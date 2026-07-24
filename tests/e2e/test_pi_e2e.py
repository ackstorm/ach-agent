# SPDX-License-Identifier: Apache-2.0
"""Real Pi subprocess e2e with a hermetic OpenAI-compatible model proxy."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path
from typing import Any, NoReturn

import aiohttp.web
import pytest

from ach_agent.engine.base.driver import EngineConfig
from ach_agent.engine.pi.driver import PiDriver
from ach_agent.engine.pi.protocol import EV_EOF


def _missing_pi_dependency(name: str) -> NoReturn:
    if os.environ.get("CI"):
        # GitHub Actions sets CI=true on every runner (including e2e-pi). Missing either
        # real-subprocess dependency is an install regression, never a valid CI skip.
        raise RuntimeError(
            f"{name} not installed — CI must run tests/e2e/test_pi_e2e.py, not skip it"
        )
    pytest.skip(f"{name} not installed", allow_module_level=True)


def _require_pi_binary() -> str:
    path = shutil.which("pi")
    if path is None:
        _missing_pi_dependency("pi binary")
    return path


def _require_pi_mcp_adapter() -> str:
    candidates = [
        os.environ.get("PI_MCP_ADAPTER_PATH", ""),
        "/opt/pi-mcp-adapter/node_modules/pi-mcp-adapter",
        str(Path.home() / ".pi/agent/npm/node_modules/pi-mcp-adapter"),
    ]
    path = next(
        (candidate for candidate in candidates if candidate and Path(candidate).is_dir()),
        None,
    )
    if path is None:
        _missing_pi_dependency("pi-mcp-adapter")
    return path


PI = _require_pi_binary()
PI_MCP_ADAPTER_PATH = _require_pi_mcp_adapter()


@pytest.fixture(autouse=True)
def _pinned_pi_version() -> None:
    """Every test in this module runs the pinned 0.82.0 — a version drift silently
    changing RPC/CLI behavior must fail loudly here, not pass on a different Pi."""
    import subprocess

    result = subprocess.run([PI, "--version"], capture_output=True, text=True, check=True)
    version = result.stdout.strip()
    assert version == "0.82.0", f"pi --version = {version!r}, expected the pinned 0.82.0"


async def _rpc_roundtrip(client: Any, command: str, **payload: Any) -> dict[str, Any]:
    """Send a request and return its data, asserting id AND command match (not just id) —
    a matching id with a mismatched command would mean the client desynced from the
    protocol, and a silent pass there would hide that."""
    request_id = f"e2e-{command}"
    await client.send({**payload, "type": command, "id": request_id})

    async def _wait_for_response() -> dict[str, Any]:
        while True:
            event = await client.recv()
            if event.get("type") == EV_EOF:
                pytest.fail(f"Received EV_EOF while waiting for {command} response")
            if event.get("type") != "response" or event.get("id") != request_id:
                continue
            assert event.get("command") == command, (
                f"response command mismatch: expected {command!r}, got {event.get('command')!r}"
            )
            assert event.get("success") is True, f"{command} failed: {event.get('error')}"

            data = event.get("data")
            if data is None:
                return {}
            assert isinstance(data, dict), f"response data is not a dict: {data!r}"
            return data

    try:
        async with asyncio.timeout(5.0):
            return await _wait_for_response()
    except TimeoutError:
        pytest.fail(f"Timed out waiting for {command} response")


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
    adapter_path = PI_MCP_ADAPTER_PATH
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


async def test_pi_reasoning_model_reports_resolved_thinking_level(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ACH_TOKEN", "ek_e2e_secret_marker")
    monkeypatch.setenv("ACH_API_KEY", "ek_api_secret_marker")
    adapter_path = PI_MCP_ADAPTER_PATH
    runner, base_url = await _start_stub_server()
    server: Any = None
    try:
        cfg = EngineConfig(
            engine_type="pi",
            binary_path=PI,
            home=str(tmp_path / "home"),
            work_dir=str(tmp_path / "workspace"),
            model="stub-reasoning-model",
            model_type="openai",
            model_base_url=f"{base_url}/v1",
            pi_mcp_adapter_path=adapter_path,
            thinking_enabled=True,
            thinking_effort="high",
        )
        driver = PiDriver()
        server = await driver.launch(cfg, "e2e-reasoning-key")

        # Known-good config, restated: provider/model chosen explicitly (--provider/
        # --model, asserted implicitly by a successful launch below), dummy local-proxy
        # key + localhost-only baseUrl (never the ek_ or a real ACH endpoint).
        models_doc = json.loads((server.ephemeral_home / "models.json").read_text(encoding="utf-8"))
        provider_doc = next(iter(models_doc["providers"].values()))
        assert provider_doc["apiKey"] == "$PI_LOCAL_PROXY_API_KEY"
        assert provider_doc["baseUrl"].startswith("http://127.0.0.1:")
        assert provider_doc["models"][0]["reasoning"] is True

        client: Any = server._client
        await _rpc_roundtrip(client, "new_session")
        state = await _rpc_roundtrip(client, "get_state")

        assert state.get("thinkingLevel") == "high"
        model_info = state.get("model") or {}
        assert model_info.get("reasoning") is True

        for name in ("models.json", "settings.json", "mcp.json"):
            blob = (server.ephemeral_home / name).read_text(encoding="utf-8")
            assert "ek_e2e_secret_marker" not in blob, f"ek leaked into {name}"
            assert "ek_api_secret_marker" not in blob, f"ek leaked into {name}"

        process = server._process
        assert process is not None
        environ = Path(f"/proc/{process.pid}/environ").read_bytes().split(b"\0")
        assert all(b"ACH_TOKEN" not in item for item in environ)
        assert all(b"ek_e2e_secret_marker" not in item for item in environ)
    finally:
        if server is not None:
            await PiDriver().stop(server)
        await runner.cleanup()
