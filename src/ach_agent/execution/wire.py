"""Serializable, deliberately narrow controller/engine execution contracts."""

from __future__ import annotations

import math
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer

McpTemplate = Annotated[LocalMcpServer | RemoteMcpServer, Field(discriminator="type")]


def _finite_json(value: JsonValue) -> JsonValue:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        for child in value.values():
            _finite_json(child)
    elif isinstance(value, list):
        for child in value:
            _finite_json(child)
    return value


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class PublicEngineConfig(_WireModel):
    """Allowlisted engine input with managed secret fields excluded by construction."""

    engine_type: Literal["opencode", "pi"] = "opencode"
    binary_path: str = "opencode"
    home: str = ""
    work_dir: str = "/workspace"
    model: str = "gpt-4o-mini"
    model_type: str = "openai"
    params: dict[str, JsonValue] = Field(default_factory=dict)
    thinking_enabled: bool = False
    thinking_effort: str | None = None
    system_prompt: str = ""
    compose: Literal["append", "replace"] = "append"
    steps: int = Field(default=50, gt=0)
    startup_timeout_seconds: int = Field(default=30, gt=0)
    model_base_url: str = ""
    mcp_servers: dict[str, str] = Field(default_factory=dict)
    mcp_local_urls: dict[str, str] = Field(default_factory=dict)
    mcp_templates: dict[str, McpTemplate] = Field(default_factory=dict)
    exclude_tools: list[str] = Field(default_factory=list)
    codemem_db_path: str = ""
    codemem_project: str = ""
    pi_mcp_adapter_path: str = ""

    _params_finite = field_validator("params", "mcp_templates")(_finite_json)


class ControllerHello(_WireModel):
    version: int
    instance_id: str
    controller_id: str


class AcquireRequest(_WireModel):
    controller_id: str
    invocation_id: str
    lane_key: str
    conversation_key: str
    reuse: bool
    remaining_seconds: float = Field(gt=0)
    config: PublicEngineConfig

    @field_validator("remaining_seconds")
    @classmethod
    def finite_remaining_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("remaining_seconds must be finite")
        return value


class ExecutionHandle(_WireModel):
    instance_id: str
    controller_id: str
    execution_id: str
    invocation_id: str
    proxy_route: str


class TurnRequest(_WireModel):
    controller_id: str
    execution_id: str
    invocation_id: str
    turn_id: str
    prompt: str
    max_tool_calls: int = Field(ge=0)


class SessionOperation(_WireModel):
    controller_id: str
    execution_id: str
    invocation_id: str
    operation: Literal["discard", "compact", "forget"]


class ReleaseRequest(_WireModel):
    controller_id: str
    execution_id: str
    invocation_id: str
    idle_ttl_seconds: float = Field(ge=0)

    @field_validator("idle_ttl_seconds")
    @classmethod
    def finite_idle_ttl_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("idle_ttl_seconds must be finite")
        return value


class ExecutionEvent(_WireModel):
    kind: Literal["text", "tool", "usage", "session_resolved", "turn_done", "error"]
    execution_id: str
    invocation_id: str
    turn_id: str
    payload: JsonValue = None

    _payload_finite = field_validator("payload")(_finite_json)
