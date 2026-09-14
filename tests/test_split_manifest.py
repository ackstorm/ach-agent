"""Contract checks for the three-container packaging examples."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from ach_agent.boot.roles import build_role_configs
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


def test_roles_use_image_entrypoint_args_and_only_harness_gets_full_config() -> None:
    pod = _documents(SPLIT / "pod.yaml")[0]
    containers = {item["name"]: item for item in pod["spec"]["containers"]}
    assert all("command" not in item for item in containers.values())
    assert containers["harness"]["args"] == ["--role", "harness"]
    assert containers["channels"]["args"] == ["--role", "channels"]
    assert containers["engine"]["args"] == ["--role", "engine"]
    harness_env = {item["name"] for item in containers["harness"].get("env", [])}
    assert "ACH_CONFIG_PATH" not in harness_env
    assert "ACH_TOKEN" in harness_env
    assert "ACH_CHANNELS_HMAC_KEY" not in harness_env
    assert "ACH_CHANNELS_CONFIG_PATH" not in harness_env
    assert "ACH_ENGINE_CONFIG_PATH" not in harness_env
    assert {item["name"] for item in (containers["channels"].get("env") or [])} == set()
    assert {item["name"] for item in (containers["engine"].get("env") or [])} == set()

    harness_mounts = {item["mountPath"]: item for item in containers["harness"]["volumeMounts"]}
    channel_mounts = {item["mountPath"]: item for item in containers["channels"]["volumeMounts"]}
    engine_mounts = {item["mountPath"]: item for item in containers["engine"]["volumeMounts"]}
    assert harness_mounts["/etc/ach-agent/config.yaml"]["readOnly"] is True
    assert harness_mounts["/run/ach-agent/channels"].get("readOnly") is not True
    assert harness_mounts["/run/ach-agent/engine"]["readOnly"] is True
    assert channel_mounts["/run/ach-agent/channels"]["readOnly"] is True
    assert "/run/ach-agent/engine" not in channel_mounts
    assert engine_mounts["/run/ach-agent/engine"].get("readOnly") is not True
    assert "/run/ach-agent/channels" not in engine_mounts
    assert all("containerPort" not in item for item in containers.values())
    for role in ("harness", "engine"):
        probe_text = str(containers[role]["startupProbe"])
        assert "HTTPTransport" in probe_text
        assert "/run/ach-agent/" in probe_text
        assert "uds=" in probe_text
    assert "8090" not in str(containers["harness"])
    assert "8081" not in str(containers["engine"])


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
        "channels-ipc",
        "engine-ipc",
    }
    assert "ACH_CHANNELS_HMAC_KEY" not in str(compose)
    assert "ACH_CHANNELS_CONFIG_PATH" not in str(compose)
    assert "ACH_ENGINE_CONFIG_PATH" not in str(compose)
    assert "channels-ipc" in str(compose)
    assert "engine-ipc" in str(compose)
    assert "8090" not in str(compose)
    assert "8081" not in str(compose)
    assert "bootstrap" not in str(compose).lower()


def test_dockerfile_exposes_split_targets_and_keeps_combined_default() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "AS engine-opencode" in dockerfile
    assert "AS engine-pi" in dockerfile
    assert "AS combined" in dockerfile
    assert 'ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ach_agent.main"]' in dockerfile
    assert "/tmp/ach-home/workspace" in dockerfile
    assert "opencode --version" in dockerfile
    assert "pi --version" in dockerfile
    assert "codemem --version" in dockerfile
    assert (
        dockerfile.count('ENTRYPOINT ["/usr/bin/tini", "--", "python", "-m", "ach_agent.main"]')
        >= 2
    )
    assert "/run/ach-agent/channels" in dockerfile
    assert "/run/ach-agent/engine" in dockerfile
    assert "EXPOSE 8090" not in dockerfile
    assert "EXPOSE 8081" not in dockerfile


def test_all_split_manifests_use_socket_probes_and_no_bootstrap_contract() -> None:
    paths = list(SPLIT.glob("compose*.yaml")) + [SPLIT / "pod.yaml"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "bootstrap" not in lowered, path
        assert "hmac" not in lowered, path
        assert "8090" not in text, path
        assert "8081" not in text, path
    acceptance = (SPLIT / "compose-acceptance.yaml").read_text(encoding="utf-8")
    engine_block = acceptance.split("\n  engine:", 1)[1]
    assert "DEBUG:" not in engine_block
    assert "CUSTOM_TOOL_TOKEN:" not in engine_block


def test_example_role_artifacts_validate_against_the_runtime_wire_models() -> None:
    config = yaml.safe_load((SPLIT / "config.yaml").read_text(encoding="utf-8"))
    assert AgentConfig.model_validate(config).agent.name == "split-example"

    channels = json.loads((SPLIT / "channels.json").read_text(encoding="utf-8"))
    assert channels["schemaVersion"] == "1"
    for source in channels["channels"]:
        ChannelSourceConfig.model_validate(source)

    engine = json.loads((SPLIT / "engine.json").read_text(encoding="utf-8"))
    assert PublicEngineConfig.model_validate(engine).agent_name == "split-example"


def test_opencode_and_pi_harness_engine_artifacts_stay_in_sync() -> None:
    for suffix in ("", "-pi"):
        config = yaml.safe_load((SPLIT / f"config{suffix}.yaml").read_text(encoding="utf-8"))
        cfg = AgentConfig.model_validate(config)
        _channels, projection = build_role_configs(cfg)
        expected = PublicEngineConfig.model_validate(projection)
        artifact = json.loads((SPLIT / f"engine{suffix}.json").read_text(encoding="utf-8"))
        actual = PublicEngineConfig.model_validate(artifact)
        assert actual.model_dump() == expected.model_dump()
        assert actual.agent_name == cfg.agent.name
        assert actual.engine_type == cfg.engine.type


def test_ephemeral_artifacts_resolve_to_the_ephemeral_mount_map() -> None:
    config = yaml.safe_load((SPLIT / "config-ephemeral.yaml").read_text(encoding="utf-8"))
    cfg = AgentConfig.model_validate(config)
    _channels, projection = build_role_configs(cfg)
    public = PublicEngineConfig.model_validate(projection)
    assert public.persistence_enabled is False
    assert public.home == "/tmp/ach-home"
    assert public.work_dir == "/tmp/ach-home/workspace"
    assert public.public_context == "/tmp/ach-public-context"
    assert public.codemem_db_path == "/tmp/ach-home/state/codemem.db"

    compose = _documents(SPLIT / "compose-ephemeral.yaml")[0]
    services = compose["services"]
    harness = services["harness"]
    engine = services["engine"]
    assert "/tmp/ach-harness-state:uid=10001,gid=10001" in harness["tmpfs"]
    assert "/tmp/ach-home:uid=10001,gid=10001" in engine["tmpfs"]
    assert any("/tmp/ach-home/workspace" in mount for mount in harness["volumes"])
    assert any("/tmp/ach-home/workspace" in mount for mount in engine["volumes"])
    assert any("/tmp/ach-public-context" in mount for mount in harness["volumes"])
    assert any("/tmp/ach-public-context" in mount for mount in engine["volumes"])
    assert all(
        "/var/lib/ach-agent" not in mount
        for service in services.values()
        for mount in service.get("volumes", [])
    )
    assert any("ephemeral-channels-ipc" in mount for mount in services["harness"]["volumes"])
    assert any("ephemeral-engine-ipc" in mount for mount in services["harness"]["volumes"])
    assert "ACH_CHANNELS_HMAC_KEY" not in str(compose)
