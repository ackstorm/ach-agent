# SPDX-License-Identifier: MIT
"""Focused native managed-config refresh tests for a warm OpenCode session."""

from __future__ import annotations

import json
from pathlib import Path

import asyncio
import pytest

from ach_runtime.config.public import public_config
from ach_runtime.config.schema import RuntimeConfig, ThinkingBlock
from ach_runtime.engine.opencode import EngineInvocation, NativeLaunchFailed, OpenCodeEngine, _managed_fingerprint
from fake_facades import fake_facades

FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"


def test_effective_refresh_identity_uses_content_and_approved_environment():
    content = {"plugins": [{"options": {"fixture": {"maxSteps": 2, "model": "a"}}}]}
    env = {"HOME": "/session", "OPENCODE_SERVER_PASSWORD": "first", "AWS_REGION": "eu-central-1"}
    same = json.loads(json.dumps(content))
    assert _managed_fingerprint(content, env) == _managed_fingerprint(
        same, {**env, "OPENCODE_SERVER_PASSWORD": "rotated"}
    )
    changed = json.loads(json.dumps(content))
    changed["plugins"][0]["options"]["fixture"]["maxSteps"] = 1
    assert _managed_fingerprint(content, env) != _managed_fingerprint(changed, env)
    assert _managed_fingerprint(content, env) != _managed_fingerprint(content, {**env, "AWS_REGION": "us-east-1"})


@pytest.fixture
def public():
    wire = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    config = RuntimeConfig.model_validate(wire)
    return public_config(config, engine_env=config.engine.env)


@pytest.fixture
async def engine():
    instance = OpenCodeEngine()
    try:
        yield instance
    finally:
        for session in list(instance._sessions.values()):
            await session._kill(confirm_timeout=5.0)


def _public_with(public, *, temperature: float, max_steps: int, effort: str = "high", model_type: str | None = None):
    model_updates = {
        "params": {**public.model.params, "temperature": temperature},
        "thinking": ThinkingBlock(enabled=True, effort=effort),
    }
    if model_type is not None:
        model_updates["type"] = model_type
    model = public.model.model_copy(update={
        **model_updates,
    })
    limits = public.limits.model_copy(update={"max_steps": max_steps})
    return public.model_copy(update={"model": model, "limits": limits})


def _invocation(public, tmp_path: Path, *, invocation_id: str, session_ref: str, model_url: str, prompt: str):
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return EngineInvocation(
        invocation_id=invocation_id,
        session_ref=session_ref,
        workspace_dir=workspace,
        prompt=prompt,
        public=public,
        engine_env=dict(public.engine.env),
        provider_base_url_override=model_url,
    )






