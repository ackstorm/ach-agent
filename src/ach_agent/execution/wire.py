"""Serializable, deliberately narrow controller/engine execution contracts."""

from __future__ import annotations

import math
from pathlib import PurePosixPath
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

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
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, populate_by_name=True)


class PublicEngineConfig(_WireModel):
    """Allowlisted engine input with managed secret fields excluded by construction."""

    # Bootstrap identity/layout metadata is public and credential-free.  The harness
    # supplies these values directly; the engine never receives AgentConfig or its
    # persistence database path.
    agent_name: str = Field(default="", alias="agentName")
    engine_type: Literal["opencode", "pi"] = "opencode"
    binary_path: str = "opencode"
    home: str = ""
    work_dir: str = "/workspace"
    persistence_enabled: bool = Field(default=False, alias="persistenceEnabled")
    persistence_mount_path: str = Field(default="", alias="persistenceMountPath")
    public_context: str = Field(default="", alias="publicContext")
    # Names are an explicit engine-role contract.  Values are read only from the
    # engine process environment and are never serialized in this object.
    engine_env_names: list[str] = Field(default_factory=list, alias="engineEnvNames")
    model: str = "gpt-4o-mini"
    model_type: str = "openai"
    params: dict[str, JsonValue] = Field(default_factory=dict)
    thinking_enabled: bool = False
    thinking_effort: str | None = None
    system_prompt: str = ""
    compose: Literal["append", "replace"] = "append"
    steps: int = 50
    startup_timeout_seconds: int = 30
    model_base_url: str = ""
    mcp_servers: dict[str, str] = Field(default_factory=dict)
    mcp_local_urls: dict[str, str] = Field(default_factory=dict)
    mcp_templates: dict[str, McpTemplate] = Field(default_factory=dict)
    exclude_tools: list[str] = Field(default_factory=list)
    codemem_db_path: str = ""
    codemem_project: str = ""
    pi_mcp_adapter_path: str = ""
    # Native TUI correlation is issued by H (or the local parent) and adopted
    # by E.  It is an opaque route token, never an engine credential.
    trace_token: str = Field(default="", alias="traceToken")

    _params_finite = field_validator("params", "mcp_templates")(_finite_json)

    @field_validator("home", "work_dir", "persistence_mount_path", "public_context")
    @classmethod
    def trusted_paths(cls, value: str) -> str:
        if value:
            path = PurePosixPath(value)
            if not path.is_absolute() or ".." in path.parts:
                raise ValueError("engine paths must be absolute and must not contain '..'")
        return value

    @field_validator("engine_env_names")
    @classmethod
    def valid_engine_env_names(cls, values: list[str]) -> list[str]:
        import re

        if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) for name in values):
            raise ValueError("engineEnvNames must contain valid environment variable names")
        managed = {
            "ACH_TOKEN",
            "ACH_API_KEY",
            "ACH_MODEL_TOKEN",
            "ACH_CHANNELS_HMAC_KEY",
            "ACH_HARNESS_URL",
            "ACH_ENGINE_URL",
            "ACH_MODEL_BASE_URL",
            "ACH_MODEL_HEADER",
        }
        leaked = sorted(set(values) & managed)
        if leaked:
            raise ValueError(f"engineEnvNames contains harness-managed names: {leaked}")
        return list(dict.fromkeys(values))


class ControllerHello(_WireModel):
    version: int
    instance_id: str
    controller_id: str


class ControllerStopRequest(_WireModel):
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


class SessionReadyRequest(_WireModel):
    """A harness acknowledgement for one resolved native session.

    The native reference is deliberately carried only in the preceding event.  The
    acknowledgement is scoped to the pending invocation/turn and cannot select an
    arbitrary harness session.
    """

    controller_id: str
    execution_id: str
    invocation_id: str
    turn_id: str


class SessionImportRow(_WireModel):
    """One bounded legacy mapping exported by H (never an H database path)."""

    key: str
    oc_session_id: str = Field(alias="ocSessionId")
    last_used: float = Field(alias="lastUsed")


class SessionImportRequest(_WireModel):
    """The single startup-only controller request for legacy session mappings."""

    controller_id: str
    rows: list[SessionImportRow] = Field(default_factory=list, max_length=1024)


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


