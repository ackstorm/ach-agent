# SPDX-License-Identifier: Apache-2.0
import pytest
import yaml

from ach_runtime.promote import PromoteError, promote

AGENT = """\
ach: {profile: dev, environment: agent-finops}
model: {name: smart, api: anthropic, thinking: {enabled: true, effort: high}}
mcpServers:
  - name: extra
    url: https://mcp.example/mcp
    headers: {Authorization: {env: EXTRA_TOKEN}, X-Tag: {value: lit}}
engine: {denyTools: [question]}
filter: {exclude: {tools: [bash]}}
prompt: {system: {type: file, file: AGENTS.md}, compose: append}
limits: {maxSteps: 400}
"""

DEPLOY = """\
metadata: {namespace: achagents}
secretName: agent-finops
spec:
  profileRef: {name: default}
  ach:
    baseUrl: https://hub.example
    keyFrom: {tokenExchange: {identity: svc}}
  channels: [{name: chat, type: a2a}]
"""


def _files(tmp_path, agent=AGENT, deploy=DEPLOY):
    (tmp_path / "agent.yaml").write_text(agent)
    (tmp_path / "deploy.yaml").write_text(deploy)
    (tmp_path / "AGENTS.md").write_text("You are FinOps.\nBe brief.\n")
    return tmp_path / "agent.yaml", tmp_path / "deploy.yaml"


def test_promote_copies_the_manifest_and_merges_deploy(tmp_path) -> None:
    config, deploy = _files(tmp_path)
    cr = yaml.safe_load(promote(config, deploy, name="finops"))
    assert cr == {
        "apiVersion": "ach-agent.ackstorm.ai/v1alpha1",
        "kind": "Agent",
        "metadata": {"name": "finops", "namespace": "achagents"},
        "spec": {
            "capabilityServer": "ach",
            "ach": {
                "environment": "agent-finops",
                "model": {"name": "smart", "api": "anthropic", "thinking": {"enabled": True, "effort": "high"}},
                "mcpServers": [{
                    "name": "extra", "url": "https://mcp.example/mcp",
                    "headers": {
                        "Authorization": {"secretKeyRef": {"name": "agent-finops", "key": "EXTRA_TOKEN"}},
                        "X-Tag": {"value": "lit"},
                    },
                }],
                "baseUrl": "https://hub.example",
                "keyFrom": {"tokenExchange": {"identity": "svc"}},
            },
            "engine": {"denyTools": ["question"]},
            "filter": {"exclude": {"tools": ["bash"]}},
            "prompt": {"system": {"type": "text", "text": "You are FinOps.\nBe brief.\n"}, "compose": "append"},
            "limits": {"maxSteps": 400},
            "profileRef": {"name": "default"},
            "channels": [{"name": "chat", "type": "a2a"}],
        },
    }


def test_promote_omits_what_agent_yaml_does_not_set(tmp_path) -> None:
    config, deploy = _files(tmp_path, agent="ach: {environment: e}\nmodel: {name: m}\n")
    spec = yaml.safe_load(promote(config, deploy, name="finops"))["spec"]
    assert spec["ach"]["model"] == {"name": "m"}
    assert not {"engine", "filter", "prompt", "limits"} & set(spec)


def test_promote_keeps_an_ach_prompt(tmp_path) -> None:
    config, deploy = _files(tmp_path, agent=AGENT.replace("{type: file, file: AGENTS.md}", "{type: ach, ach: finops, achFile: main.md}"))
    spec = yaml.safe_load(promote(config, deploy, name="finops"))["spec"]
    assert spec["prompt"]["system"] == {"type": "ach", "ach": "finops", "achFile": "main.md"}


def test_promote_writes_a_multiline_prompt_as_a_block(tmp_path) -> None:
    config, deploy = _files(tmp_path)
    assert "text: |\n" in promote(config, deploy, name="finops")


def test_promote_is_byte_identical_for_identical_inputs(tmp_path) -> None:
    config, deploy = _files(tmp_path)
    assert promote(config, deploy, name="finops") == promote(config, deploy, name="finops")


def test_promote_never_reads_a_header_secret(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("EXTRA_TOKEN", "s3cret-value")
    config, deploy = _files(tmp_path)
    assert "s3cret-value" not in promote(config, deploy, name="finops")


def test_promote_fails_on_a_field_set_in_both_files(tmp_path) -> None:
    config, deploy = _files(tmp_path, deploy=DEPLOY + "  limits: {maxSteps: 50}\n")
    with pytest.raises(PromoteError, match=r"spec\.limits\.maxSteps"):
        promote(config, deploy, name="finops")


def test_promote_merges_disjoint_fields_of_one_block(tmp_path) -> None:
    config, deploy = _files(tmp_path, deploy=DEPLOY + "  limits: {maxInvocationSeconds: 1800}\n")
    cr = yaml.safe_load(promote(config, deploy, name="finops"))
    assert cr["spec"]["limits"] == {"maxSteps": 400, "maxInvocationSeconds": 1800}


def test_promote_rejects_a_standalone_manifest(tmp_path) -> None:
    config, deploy = _files(tmp_path, agent="model: {name: m, api: openai, baseUrl: 'https://m.example/v1'}\n")
    with pytest.raises(PromoteError, match="standalone"):
        promote(config, deploy, name="finops")


def test_promote_needs_secret_name_for_an_env_header(tmp_path) -> None:
    config, deploy = _files(tmp_path, deploy=DEPLOY.replace("secretName: agent-finops\n", ""))
    with pytest.raises(PromoteError, match="secretName"):
        promote(config, deploy, name="finops")


def test_promote_rejects_unknown_deploy_keys(tmp_path) -> None:
    config, deploy = _files(tmp_path, deploy=DEPLOY + "channels: []\n")
    with pytest.raises(PromoteError, match="channels"):
        promote(config, deploy, name="finops")


def test_deploy_metadata_name_wins_over_the_default(tmp_path) -> None:
    config, deploy = _files(tmp_path, deploy=DEPLOY.replace("{namespace: achagents}", "{name: other, namespace: n}"))
    assert yaml.safe_load(promote(config, deploy, name="finops"))["metadata"] == {"name": "other", "namespace": "n"}
