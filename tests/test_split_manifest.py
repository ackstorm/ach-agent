"""Contract checks for the three-container packaging examples."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ach_agent.config.schema import AgentConfig, ChannelSourceConfig
from ach_agent.execution.wire import PublicEngineConfig

ROOT = Path(__file__).parents[1]
SPLIT = ROOT / "docker" / "split"


def _documents(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as stream:
        return [doc for doc in yaml.safe_load_all(stream) if isinstance(doc, dict)]


def test_pod_has_three_restricted_ordinary_roles_and_no_init_or_host_sharing() -> None:
    pod = _documents(SPLIT / "pod.yaml")[0]
    spec = pod["spec"]
    assert isinstance(spec, dict)
    assert spec["automountServiceAccountToken"] is False
    assert spec["hostNetwork"] is False
    assert spec["hostPID"] is False
    assert spec["shareProcessNamespace"] is False
    assert "initContainers" not in spec

    containers = spec["containers"]
    assert isinstance(containers, list)
    assert {item["name"] for item in containers} == {"harness", "channels", "engine"}
    assert all("securityContext" in item for item in containers)
    assert all(item["securityContext"]["allowPrivilegeEscalation"] is False for item in containers)
    assert all(item["securityContext"]["readOnlyRootFilesystem"] is True for item in containers)
    assert all("hostPath" not in volume for volume in spec["volumes"])


def test_pod_keeps_private_and_shared_mounts_narrow() -> None:
    pod = _documents(SPLIT / "pod.yaml")[0]
    containers = {item["name"]: item for item in pod["spec"]["containers"]}

    harness_mounts = {item["mountPath"]: item for item in containers["harness"]["volumeMounts"]}
    channel_mounts = {item["mountPath"]: item for item in containers["channels"]["volumeMounts"]}
    engine_mounts = {item["mountPath"]: item for item in containers["engine"]["volumeMounts"]}

    assert "/var/lib/ach-agent/state" in harness_mounts
    assert "/var/lib/ach-agent/state" in engine_mounts
    assert (
        harness_mounts["/var/lib/ach-agent/state"]["name"]
        != engine_mounts["/var/lib/ach-agent/state"]["name"]
    )
    assert "/var/lib/ach-agent/home" in engine_mounts
    assert "/var/lib/ach-agent/workspace" in harness_mounts
    assert "/var/lib/ach-agent/workspace" in engine_mounts
    assert "/var/lib/ach-agent/public-context" in harness_mounts
    assert "/var/lib/ach-agent/public-context" in engine_mounts
    assert "/var/lib/ach-agent/state" not in channel_mounts
    assert "/var/lib/ach-agent/workspace" not in channel_mounts


def test_role_environment_contract_is_explicit_and_agent_name_is_shared() -> None:
    pod = _documents(SPLIT / "pod.yaml")[0]
    containers = {item["name"]: item for item in pod["spec"]["containers"]}

    def env(container: dict[str, object]) -> dict[str, object]:
        return {item["name"]: item for item in container["env"]}

    harness = env(containers["harness"])
    channels = env(containers["channels"])
    engine = env(containers["engine"])
    assert harness["ACH_ROLE"]["value"] == "harness"
    assert channels["ACH_ROLE"]["value"] == "channels"
    assert engine["ACH_ROLE"]["value"] == "engine"
    assert channels["ACH_AGENT_NAME"]["value"] == "split-example"
    assert harness["ACH_AGENT_NAME"]["value"] == "split-example"
    assert engine["ACH_ENGINE_CONFIG_PATH"]["value"] == "/etc/ach-agent/engine.json"
    assert channels["ACH_CHANNELS_CONFIG_PATH"]["value"] == "/etc/ach-agent/channels.json"


def test_compose_uses_one_network_namespace_and_named_role_volumes() -> None:
    documents = _documents(SPLIT / "compose.yaml")
    assert len(documents) == 1
    compose = documents[0]
    services = compose["services"]
    assert set(services) == {"harness", "channels", "engine"}
    assert services["channels"]["network_mode"] == "service:harness"
    assert services["engine"]["network_mode"] == "service:harness"
    assert services["harness"]["ports"] == ["8080:8080"]
    assert services["harness"].get("network_mode") != "host"
    assert set(compose["volumes"]) >= {
        "harness-state",
        "engine-home",
        "engine-codemem",
        "shared-workspace",
        "public-context",
    }


def test_dockerfile_exposes_split_targets_and_keeps_combined_default() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "AS engine-opencode" in dockerfile
    assert "AS engine-pi" in dockerfile
    assert "AS combined" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ach_agent.main"]' in dockerfile
    assert "opencode --version" in dockerfile
    assert "pi --version" in dockerfile
    assert "codemem --version" in dockerfile


def test_example_role_artifacts_validate_against_the_runtime_wire_models() -> None:
    config = yaml.safe_load((SPLIT / "config.yaml").read_text(encoding="utf-8"))
    assert AgentConfig.model_validate(config).agent.name == "split-example"

    channels = json.loads((SPLIT / "channels.json").read_text(encoding="utf-8"))
    assert channels["schemaVersion"] == "1"
    for source in channels["channels"]:
        ChannelSourceConfig.model_validate(source)

    engine = json.loads((SPLIT / "engine.json").read_text(encoding="utf-8"))
    assert PublicEngineConfig.model_validate(engine).agent_name == "split-example"
