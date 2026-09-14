from __future__ import annotations

import pytest
from pydantic import ValidationError


def test_execution_wire_round_trip_and_extra_fields_are_rejected() -> None:
    from ach_agent.execution.wire import (
        AcquireRequest,
        ControllerHello,
        ExecutionEvent,
        ExecutionHandle,
        PublicEngineConfig,
        ReleaseRequest,
        SessionOperation,
        TurnRequest,
    )

    config = PublicEngineConfig(
        engine_type="pi",
        binary_path="/engine/bin/pi",
        home="/engine/home",
        work_dir="/engine/work",
        model="model-x",
        model_type="openai",
        params={"temperature": 0.1},
        thinking_enabled=True,
        thinking_effort="high",
        system_prompt="persona",
        compose="append",
        steps=4,
        startup_timeout_seconds=9,
        model_base_url="http://127.0.0.1:8000/model",
        mcp_local_urls={"proxy": "http://127.0.0.1:8000/mcp/proxy"},
        mcp_templates={
            "srv": {
                "type": "remote",
                "url": "http://mcp",
                "headers": {"Authorization": "${env:TOKEN}"},
            }
        },
        exclude_tools=["dangerous"],
        codemem_db_path="/engine/memory.db",
        codemem_project="project",
        pi_mcp_adapter_path="/engine/pi-adapter",
    )
    request = AcquireRequest(
        controller_id="controller",
        invocation_id="invocation",
        lane_key="lane",
        conversation_key="conversation",
        reuse=True,
        remaining_seconds=12.5,
        config=config,
    )
    assert AcquireRequest.model_validate_json(request.model_dump_json()) == request

    messages = [
        ControllerHello(version=1, instance_id="instance", controller_id="controller"),
        ExecutionHandle(
            instance_id="instance",
            controller_id="controller",
            execution_id="execution",
            invocation_id="invocation",
            proxy_route="/execute/execution",
        ),
        TurnRequest(
            controller_id="controller",
            execution_id="execution",
            invocation_id="invocation",
            turn_id="turn",
            prompt="hello",
            max_tool_calls=2,
        ),
        SessionOperation(
            controller_id="controller",
            execution_id="execution",
            invocation_id="invocation",
            operation="compact",
        ),
        ReleaseRequest(
            controller_id="controller",
            execution_id="execution",
            invocation_id="invocation",
            idle_ttl_seconds=10,
        ),
        ExecutionEvent(
            kind="tool",
            execution_id="execution",
            invocation_id="invocation",
            turn_id="turn",
            payload={"name": "tool", "input": {"arg": "value"}, "output": {"ok": True}},
        ),
    ]
    for message in messages:
        assert message.__class__.model_validate_json(message.model_dump_json()) == message

    with pytest.raises(ValidationError):
        PublicEngineConfig.model_validate({"unexpected_secret": "ek-test"})
    with pytest.raises(ValidationError):
        PublicEngineConfig.model_validate({"engineEnvNames": ["BAD-NAME"]})
    with pytest.raises(ValidationError):
        AcquireRequest.model_validate({**request.model_dump(), "forward_env": ["TOKEN"]})
    for forbidden in ("forward_env", "extra_mcp_servers", "environment", "managed_headers"):
        with pytest.raises(ValidationError):
            PublicEngineConfig.model_validate(
                {**config.model_dump(), forbidden: {"TOKEN": "secret"}}
            )


def test_deadlines_are_finite_and_public_mcp_templates_remain_raw() -> None:
    from ach_agent.execution.wire import AcquireRequest, PublicEngineConfig, ReleaseRequest

    config = PublicEngineConfig(
        mcp_templates={
            "local": {
                "type": "local",
                "command": "server",
                "args": ["--stdio"],
                "env": ["TOKEN_NAME"],
            },
            "remote": {
                "type": "remote",
                "url": "http://mcp",
                "headers": {"Authorization": "${env:TOKEN}"},
            },
        },
    )
    restored = PublicEngineConfig.model_validate_json(config.model_dump_json())
    assert restored == config
    base = {
        "controller_id": "c",
        "invocation_id": "i",
        "lane_key": "l",
        "conversation_key": "k",
        "reuse": True,
        "config": config,
    }
    with pytest.raises(ValidationError):
        AcquireRequest.model_validate({**base, "remaining_seconds": float("inf")})
    with pytest.raises(ValidationError):
        AcquireRequest.model_validate({**base, "remaining_seconds": 0})
    with pytest.raises(ValidationError):
        ReleaseRequest.model_validate(
            {
                "controller_id": "c",
                "execution_id": "e",
                "invocation_id": "i",
                "idle_ttl_seconds": float("nan"),
            }
        )


def test_startup_wire_carries_hydration_directory_and_names_only() -> None:
    from ach_agent.execution.wire import PublicEngineConfig

    config = PublicEngineConfig(
        hydration_dir="/run/ach-agent/transfer/.ach-harness-shared-files-x",
        engine_env_names=["SAFE_OPERATOR"],
    )
    payload = config.model_dump(by_alias=True)
    assert payload["hydrationDir"].startswith("/run/ach-agent/transfer/")
    assert payload["engineEnvNames"] == ["SAFE_OPERATOR"]
    assert "engineEnv" not in payload


def test_execution_event_rejects_non_finite_payload() -> None:
    from ach_agent.execution.wire import ExecutionEvent

    with pytest.raises(ValidationError):
        ExecutionEvent(
            kind="error",
            execution_id="e",
            invocation_id="i",
            turn_id="t",
            payload={"value": float("nan")},
        )
