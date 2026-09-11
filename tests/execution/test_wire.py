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
            kind="usage",
            execution_id="execution",
            invocation_id="invocation",
            turn_id="turn",
            payload={"input": 1},
        ),
    ]
    for message in messages:
        assert message.__class__.model_validate_json(message.model_dump_json()) == message

    with pytest.raises(ValidationError):
        PublicEngineConfig.model_validate({"unexpected_secret": "ek-test"})
    with pytest.raises(ValidationError):
        AcquireRequest.model_validate({**request.model_dump(), "forward_env": ["TOKEN"]})


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
