# SPDX-License-Identifier: Apache-2.0
import pytest
from pydantic import ValidationError

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.config.schema import EngineBlock

COMPACTION = {"auto": True, "keep": {"tokens": 8000}, "buffer": 20000}


def test_engine_type_defaults_to_opencode() -> None:
    assert EngineBlock.model_validate({"compaction": COMPACTION}).type == "opencode"


def test_pi_accepts_its_own_tool_names() -> None:
    block = EngineBlock.model_validate({"type": "pi", "compaction": COMPACTION, "denyTools": ["bash", "write"]})
    assert block.deny_tools == ["bash", "write"]


def test_pi_rejects_an_opencode_tool_name() -> None:
    with pytest.raises(ValidationError, match="engine.denyTools"):
        EngineBlock.model_validate({"type": "pi", "compaction": COMPACTION, "denyTools": ["websearch"]})


@pytest.mark.parametrize("name", ["PI_OFFLINE", "PIG_HOME", "PI_CODING_AGENT_DIR"])
def test_pi_env_names_are_reserved(name: str) -> None:
    with pytest.raises(ValidationError, match="reserved"):
        EngineBlock.model_validate({"compaction": COMPACTION, "env": {name: "1"}})


def test_bootstrap_engine_defaults_to_opencode() -> None:
    bootstrap = ExecutionBootstrap.model_validate({
        "bootstrapVersion": "workspace-v3",
        "agent": {"name": "a", "namespace": "n", "uid": "00000000-0000-4000-8000-000000000001"},
        "facadeEndpoint": "https://facade.example/",
    })
    assert bootstrap.engine == "opencode"


def _parse(wire: dict, engine: str, model: str):
    from ach_runtime.config.canonical import compute_config_version
    from ach_runtime.config.schema import parse_config

    wire["engine"]["type"] = engine
    wire["capability"]["ach"]["model"]["type"] = model
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


@pytest.mark.parametrize("model", ["gemini", "anthropic"])
def test_pi_rejects_a_non_openai_model(pr_review_wire, model: str) -> None:
    from ach_runtime.config.schema import ConfigError

    with pytest.raises(ConfigError, match="engine.type 'pi'.*model.type"):
        _parse(pr_review_wire, "pi", model)


def test_pi_accepts_an_openai_model(pr_review_wire) -> None:
    assert _parse(pr_review_wire, "pi", "openai").engine.type == "pi"


def test_opencode_accepts_a_gemini_model(pr_review_wire) -> None:
    assert _parse(pr_review_wire, "opencode", "gemini").engine.type == "opencode"
