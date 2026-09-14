from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
import uvicorn

from ach_agent.boot.channels_api import create_channels_app
from ach_agent.boot.completions import CompletionRegistry
from ach_agent.boot.execution_client import ExecutionClient
from ach_agent.boot.ipc import bind_listener, engine_socket_path
from ach_agent.channels.client import ChannelsClient
from ach_agent.config.schema import ChannelSourceConfig
from ach_agent.execution.app import create_execution_app
from ach_agent.execution.service import ExecutionService


@pytest.mark.asyncio
async def test_execution_client_uses_real_unix_socket(fake_driver, tmp_path: Path) -> None:
    service = ExecutionService(fake_driver, {})
    listener = bind_listener(engine_socket_path(tmp_path))
    server = uvicorn.Server(
        uvicorn.Config(create_execution_app(service), log_level="error", lifespan="off")
    )
    task = asyncio.create_task(server.serve(sockets=[listener]))
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(0.01)
        client = ExecutionClient(socket_path=str(engine_socket_path(tmp_path)), controller_id="c")
        try:
            hello = await client.connect()
            assert hello.instance_id == service.instance_id
            assert (await client.control_client.get("/execution/v1/health")).status_code == 200
        finally:
            await client.close()
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=5)
        listener.close()
        engine_socket_path(tmp_path).unlink(missing_ok=True)


@pytest.mark.asyncio
async def test_channels_client_fetches_typed_source_projection() -> None:
    async def admission(_event):
        raise AssertionError("config fetch must not submit an event")

    source = ChannelSourceConfig.model_validate(
        {
            "name": "incoming",
            "type": "webhook",
            "source": "generic",
            "webhook": {"auth": {"type": "none"}},
        }
    )
    app = create_channels_app(
        CompletionRegistry(admission),
        b"test-key",
        agent="agent-a",
        channels=[source.name],
        source_configs=[source],
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://ach-internal") as http:
        client = ChannelsClient("http://ach-internal", b"test-key", http_client=http)
        inputs = await client.fetch_config()
    assert inputs.agent_name == "agent-a"
    assert inputs.channels == [source]