class WorkspaceCancelRequest(_WireModel):
    """Cancel one reservation or acquired invocation.

    ``execution_id`` is optional for pre-acquire workspace reservations.  Acquired
    invocation cancellation carries it so a duplicate cleanup can be confirmed only
    for the exact handle that owned the native process.
    """

    controller_id: str
    invocation_id: str
    execution_id: str | None = None


class WorkspaceHook(_WireModel):
    """Credential-free channel hook configuration owned by the engine."""

    script: str = Field(min_length=1)
    env: dict[str, str] = Field(default_factory=dict)
    timeout_seconds: float = Field(gt=0, le=3600)

    @field_validator("timeout_seconds")
    @classmethod
    def finite_timeout(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("timeout_seconds must be finite")
        return value


class WorkspacePrepareRequest(_WireModel):
    """Prepare one public session workspace before native acquisition."""

    controller_id: str
    invocation_id: str
    session_key: str
    event_id: str
    channel_name: str
    delivery_context: dict[str, JsonValue] = Field(default_factory=dict)
    home: str
    work_dir: str
    prepare: WorkspaceHook | None = None
    cleanup: WorkspaceHook | None = None
    notify_on_stop: bool = True
    cleanup_ack_required: bool = False
    cleanup_timeout_seconds: float = Field(default=120.0, gt=0, le=3600)
    remaining_seconds: float = Field(gt=0)

    @field_validator("remaining_seconds")
    @classmethod
    def finite_remaining_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("remaining_seconds must be finite")
        return value

    @field_validator("cleanup_timeout_seconds")
    @classmethod
    def finite_cleanup_timeout_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("cleanup_timeout_seconds must be finite")
        return value

    @model_validator(mode="after")
    def ack_requires_notification(self) -> WorkspacePrepareRequest:
        if self.cleanup_ack_required and not self.notify_on_stop:
            raise ValueError("cleanup_ack_required requires notify_on_stop")
        return self

    @property
    def cleanup_budget_seconds(self) -> float:
        """Known public plus private hook allowance for the outer cleanup response."""
        public = self.cleanup.timeout_seconds if self.cleanup is not None else 0.0
        private = self.cleanup_timeout_seconds if self.cleanup_ack_required else 0.0
        return public + private


class WorkspaceCleanupAckRequest(_WireModel):
    """Correlated acknowledgement after harness-private cleanup has completed."""

    controller_id: str
    instance_id: str
    session_key: str
    event_id: str
    invocation_id: str


class WorkspaceHandoffRequest(_WireModel):
    """Import an approved credential-free Git bundle into the public workspace."""

    controller_id: str
    invocation_id: str
    session_key: str
    home: str
    work_dir: str
    bundle_path: str = Field(min_length=1)
    head: str = Field(min_length=1)
    origin: str | None = None
    remaining_seconds: float = Field(gt=0)

    @field_validator("bundle_path")
    @classmethod
    def safe_bundle_path(cls, value: str) -> str:
        from pathlib import PurePosixPath

        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("bundle_path must stay under the public workspace")
        return value

    @field_validator("remaining_seconds")
    @classmethod
    def finite_remaining_seconds(cls, value: float) -> float:
        if not math.isfinite(value):
            raise ValueError("remaining_seconds must be finite")
        return value


class WorkspaceStoppedEvent(_WireModel):
    """Correlated notification that native/public workspace cleanup has completed."""

    kind: Literal["workspace_stopped"] = "workspace_stopped"
    controller_id: str
    instance_id: str
    session_key: str
    event_id: str
    invocation_id: str
    workspace: str


class WorkspaceOperationFailure(_WireModel):
    """Typed completed workspace failure; ``confirmed`` means cleanup is known."""

    type: Literal["WorkspaceOperationFailed"] = "WorkspaceOperationFailed"
    message: str
    confirmed: bool


class ExecutionEvent(_WireModel):
    kind: Literal["text", "tool", "usage", "session_resolved", "turn_done", "error"]
    execution_id: str
    invocation_id: str
    turn_id: str
    payload: JsonValue = None

    _payload_finite = field_validator("payload")(_finite_json)
