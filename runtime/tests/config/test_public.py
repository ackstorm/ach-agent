# SPDX-License-Identifier: MIT
"""PublicConfig / public_config() and ExecutionBootstrap — contract §5/§11.

PublicConfig must never carry control broker/tokens/private keys, ach.identity, channel auth
or handoff secretEnv. ExecutionBootstrap is independent of RuntimeConfig/PublicConfig: no
model, prompts, engine.env or configVersion.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.public import ExecutionBootstrap, public_config
from ach_runtime.config.schema import ConfigError, parse_config

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _approved_engine_env(wire: dict[str, Any]) -> dict[str, str]:
    return dict(wire["engine"]["env"])


def test_public_config_excludes_ach_identity_and_private_sections(
    pr_review_wire: dict[str, Any],
) -> None:
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    dumped = pub.model_dump(mode="json", by_alias=True)

    assert "ach" not in dumped
    assert "channels" not in dumped
    assert "memory" not in dumped
    assert "workspace" not in dumped
    assert "artifacts" not in dumped
    # infrastructure.control (serviceAccount) never appears in the public projection.
    assert set(dumped["infrastructure"]) == {"facadeEndpoint"}


def test_public_config_only_contains_execution_data(pr_review_wire: dict[str, Any]) -> None:
    """Task-1 scope reset: the public projection is approved literals/endpoints plus
    model/capability/prompt/hooks/limits/version/agent — no ACH identity, channel/handoff
    aliases, secrets or control infrastructure."""
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    dumped = pub.model_dump(mode="json", by_alias=True)

    assert set(dumped) == {
        "schemaVersion",
        "agent",
        "configVersion",
        "model",
        "capability",
        "prompt",
        "limits",
        "engine",
        "hooks",
        "infrastructure",
    }
    assert set(dumped["infrastructure"]) == {"facadeEndpoint"}

    serialized = json.dumps(dumped)
    assert "ACH_SECRET_" not in serialized
    assert "secretEnv" not in serialized


def test_public_config_serialized_has_no_secret_alias_or_token_strings(
    pr_review_wire: dict[str, Any],
) -> None:
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    serialized = json.dumps(pub.model_dump(mode="json", by_alias=True))

    for sentinel in (
        "ACH_SECRET_IDENTITY",
        "ACH_SECRET_GITLAB_WEBHOOK",
        "ACH_SECRET_GITLAB_RO",
        "gitlab-reviewer-webhook",
    ):
        assert sentinel not in serialized


def test_public_config_includes_facade_endpoint(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    assert pub.infrastructure.facade_endpoint == cfg.infrastructure.facade_endpoint


def test_public_config_selected_cloud_literal(pr_review_wire: dict[str, Any]) -> None:
    from ach_runtime.config.canonical import compute_config_version

    pr_review_wire["engine"]["env"]["AWS_REGION"] = "eu-west-1"
    pr_review_wire["configVersion"] = compute_config_version(pr_review_wire)
    config = parse_config(pr_review_wire)
    public = public_config(config, {"AWS_REGION": "eu-west-1"})
    assert public.engine.env == {"AWS_REGION": "eu-west-1"}
    assert "ach" not in public.model_dump(by_alias=True)


def test_public_config_facade_endpoint_requires_a_host(
    pr_review_wire: dict[str, Any],
) -> None:
    """review I5: validated as PublicConfig's own leaf, not merely assumed safe because
    public_config() happens to copy it from an already-checked RuntimeConfig."""
    from ach_runtime.config.public import AchCapabilityBlock, PublicConfig, PublicEngineBlock, PublicInfrastructureBlock
    from pydantic import ValidationError

    cfg = parse_config(pr_review_wire)
    with pytest.raises(ValidationError, match="http"):
        PublicConfig(
            schemaVersion=cfg.schema_version,
            agent=cfg.agent,
            configVersion=cfg.config_version,
            model=cfg.model,
            capability=AchCapabilityBlock(filter=cfg.capability.filter),
            prompt=cfg.prompt,
            limits=cfg.limits,
            engine=PublicEngineBlock(env={}, startupTimeoutSeconds=1, compaction=cfg.engine.compaction),
            hooks=cfg.hooks,
            infrastructure=PublicInfrastructureBlock(
                facadeEndpoint="not-an-endpoint",
            ),
        )


def test_public_config_rejects_unapproved_engine_env_name(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    with pytest.raises(ConfigError, match="not approved"):
        public_config(cfg, {"UNAPPROVED_NAME": "x"})


def test_public_config_rejects_engine_env_value_drift(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    approved = _approved_engine_env(pr_review_wire)
    name = next(iter(approved))
    with pytest.raises(ConfigError, match="does not match the approved literal"):
        public_config(cfg, {name: approved[name] + "-tampered"})


def test_public_config_accepts_subset_of_approved_engine_env(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    public_config(cfg, {})  # empty is a valid subset — must not raise


def test_public_config_is_strict_forbid_extra() -> None:
    from ach_runtime.config.public import PublicConfig

    with pytest.raises(ValidationError):
        PublicConfig.model_validate({"notAField": True})


# ---------------------------------------------------------------------------
# ExecutionBootstrap (contract §11)
# ---------------------------------------------------------------------------


@pytest.fixture
def bootstrap_wire() -> dict[str, Any]:
    with (FIXTURES_DIR / "bootstrap.json").open(encoding="utf-8") as f:
        return json.load(f)


def test_bootstrap_fixture_is_accepted(bootstrap_wire: dict[str, Any]) -> None:
    bootstrap = ExecutionBootstrap.model_validate(bootstrap_wire)
    assert bootstrap.bootstrap_version == "workspace-v3"


def test_bootstrap_rejects_unknown_field(bootstrap_wire: dict[str, Any]) -> None:
    bootstrap_wire["unexpectedField"] = "x"
    with pytest.raises(ValidationError):
        ExecutionBootstrap.model_validate(bootstrap_wire)


@pytest.mark.parametrize("private_field", ["model", "prompts", "engineEnv", "configVersion"])
def test_bootstrap_rejects_private_runtime_fields(
    bootstrap_wire: dict[str, Any], private_field: str
) -> None:
    bootstrap_wire[private_field] = {"leaked": True}
    with pytest.raises(ValidationError):
        ExecutionBootstrap.model_validate(bootstrap_wire)


@pytest.mark.parametrize("field", ["facadeEndpoint"])
def test_bootstrap_endpoint_requires_a_host(bootstrap_wire: dict[str, Any], field: str) -> None:
    """review I5: ExecutionBootstrap is validated as its OWN standalone document (it is not
    derived from a RuntimeConfig in Python) — it needs this leaf check on its own fields."""
    bootstrap_wire[field] = "not-an-endpoint"
    with pytest.raises(ValidationError, match="http"):
        ExecutionBootstrap.model_validate(bootstrap_wire)


def test_bootstrap_without_projected_token_metadata(bootstrap_wire: dict[str, Any]) -> None:
    """Task-1 scope reset: bootstrap is public connection metadata, not a credential or proof
    of successful enrollment — it carries only bootstrapVersion/agent identity/endpoints and
    rejects a private `ach` block."""
    assert set(bootstrap_wire) == {"bootstrapVersion", "agent", "facadeEndpoint"}
    bootstrap = ExecutionBootstrap.model_validate(bootstrap_wire)
    assert bootstrap.bootstrap_version == "workspace-v3"

    private = dict(bootstrap_wire)
    private["ach"] = {"baseUrl": "https://ach.ackstorm.ai", "environment": "engineering-prod"}
    with pytest.raises(ValidationError):
        ExecutionBootstrap.model_validate(private)


def test_bootstrap_and_public_config_agree_on_facade_endpoint(
    pr_review_wire: dict[str, Any], bootstrap_wire: dict[str, Any]
) -> None:
    """bootstrap.json and the RuntimeConfig execution endpoints describe the SAME
    control/facade connection — they must name the same values, not drift."""
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    assert bootstrap_wire["facadeEndpoint"] == pub.infrastructure.facade_endpoint


def test_public_engine_carries_deny_tools(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["engine"]["denyTools"] = ["question"]
    pr_review_wire["configVersion"] = compute_config_version(pr_review_wire)
    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, _approved_engine_env(pr_review_wire))
    assert pub.engine.deny_tools == ["question"]


def test_bootstrap_rejects_control_endpoint_and_v1(bootstrap_wire: dict[str, Any]) -> None:
    with_control = {**bootstrap_wire, "controlEndpoint": "http://ctl.example:8081"}
    with pytest.raises(ValidationError):
        ExecutionBootstrap.model_validate(with_control)
    with pytest.raises(ValidationError):
        ExecutionBootstrap.model_validate({**bootstrap_wire, "bootstrapVersion": "workspace-v1"})


def test_public_config_requires_facade_endpoint(pr_review_wire: dict[str, Any]) -> None:
    del pr_review_wire["infrastructure"]
    pr_review_wire["configVersion"] = compute_config_version(pr_review_wire)
    cfg = parse_config(pr_review_wire)
    with pytest.raises(ConfigError, match="infrastructure.facadeEndpoint is required"):
        public_config(cfg, {})
