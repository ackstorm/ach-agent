# SPDX-License-Identifier: MIT
"""Contract of the pi engine: every case runs on PiG and on upstream pi."""

import json
import os
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine import pi as pi_module
from ach_runtime.engine.base import EngineInvocation, NativeLaunchFailed
from ach_runtime.engine.pi import PiEngine
from fake_facades import fake_facades

FIXTURE = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


@pytest.fixture(params=["PIG_BIN", "UPSTREAM_PI_BIN"])
def binary(request, monkeypatch) -> str:
    path = os.environ[request.param]
    monkeypatch.setattr(pi_module, "PI_BIN", path)
    return path


@pytest.fixture
async def engine(binary):
    instance = PiEngine()
    yield instance
    await instance.close()


def _public(*, max_steps: int = 50):
    config = RuntimeConfig.model_validate(json.loads(FIXTURE.read_text(encoding="utf-8")))
    public = public_config(config, engine_env=config.engine.env)
    return public.model_copy(update={
        "model": public.model.model_copy(update={"type": "openai", "params": {}}),
        "limits": public.limits.model_copy(update={"max_steps": max_steps}),
    })


def _invocation(tmp_path: Path, model_url: str, *, invocation_id: str = "inv-1", max_steps: int = 50, mcp: dict | None = None) -> EngineInvocation:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    public = _public(max_steps=max_steps)
    return EngineInvocation(
        invocation_id=invocation_id, session_ref="session-1", workspace_dir=workspace,
        prompt="reply", public=public, engine_env=dict(public.engine.env),
        mcp=mcp or {}, provider_base_url_override=model_url,
    )


async def test_turn_completes_with_result_and_usage(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        receipt = await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert receipt.native_session_id
        assert await engine.wait_settled("inv-1") == "completed"
        assert engine.result("inv-1") == "pong"
        usage = engine.usage("inv-1")
        assert usage.source == "pi" and usage.steps == 1 and usage.reasoning is None


async def test_max_steps_aborts_as_failed_with_no_result(tmp_path, engine) -> None:
    async with fake_facades("tool-loop") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url, max_steps=3), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.wait_settled("inv-1") == "failed"
        assert engine.result("inv-1") is None
        assert 3 <= engine.usage("inv-1").steps <= 4  # soft limit: overshoots by at most one turn


async def test_provider_error_fails(tmp_path, engine) -> None:
    async with fake_facades("model-error") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        assert await engine.wait_settled("inv-1") == "failed"


async def test_mcp_tools_reach_the_model(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        mcp = {"demo": {"url": fakes.mcp_url, "headers": {}}}
        await engine.start(_invocation(tmp_path, fakes.model_url, mcp=mcp), home=tmp_path / "home", scratch=tmp_path / "scratch")
        await engine.wait_settled("inv-1")
        records = (await fakes.control({"command": "capture"}))["records"]
        names = [tool["function"]["name"] for tool in records[0]["body"]["tools"]]
        assert any(name.startswith("mcp__demo__") for name in names), names


async def test_progress_advances_during_a_turn(tmp_path, engine) -> None:
    async with fake_facades("text") as fakes:
        await engine.start(_invocation(tmp_path, fakes.model_url), home=tmp_path / "home", scratch=tmp_path / "scratch")
        started = engine.last_progress("inv-1")
        await engine.wait_settled("inv-1")
        assert engine.last_progress("inv-1") >= started


async def test_missing_binary_names_the_engine(tmp_path, engine, monkeypatch) -> None:
    monkeypatch.setattr(pi_module, "PI_BIN", "/nonexistent/pi")
    with pytest.raises(NativeLaunchFailed, match="pi engine"):
        await engine.start(_invocation(tmp_path, "http://127.0.0.1:9/v1"), home=tmp_path / "home", scratch=tmp_path / "scratch")
