# SPDX-License-Identifier: MIT
"""RuntimeConfig / parse_config — contract docs/schemas/ach-workspace-contract-v1.md.

Covers the surface required by
.superpowers/sdd/2026-09-30-workspace-runtime/task-2-runtime-dispatch.md: the complete
representative PR fixture accepted; maxSteps missing/0/negative rejected; reserved engine env
and SecretRef-shaped values rejected; invalid keys/duplicates/destination/shared-handoff
concurrency rejected; strict unknown fields; explicit false/empty/maps surviving roundtrip;
wrong supplied hash failing; parse->roundtrip preserving canonical bytes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from ach_runtime.config.canonical import canonical_config_bytes, compute_config_version
from ach_runtime.config.schema import ConfigError, QueueBlock, RuntimeConfig, parse_config


def _resign(wire: dict[str, Any]) -> dict[str, Any]:
    """Recompute configVersion after a test mutates the wire, so parse_config reaches the
    real field validator under test instead of failing earlier on a stale hash."""
    wire["configVersion"] = compute_config_version(wire)
    return wire


# ---------------------------------------------------------------------------
# The complete representative PR fixture
# ---------------------------------------------------------------------------


def test_pr_review_fixture_is_accepted(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    assert cfg.schema_version == "workspace-v2"
    assert cfg.agent.name == "gitlab-reviewer"
    assert cfg.limits.max_steps == 30
    assert cfg.channels[0].handoff is not None


def test_pr_review_fixture_configVersion_is_computed_not_arbitrary(
    pr_review_wire: dict[str, Any],
) -> None:
    supplied = pr_review_wire["configVersion"]
    assert supplied != "0" * 64
    assert supplied == compute_config_version(pr_review_wire)


def test_wrong_supplied_hash_fails(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["configVersion"] = "0" * 64
    with pytest.raises(ConfigError, match="configVersion mismatch"):
        parse_config(pr_review_wire)


def test_malformed_supplied_hash_fails(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["configVersion"] = "not-hex"
    with pytest.raises(ConfigError, match="64 lowercase hex"):
        parse_config(pr_review_wire)


def test_parse_roundtrip_preserves_canonical_bytes(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    dumped = cfg.model_dump(mode="json", by_alias=True, exclude_unset=True)
    dumped["configVersion"] = pr_review_wire["configVersion"]
    assert canonical_config_bytes(dumped) == canonical_config_bytes(pr_review_wire)


def test_explicit_false_empty_maps_survive_roundtrip(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["model"]["thinking"]["enabled"] = False
    pr_review_wire["ach"]["capability"]["filter"]["exclude"]["tools"] = []
    pr_review_wire["engine"]["env"] = {}
    _resign(pr_review_wire)
    cfg = parse_config(pr_review_wire)
    dumped = cfg.model_dump(mode="json", by_alias=True, exclude_unset=True)
    assert dumped["model"]["thinking"]["enabled"] is False
    assert dumped["ach"]["capability"]["filter"]["exclude"]["tools"] == []
    assert dumped["engine"]["env"] == {}


# ---------------------------------------------------------------------------
# maxSteps / deadline — mandatory positive
# ---------------------------------------------------------------------------


def test_maxSteps_missing_rejected(pr_review_wire: dict[str, Any]) -> None:
    del pr_review_wire["limits"]["maxSteps"]
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


@pytest.mark.parametrize("bad", [0, -1, -30])
def test_maxSteps_non_positive_rejected(pr_review_wire: dict[str, Any], bad: int) -> None:
    pr_review_wire["limits"]["maxSteps"] = bad
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


@pytest.mark.parametrize("bad", [0, -1])
def test_maxInvocationSeconds_deadline_non_positive_rejected(
    pr_review_wire: dict[str, Any], bad: int
) -> None:
    pr_review_wire["limits"]["maxInvocationSeconds"] = bad
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_maxInvocationSeconds_missing_rejected(pr_review_wire: dict[str, Any]) -> None:
    del pr_review_wire["limits"]["maxInvocationSeconds"]
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# Strict nested validation: RuntimeConfig.model_config(strict=True) does NOT propagate to
# independently-configured nested models (LimitsBlock, WorkspaceBlock, ...) on its own —
# parse_config must pass strict=True at call time, or `maxSteps: true` / a numeric string
# would silently coerce instead of hard-failing.
# ---------------------------------------------------------------------------


def test_maxSteps_bool_rejected_not_coerced_to_int(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["limits"]["maxSteps"] = True
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_idleTimeoutSeconds_numeric_string_rejected_not_coerced(
    pr_review_wire: dict[str, Any],
) -> None:
    pr_review_wire["workspace"]["idleTimeoutSeconds"] = "300"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_runtime_config_model_validate_without_strict_kwarg_would_coerce(
    pr_review_wire: dict[str, Any],
) -> None:
    """Documents the footgun parse_config's call-time strict=True avoids: a bare
    RuntimeConfig.model_validate(raw) (no strict kwarg) silently coerces bool -> int in a
    nested model, because RuntimeConfig's own strict=True config does not propagate."""
    pr_review_wire["limits"]["maxSteps"] = True
    cfg = RuntimeConfig.model_validate(pr_review_wire)
    assert cfg.limits.max_steps == 1

    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(pr_review_wire, strict=True)


