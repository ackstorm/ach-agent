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


async def test_warm_refresh_preserves_sid_history_and_applies_new_settings(tmp_path, public, engine):
    async with fake_facades("text") as fakes:
        home = tmp_path / "home"
        scratch = tmp_path / "scratch"
        first = _invocation(
            _public_with(public, temperature=0.1, max_steps=2), tmp_path,
            invocation_id="refresh-a", session_ref="refresh-session", model_url=fakes.model_url,
            prompt="prior refresh history marker",
        )
        first_receipt = await engine.start(first, home=home, scratch=scratch)
        assert await asyncio.wait_for(engine.wait_settled("refresh-a"), timeout=20) == "completed"
        session = engine._sessions[first.session_ref]
        first_pid = session._proc.pid
        first_sid = first_receipt.native_session_id
        native_calls = []
        original_request = session.request

        async def tracked_request(method, path, *args, **kwargs):
            native_calls.append((method, path))
            return await original_request(method, path, *args, **kwargs)

        session.request = tracked_request
        changed = _invocation(
            _public_with(public, temperature=0.25, max_steps=1), tmp_path,
            invocation_id="refresh-b", session_ref="refresh-session", model_url=fakes.model_url,
            prompt="current refresh history marker",
        )
        second_receipt = await engine.start(changed, home=home, scratch=scratch)
        assert second_receipt.native_session_id == first_sid
        assert session._proc.pid != first_pid
        assert session.home == home
        assert str(changed.workspace_dir) == str(first.workspace_dir)
        second_index = next(i for i, (_, path) in enumerate(native_calls) if path.endswith("/prompt"))
        assert any(path == "/api/config" for _, path in native_calls[:second_index])
        assert any(path == "/api/model" for _, path in native_calls[:second_index])
        assert any(path == "/api/agent/build" for _, path in native_calls[:second_index])
        assert not any(method == "POST" and path == "/api/session" for method, path in native_calls)
        assert await asyncio.wait_for(engine.wait_settled("refresh-b"), timeout=20) == "completed"

        captured = await fakes.control({"command": "capture"})
        records = captured["records"]
        current_turns = [
            record["body"] for record in records
            if isinstance(record.get("body"), dict)
            and any(
                message.get("role") == "user"
                and "current refresh history marker" in str(message.get("parts", []))
                for message in record["body"].get("contents", [])
            )
        ]
        assert len(current_turns) == 1
        contents = current_turns[0]["contents"]
        assert any(c.get("role") == "user" and "prior refresh history marker" in str(c.get("parts", [])) for c in contents)
        assert any(c.get("role") == "user" and "current refresh history marker" in str(c.get("parts", [])) for c in contents)
        assert any(c.get("role") == "model" and "pong" in str(c.get("parts", [])) for c in contents)
        assert current_turns[0].get("generationConfig", {}).get("temperature") == 0.25
        assert current_turns[0].get("generationConfig", {}).get("thinkingConfig", {}).get("thinkingLevel") == "high"

        refreshed_pid = session._proc.pid
        third = _invocation(
            changed.public, tmp_path, invocation_id="refresh-c", session_ref="refresh-session",
            model_url=fakes.model_url, prompt="unchanged config history marker",
        )
        await engine.start(third, home=home, scratch=scratch)
        assert session._proc.pid == refreshed_pid
        assert await asyncio.wait_for(engine.wait_settled("refresh-c"), timeout=20) == "completed"


@pytest.mark.parametrize(
    ("model_type", "url_suffix", "thinking_path"),
    [
        ("openai", "/chat/completions", ("reasoning_effort", "low")),
        ("gemini", ":streamGenerateContent", ("generationConfig", "thinkingConfig", "thinkingLevel", "low")),
        ("anthropic", "/messages", ("thinking", "budget_tokens", 4096)),
    ],
)
async def test_native_provider_transport_captures_configured_wire(
    model_type, url_suffix, thinking_path, tmp_path, public, engine,
):
    async with fake_facades("text") as fakes:
        invocation = _invocation(
            _public_with(public, temperature=0.25, max_steps=2, effort="low", model_type=model_type),
            tmp_path, invocation_id=f"native-{model_type}", session_ref=f"native-{model_type}-session",
            model_url=fakes.model_url, prompt=f"native {model_type} marker",
        )
        await engine.start(invocation, home=tmp_path / f"home-{model_type}", scratch=tmp_path / f"scratch-{model_type}")
        assert await asyncio.wait_for(engine.wait_settled(invocation.invocation_id), timeout=20) == "completed"

        records = (await fakes.control({"command": "capture"}))["records"]
        matching = [record for record in records if url_suffix in record.get("url", "")]
        assert matching, records
        record = matching[-1]
        body = record["body"]
        if model_type == "openai":
            assert body[thinking_path[0]] == thinking_path[1]
            assert body["temperature"] == 0.25
        elif model_type == "gemini":
            assert "alt=sse" in record["url"]
            assert body["generationConfig"]["temperature"] == 0.25
            assert body["generationConfig"]["thinkingConfig"]["thinkingLevel"] == "low"
        else:
            assert body["temperature"] == 0.25
            assert body[thinking_path[0]][thinking_path[1]] == thinking_path[2]


async def test_failed_refresh_stop_sends_no_business_prompt(tmp_path, public, engine):
    async with fake_facades("text") as fakes:
        home, scratch = tmp_path / "home", tmp_path / "scratch"
        first = _invocation(
            _public_with(public, temperature=0.1, max_steps=2), tmp_path,
            invocation_id="failed-refresh-a", session_ref="failed-refresh-session",
            model_url=fakes.model_url, prompt="settled refresh baseline",
        )
        await engine.start(first, home=home, scratch=scratch)
        assert await asyncio.wait_for(engine.wait_settled("failed-refresh-a"), timeout=20) == "completed"
        session = engine._sessions[first.session_ref]
        before = await fakes.control({"command": "capture"})

        async def refuse_clean_stop(*, timeout_seconds=5.0):
            return False

        session.stop_cleanly = refuse_clean_stop
        changed = _invocation(
            _public_with(public, temperature=0.25, max_steps=1), tmp_path,
            invocation_id="failed-refresh-b", session_ref="failed-refresh-session",
            model_url=fakes.model_url, prompt="must not be sent marker",
        )
        with pytest.raises(NativeLaunchFailed, match="confirmed clean native stop"):
            await engine.start(changed, home=home, scratch=scratch)
        after = await fakes.control({"command": "capture"})
        assert after["records"] == before["records"]
        assert session._managed_fingerprint is None
        assert session.config_version is None
