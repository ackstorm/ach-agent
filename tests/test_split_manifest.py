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
FIXTURES = ROOT / "tests" / "integration" / "fixtures" / "split"


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
    assert {item["image"] for item in containers} == {"ghcr.io/ackstorm/ach-agent:latest"}


def test_pod_keeps_private_and_shared_mounts_narrow() -> None:
    pod = _documents(SPLIT / "pod.yaml")[0]
    containers = {item["name"]: item for item in pod["spec"]["containers"]}

    harness_mounts = {item["mountPath"]: item for item in containers["harness"]["volumeMounts"]}
    channel_mounts = {item["mountPath"]: item for item in containers["channels"]["volumeMounts"]}
    engine_mounts = {item["mountPath"]: item for item in containers["engine"]["volumeMounts"]}

    assert "/var/lib/ach-agent/state" in harness_mounts
    assert "/var/lib/ach-agent/state" not in engine_mounts
    assert "/var/lib/ach-agent/home" in engine_mounts
    assert "/var/lib/ach-agent/home/workspace" in harness_mounts
    assert "/var/lib/ach-agent/home" in engine_mounts
    assert "/run/ach-agent/transfer" in harness_mounts
    assert "/run/ach-agent/transfer" in engine_mounts
    assert "/var/lib/ach-agent/state" not in channel_mounts
    assert "/var/lib/ach-agent/home/workspace" not in channel_mounts


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
    assert {item["name"] for item in (containers["channels"].get("env") or [])} == {
        "ACH_TOKEN",
        "ACH_BASE_URL",
    }
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
    assert containers["channels"]["ports"][0]["containerPort"] == 8080
    for role, port in (("channels", 8080), ("harness", 8090), ("engine", 8081)):
        container = containers[role]
        assert any(item["containerPort"] == port for item in container["ports"])
        for probe, path in (
            ("startupProbe", "/readyz"),
            ("readinessProbe", "/readyz"),
            ("livenessProbe", "/healthz"),
        ):
            assert container[probe]["httpGet"] == {"path": path, "port": port}
            assert "exec" not in container[probe]
            assert container[probe]["timeoutSeconds"] == 3
        assert container["startupProbe"]["initialDelaySeconds"] == 15
        assert container["startupProbe"]["periodSeconds"] == 5
        assert container["startupProbe"]["failureThreshold"] == 6
    assert "/etc/ach-agent/config.yaml" not in str(containers["channels"])
    assert "/etc/ach-agent/config.yaml" not in str(containers["engine"])
    assert "/var/lib/ach-agent/state" not in str(containers["channels"])
    assert "/var/lib/ach-agent/home" not in str(containers["channels"])
    assert "/run/ach-agent/transfer" in str(containers["harness"])
    assert "/run/ach-agent/transfer" in str(containers["engine"])


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
    assert all(service["build"]["target"] == "default" for service in services.values())
    assert set(compose["volumes"]) >= {
        "harness-state",
        "engine-home",
        "shared-workspace",
        "channels-ipc",
        "engine-ipc",
        "transfer-ipc",
    }
    assert "ACH_CHANNELS_HMAC_KEY" not in str(compose)
    assert "ACH_CHANNELS_CONFIG_PATH" not in str(compose)
    assert "ACH_ENGINE_CONFIG_PATH" not in str(compose)
    assert "channels-ipc" in str(compose)
    assert "engine-ipc" in str(compose)
    assert "http://127.0.0.1:8090/readyz" in str(services["harness"]["healthcheck"])
    assert "http://127.0.0.1:8081/readyz" in str(services["engine"]["healthcheck"])
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


def test_all_split_manifests_use_http_probes_and_no_bootstrap_contract() -> None:
    paths = list(SPLIT.glob("compose*.yaml")) + [SPLIT / "pod.yaml"]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        lowered = text.lower()
        assert "bootstrap" not in lowered, path
        assert "hmac" not in lowered, path
        assert "ach_agent.healthcheck" not in text, path
        assert "HTTPTransport" not in text, path
    acceptance = (SPLIT / "compose-acceptance.yaml").read_text(encoding="utf-8")
    engine_block = acceptance.split("\n  engine:", 1)[1]
    assert "DEBUG: engine-value" in engine_block
    assert "CUSTOM_TOOL_TOKEN: engine-token" in engine_block


def test_acceptance_fixtures_cover_harness_hooks_and_selected_env() -> None:
    for name in ("config-acceptance.yaml", "config-acceptance-pi.yaml"):
        cfg = AgentConfig.model_validate(
            yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
        )
        acceptance = next(channel for channel in cfg.channels if channel.name == "acceptance")
        assert acceptance.prepare is not None
        assert acceptance.cleanup is not None
        assert acceptance.prepare.secret_env["TOKEN"].env == "SPLIT_PREPARE_TOKEN"
        assert "SPLIT_PREPARE_TOKEN" not in acceptance.prepare.script
    compose = (SPLIT / "compose-acceptance.yaml").read_text(encoding="utf-8")
    assert "SPLIT_PREPARE_TOKEN: synthetic" in compose
    engine = compose.split("\n  engine:", 1)[1]
    assert "SPLIT_PREPARE_TOKEN" not in engine


def test_ephemeral_acceptance_fixture_is_nonpersistent() -> None:
    cfg = AgentConfig.model_validate(
        yaml.safe_load((FIXTURES / "config-ephemeral-acceptance.yaml").read_text(encoding="utf-8"))
    )
    assert cfg.persistence.enabled is False
    assert cfg.engine.home == ""
    assert any(channel.name == "acceptance" for channel in cfg.channels)


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
    assert public.home == "/tmp/ach-agent/home"
    assert public.work_dir == "/tmp/ach-agent/home/workspace"
    assert public.hydration_dir == ""
    assert public.codemem_db_path == "/tmp/ach-agent/home/state/codemem.db"

    compose = _documents(SPLIT / "compose-ephemeral.yaml")[0]
    services = compose["services"]
    harness = services["harness"]
    channels = services["channels"]
    engine = services["engine"]
    assert all(service["build"]["target"] == "default" for service in services.values())
    assert "http://127.0.0.1:8080/readyz" in str(channels["healthcheck"])
    assert "/tmp/ach-agent/state:uid=10001,gid=10001" in harness["tmpfs"]
    assert "/tmp/ach-agent/home:uid=10001,gid=10001" in engine["tmpfs"]
    assert any("/tmp/ach-agent/home/workspace" in mount for mount in harness["volumes"])
    assert any("/tmp/ach-agent/home" in mount for mount in engine["volumes"])
    assert all(
        "/var/lib/ach-agent" not in mount
        for service in services.values()
        for mount in service.get("volumes", [])
    )
    assert any("ephemeral-channels-ipc" in mount for mount in services["harness"]["volumes"])
    assert any("ephemeral-engine-ipc" in mount for mount in services["harness"]["volumes"])
    assert any("ephemeral-transfer-ipc" in mount for mount in services["harness"]["volumes"])
    assert "ACH_CHANNELS_HMAC_KEY" not in str(compose)
