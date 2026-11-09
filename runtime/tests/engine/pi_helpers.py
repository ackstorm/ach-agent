# SPDX-License-Identifier: Apache-2.0
"""Fixtures and builders shared by the pi engine tests."""

import json
import os
from pathlib import Path

import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.engine import pi as pi_module
from ach_runtime.engine.base import EngineInvocation
from ach_runtime.engine.pi import PiEngine

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