# ---------------------------------------------------------------------------
# Reserved engine env / SecretRef-shaped values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "ACH_TOKEN",
        "HOME",
        "XDG_CONFIG_HOME",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_MODELS_URL",
        "OPENCODE_BIN",
    ],
)
def test_reserved_engine_env_name_rejected(pr_review_wire: dict[str, Any], name: str) -> None:
    pr_review_wire["engine"]["env"][name] = "x"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_opencode_enable_exa_is_allowed(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["engine"]["env"]["OPENCODE_ENABLE_EXA"] = "true"
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


def test_malformed_engine_env_name_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["engine"]["env"]["BAD-NAME"] = "x"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_non_string_engine_env_value_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["engine"]["env"]["SELECTED_NAME"] = 7
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


@pytest.mark.parametrize(
    "name",
    [
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GCE_METADATA_HOST",
        "SSL_CERT_FILE",
        "NODE_OPTIONS",
        "LD_PRELOAD",
        "HTTPS_PROXY",
    ],
)
def test_explicitly_selected_engine_env_names_are_accepted(
    pr_review_wire: dict[str, Any], name: str
) -> None:
    pr_review_wire["engine"]["env"][name] = "public-fixture-value"
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


def test_secretref_shaped_engine_env_value_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["engine"]["env"]["SOME_VAR"] = {"env": "ACH_SECRET_SOMETHING"}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# Invalid keys / duplicates / destination / shared handoff concurrency
# ---------------------------------------------------------------------------


def test_strict_unknown_top_level_field_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["unexpectedField"] = "x"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_strict_unknown_nested_field_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["limits"]["unexpectedField"] = 1
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_handoff_session_scope_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["handoff"]["scope"] = "session"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


@pytest.mark.parametrize(
    "bad_destination",
    [
        ".",
        "..",
        "/abs/path",
        "a/../b",
        "",
        # review I4: PurePosixPath collapses these to the same empty-parts root as "."
        "./",
        "././",
        "./.",
        "handoff/\x00evil",
    ],
)
def test_handoff_destination_rejected(pr_review_wire: dict[str, Any], bad_destination: str) -> None:
    pr_review_wire["channels"][0]["handoff"]["destination"] = bad_destination
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


@pytest.mark.parametrize("ok_destination", ["...", "handoff/...", ".hidden", "a/.hidden"])
def test_handoff_destination_dot_lookalikes_that_are_legitimate_names_accepted(
    pr_review_wire: dict[str, Any], ok_destination: str
) -> None:
    """`PurePosixPath` only ever drops a component that is EXACTLY '.' — "..." and ".hidden"
    are ordinary (if unusual) directory names, not root-collapse spellings, and must not be
    rejected alongside them."""
    pr_review_wire["channels"][0]["handoff"]["destination"] = ok_destination
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


def test_shared_handoff_with_concurrency_above_one_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["workspace"]["maxConcurrentSessions"] = 2
    _resign(pr_review_wire)
    with pytest.raises(ConfigError, match="maxConcurrentSessions must be 1"):
        parse_config(pr_review_wire)


def test_handoff_with_maxConcurrentSessions_one_is_accepted(pr_review_wire: dict[str, Any]) -> None:
    assert pr_review_wire["workspace"]["maxConcurrentSessions"] == 1
    parse_config(pr_review_wire)  # must not raise


def test_handoff_secret_env_name_clashing_with_env_rejected(pr_review_wire: dict[str, Any]) -> None:
    handoff = pr_review_wire["channels"][0]["handoff"]
    handoff["secretEnv"]["GITLAB_REPO_BASEURL"] = {"env": "ACH_SECRET_CLASH"}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_handoff_reserved_env_name_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["handoff"]["env"]["ACH_SESSION_KEY"] = "x"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# Routing — optional, independent overrides (contract §2)
# ---------------------------------------------------------------------------


def test_routing_absent_is_accepted(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = None
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


def test_routing_partial_override_keeps_other_default(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = {"workspaceKey": "{{ event.gitlab.projectId }}"}
    _resign(pr_review_wire)
    cfg = parse_config(pr_review_wire)
    routing = cfg.channels[0].routing
    assert routing is not None
    assert routing.workspace_key == "{{ event.gitlab.projectId }}"
    assert routing.session_key is None


def test_routing_empty_string_override_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = {"sessionKey": ""}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_routing_whitespace_only_override_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = {"sessionKey": "   "}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_routing_empty_object_is_accepted(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = {}
    _resign(pr_review_wire)
    cfg = parse_config(pr_review_wire)
    routing = cfg.channels[0].routing
    assert routing is not None
    assert routing.workspace_key is None
    assert routing.session_key is None


@pytest.mark.parametrize("key", ["workspaceKey", "sessionKey"])
def test_routing_each_one_sided_override_accepted(pr_review_wire: dict[str, Any], key: str) -> None:
    pr_review_wire["channels"][0]["routing"] = {key: "{{ event.gitlab.projectId }}"}
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


@pytest.mark.parametrize("key", ["workspaceKey", "sessionKey"])
def test_routing_explicit_null_override_rejected(pr_review_wire: dict[str, Any], key: str) -> None:
    """review I7: a KEY PRESENT with value null is not the same as an omitted key — the
    contract requires every present override to be a non-empty template string."""
    pr_review_wire["channels"][0]["routing"] = {key: None}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError, match="present but null"):
        parse_config(pr_review_wire)


def test_routing_both_overrides_explicit_null_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["channels"][0]["routing"] = {"workspaceKey": None, "sessionKey": None}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# agent.uid canonical form
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_uid",
    ["3FA0B3B2-9C7A-4E1D-8A2F-6D1C0E9B4A77", "not-a-uuid", "{3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77}"],
)
def test_agent_uid_non_canonical_rejected(pr_review_wire: dict[str, Any], bad_uid: str) -> None:
    pr_review_wire["agent"]["uid"] = bad_uid
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# QueueBlock — Started is the only ACK semantic (contract §3); no legacy ackMode selector
# ---------------------------------------------------------------------------


def test_queue_block_has_no_ack_mode_field() -> None:
    assert "ack_mode" not in QueueBlock.model_fields
    with pytest.raises(ValidationError):
        QueueBlock.model_validate({"type": "redis", "key": "k", "ackMode": "onComplete"})


def test_queue_block_accepts_minimal_shape() -> None:
    QueueBlock.model_validate({"type": "redis", "key": "k"})


# ---------------------------------------------------------------------------
# Infrastructure invariants (review I5): distinct SAs, endpoints with a host, version-tagged
# nonempty image, positive bounded storage, typed/valid scheduling.
# ---------------------------------------------------------------------------


def test_workspace_v1_is_rejected_naming_contract_v2(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["schemaVersion"] = "workspace-v1"
    _resign(pr_review_wire)
    with pytest.raises(ConfigError) as exc:
        parse_config(pr_review_wire)
    assert "workspace-v1" in str(exc.value)
    assert "ach-runtime-contract-v2" in str(exc.value)


@pytest.mark.parametrize("key", ["control", "execution"])
def test_infrastructure_control_and_execution_are_rejected(
    pr_review_wire: dict[str, Any], key: str
) -> None:
    pr_review_wire["infrastructure"][key] = {"serviceAccount": "x"}
    _resign(pr_review_wire)
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_infrastructure_facade_endpoint_only(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    assert set(pr_review_wire["infrastructure"]) == {"facadeEndpoint"}
    assert cfg.infrastructure.facade_endpoint == pr_review_wire["infrastructure"]["facadeEndpoint"]


def test_absent_infrastructure_block_parses(pr_review_wire: dict[str, Any]) -> None:
    del pr_review_wire["infrastructure"]
    _resign(pr_review_wire)
    assert parse_config(pr_review_wire).infrastructure.facade_endpoint is None


@pytest.mark.parametrize("bad", ["ach-control.ach.svc:8081", "ftp://ach-control.ach.svc:8081", "https://"])
def test_facade_endpoint_without_http_s_scheme_or_host_rejected(
    pr_review_wire: dict[str, Any], bad: str
) -> None:
    pr_review_wire["infrastructure"]["facadeEndpoint"] = bad
    _resign(pr_review_wire)
    with pytest.raises(ConfigError, match="http"):
        parse_config(pr_review_wire)


def test_http_and_https_facade_endpoints_both_accepted(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["infrastructure"]["facadeEndpoint"] = "https://host.example:8081/facades"
    _resign(pr_review_wire)
    parse_config(pr_review_wire)  # must not raise


def test_agent_namespace_stays_opaque(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["agent"]["namespace"] = "Not_DNS 1123!"
    _resign(pr_review_wire)
    assert parse_config(pr_review_wire).agent.namespace == "Not_DNS 1123!"


# ---------------------------------------------------------------------------
# M1: parse_config raises ConfigError for every rejection on this boundary, not a mix of
# ConfigError and CanonicalizationError.
# ---------------------------------------------------------------------------


def test_ingress_unicode_failure_raises_configerror_not_canonicalizationerror(
    pr_review_wire: dict[str, Any],
) -> None:
    # Ingress validation runs before the configVersion check, so an unrecomputed (now stale)
    # hash does not mask this — the lone surrogate must fail first either way.
    pr_review_wire["model"]["params"]["bad"] = "\ud800"  # lone surrogate
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_ingress_numeric_failure_raises_configerror_not_canonicalizationerror(
    pr_review_wire: dict[str, Any],
) -> None:
    pr_review_wire["model"]["params"]["bad"] = float("nan")
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


def test_surrogate_object_key_raises_configerror_not_unicodeencodeerror(
    pr_review_wire: dict[str, Any],
) -> None:
    # validate_json_value only checks keys are str, so a lone surrogate key reaches the
    # canonicalization dumps boundary (I2), not the earlier ingress check (I1's sibling).
    pr_review_wire["model"]["params"]["\ud800"] = 1
    with pytest.raises(ConfigError):
        parse_config(pr_review_wire)


# ---------------------------------------------------------------------------
# load_config_json(bytes) -> dict, parse_config(dict) -> RuntimeConfig, end to end
# ---------------------------------------------------------------------------


def test_load_config_json_then_parse_config_end_to_end(pr_review_wire: dict[str, Any]) -> None:
    import json

    raw = json.dumps(pr_review_wire).encode("utf-8")
    from ach_runtime.config.canonical import load_config_json

    decoded = load_config_json(raw)
    cfg = parse_config(decoded)
    assert isinstance(cfg, RuntimeConfig)


# ---------------------------------------------------------------------------
# review I6: the fixture's prompt must be the COMPLETE authoring example, not a shortened
# demonstration — asserted against the actual YAML source, not eyeballed.
# ---------------------------------------------------------------------------


def test_fixture_system_prompt_matches_yaml_source_exactly(pr_review_wire: dict[str, Any]) -> None:
    import yaml

    examples_path = Path(__file__).parent.parent.parent / "examples" / "gitlab-pr.yaml"
    with examples_path.open(encoding="utf-8") as f:
        source = yaml.safe_load(f)
    source_text = source["spec"]["prompt"]["system"]["text"]
    fixture_text = pr_review_wire["prompt"]["system"]["text"]
    assert fixture_text == source_text
    # A regression that silently truncated the fixture again would still pass an `==` check
    # against an equally-truncated future edit of the YAML; pin a length floor from the
    # actual paragraph content so a short fixture fails loudly either way.
    assert len(fixture_text) > 900
    for must_contain in (
        "./handoff/repo",
        "untrusted input",
        "Do not modify tracked source, commit, push, approve or merge",
        "create_artifact",
        "Confidence (0–5)",
    ):
        assert must_contain in fixture_text


# ---------------------------------------------------------------------------
# Lifecycle hooks: workspaceStart / workspaceStop
# ---------------------------------------------------------------------------


def test_workspace_hooks_are_accepted(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["hooks"]["workspaceStart"] = {"script": "true"}
    pr_review_wire["hooks"]["workspaceStop"] = {"script": "true", "timeoutSeconds": 5}
    cfg = parse_config(_resign(pr_review_wire))
    assert cfg.hooks.workspace_start is not None
    assert cfg.hooks.workspace_start.timeout_seconds == 120
    assert cfg.hooks.workspace_stop is not None
    assert cfg.hooks.workspace_stop.timeout_seconds == 5


def test_workspace_hooks_default_to_none(pr_review_wire: dict[str, Any]) -> None:
    cfg = parse_config(pr_review_wire)
    assert cfg.hooks.workspace_start is None
    assert cfg.hooks.workspace_stop is None


def test_unknown_hook_name_rejected(pr_review_wire: dict[str, Any]) -> None:
    pr_review_wire["hooks"]["preStart"] = {"script": "true"}
    with pytest.raises(ConfigError):
        parse_config(_resign(pr_review_wire))


def test_engine_deny_tools_defaults_to_empty_and_accepts_names(
    pr_review_wire: dict[str, Any],
) -> None:
    assert RuntimeConfig.model_validate(pr_review_wire).engine.deny_tools == []
    pr_review_wire["engine"]["denyTools"] = ["question", "execute"]
    assert RuntimeConfig.model_validate(pr_review_wire).engine.deny_tools == ["question", "execute"]


@pytest.mark.parametrize("deny", [["web.fetch"], [""], ["question", "question"]])
def test_engine_deny_tools_rejects_dotted_empty_or_duplicate_names(
    pr_review_wire: dict[str, Any], deny: list[str]
) -> None:
    pr_review_wire["engine"]["denyTools"] = deny
    with pytest.raises(ValidationError):
        RuntimeConfig.model_validate(pr_review_wire)


def test_workspace_ready_timeout_defaults_to_30_and_must_be_positive(pr_review_wire: dict[str, Any]) -> None:
    assert parse_config(pr_review_wire).workspace.ready_timeout_seconds == 30
    pr_review_wire["workspace"]["readyTimeoutSeconds"] = 90
    assert parse_config(_resign(pr_review_wire)).workspace.ready_timeout_seconds == 90
    pr_review_wire["workspace"]["readyTimeoutSeconds"] = 0
    with pytest.raises((ValidationError, ConfigError)):
        parse_config(_resign(pr_review_wire))
