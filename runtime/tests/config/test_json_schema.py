# SPDX-License-Identifier: MIT
"""Draft 2020-12 JSON Schema parity for the artifact ACH vendors (review I2).

`parse_config`/Pydantic rejecting a bad wire is not evidence that the EXPORTED
`docs/schemas/ach-workspace-config-v2.schema.json` rejects the same wire — the two are
produced by different code paths (Pydantic's Python validators vs. `gen_schema.py`'s JSON
Schema export). This module runs the real `jsonschema.Draft202012Validator` against both the
shared fixture (must pass) and the review's own six repro cases (each must fail), so semantic
parity is evidence, not assumed from the generation-drift check alone.

Covers more than the review's own six repro cases on purpose (its own pre-commit re-check
flagged that channel type/block coherence, the nested `webhook.gitlabEvents` requirement and
the conversational-events restriction ARE schema-representable and must be patched, not
just documented as out of reach). Only two things stay genuinely outside JSON Schema,
documented in `gen_schema.py` itself: the configVersion digest (needs computation), and real filesystem/symlink containment for
`handoff.destination` (deferred to the handoff implementation by the contract itself).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.public import ExecutionBootstrap, PublicConfig

SCHEMA_PATH = Path(__file__).parent.parent.parent / "docs" / "schemas" / "ach-workspace-config-v2.schema.json"


@pytest.fixture(scope="module")
def runtime_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as f:
        schema = json.load(f)
    Draft202012Validator.check_schema(schema)
    return schema


@pytest.fixture(scope="module")
def runtime_validator(runtime_schema: dict[str, Any]) -> Draft202012Validator:
    return Draft202012Validator(runtime_schema)


def test_committed_schema_is_valid_draft202012(runtime_schema: dict[str, Any]) -> None:
    assert runtime_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"


def test_pr_review_fixture_validates_against_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    errors = list(runtime_validator.iter_errors(pr_review_wire))
    assert errors == []


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
def test_explicitly_selected_engine_env_names_accepted_by_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any], name: str
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["engine"]["env"][name] = "public-fixture-value"
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire)) == []


@pytest.mark.parametrize(
    "name",
    [
        "HOME",
        "ACH_TOKEN",
        "XDG_CONFIG_HOME",
        "OPENCODE_CONFIG_CONTENT",
        "OPENCODE_SERVER_PASSWORD",
        "OPENCODE_DISABLE_PROJECT_CONFIG",
        "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_MODELS_URL",
        "OPENCODE_BIN",
    ],
)
def test_runtime_owned_engine_env_names_rejected_by_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any], name: str
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["engine"]["env"][name] = "public-fixture-value"
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire))


def test_malformed_engine_env_name_rejected_by_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["engine"]["env"]["BAD-NAME"] = "x"
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire))


def test_non_string_engine_env_value_rejected_by_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["engine"]["env"]["SELECTED_NAME"] = 7
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire))


def _resign(wire: dict[str, Any]) -> dict[str, Any]:
    wire["configVersion"] = compute_config_version(wire)
    return wire


def _mutation_noncanonical_uid(w: dict[str, Any]) -> None:
    w["agent"]["uid"] = w["agent"]["uid"].upper()


def _mutation_reserved_engine_home(w: dict[str, Any]) -> None:
    w["engine"]["env"]["HOME"] = "/x"


def _mutation_empty_routing_override(w: dict[str, Any]) -> None:
    w["channels"][0]["routing"] = {"sessionKey": ""}


def _mutation_null_routing_override(w: dict[str, Any]) -> None:
    w["channels"][0]["routing"] = {"sessionKey": None}


def _mutation_absolute_handoff_destination(w: dict[str, Any]) -> None:
    w["channels"][0]["handoff"]["destination"] = "/abs"


def _mutation_handoff_destination_dot_slash(w: dict[str, Any]) -> None:
    w["channels"][0]["handoff"]["destination"] = "././"


def _mutation_shared_handoff_concurrency_two(w: dict[str, Any]) -> None:
    w["workspace"]["maxConcurrentSessions"] = 2


def _mutation_webhook_channel_missing_webhook_block(w: dict[str, Any]) -> None:
    w["channels"][0]["webhook"] = None


def _mutation_whitespace_only_routing_override(w: dict[str, Any]) -> None:
    w["channels"][0]["routing"] = {"sessionKey": "   "}


def _mutation_snake_case_max_steps(w: dict[str, Any]) -> None:
    w["limits"]["max_steps"] = w["limits"].pop("maxSteps")


def _mutation_cron_type_missing_cron_block(w: dict[str, Any]) -> None:
    w["channels"][0]["type"] = "cron"
    w["channels"][0]["source"] = None
    w["channels"][0]["webhook"] = None


def _mutation_webhook_script_missing_nested_gitlab_events(w: dict[str, Any]) -> None:
    w["channels"][0]["type"] = "webhook-script"
    w["channels"][0]["script"] = {"script": "x", "env": {}, "secretEnv": {}, "timeoutSeconds": 10}
    w["channels"][0]["webhook"]["gitlabEvents"] = None
    w["channels"][0]["prompt"] = None
    w["channels"][0]["handoff"] = None


def _mutation_webhook_routing_nonconversational_event(w: dict[str, Any]) -> None:
    w["channels"][0]["webhook"]["gitlabEvents"] = ["push"]


def _mutation_webhook_with_foreign_cron_block(w: dict[str, Any]) -> None:
    w["channels"][0]["cron"] = {"schedule": "* * * * *", "timezone": "UTC"}


def _mutation_webhook_script_keeps_forbidden_prompt(w: dict[str, Any]) -> None:
    w["channels"][0]["type"] = "webhook-script"
    w["channels"][0]["script"] = {"script": "x", "env": {}, "secretEnv": {}, "timeoutSeconds": 10}
    # prompt stays non-null (webhook-script forbids it) — handoff cleared so only `prompt`
    # is under test.
    w["channels"][0]["handoff"] = None


# review I5 follow-up: endpoint/image/quantity leaves, exported alongside the same I2 patch.
def _mutation_facade_endpoint_missing_scheme(w: dict[str, Any]) -> None:
    w["infrastructure"]["facadeEndpoint"] = "ach-control.ach.svc:8081"


# fix-2 gap A: webhook-script requires source == "gitlab" specifically — plain "webhook"
# accepts github/generic too, so this must be its own case, not folded into the generic
# "source missing" mutation already covered by _mutation_cron_type_missing_cron_block-style
# non-null checks.
def _mutation_webhook_script_source_not_gitlab(w: dict[str, Any]) -> None:
    w["channels"][0]["type"] = "webhook-script"
    w["channels"][0]["source"] = "generic"
    w["channels"][0]["script"] = {"script": "x", "env": {}, "secretEnv": {}, "timeoutSeconds": 10}
    w["channels"][0]["prompt"] = None
    w["channels"][0]["handoff"] = None


# fix-2 gap B: minLength + path-shape conditions alone admitted an all-whitespace
# destination; Python's check is `.strip()`-based, same as routing overrides.
def _mutation_whitespace_only_handoff_destination(w: dict[str, Any]) -> None:
    w["channels"][0]["handoff"]["destination"] = "   "


# The review's own six reproduction cases (task-2-schema-review.md I2 evidence) plus the
# broader channel-coherence/whitespace/snake_case cases its own pre-commit re-check asked
# for, plus I5's endpoint/image/quantity/fixed-path leaves — not just the reviewer's repro
# subset.
NEGATIVE_MUTATIONS = {
    "noncanonical_uid": _mutation_noncanonical_uid,
    "reserved_engine_env_HOME": _mutation_reserved_engine_home,
    "empty_routing_override": _mutation_empty_routing_override,
    "null_routing_override": _mutation_null_routing_override,
    "whitespace_only_routing_override": _mutation_whitespace_only_routing_override,
    "absolute_handoff_destination": _mutation_absolute_handoff_destination,
    "handoff_destination_dot_slash_equivalent_root": _mutation_handoff_destination_dot_slash,
    "shared_handoff_with_concurrency_two": _mutation_shared_handoff_concurrency_two,
    "webhook_channel_missing_webhook_block": _mutation_webhook_channel_missing_webhook_block,
    "snake_case_max_steps": _mutation_snake_case_max_steps,
    "cron_type_missing_cron_block": _mutation_cron_type_missing_cron_block,
    "webhook_script_missing_nested_gitlab_events": _mutation_webhook_script_missing_nested_gitlab_events,
    "webhook_routing_nonconversational_event": _mutation_webhook_routing_nonconversational_event,
    "webhook_with_foreign_cron_block": _mutation_webhook_with_foreign_cron_block,
    "webhook_script_keeps_forbidden_prompt": _mutation_webhook_script_keeps_forbidden_prompt,
    "facade_endpoint_missing_scheme": _mutation_facade_endpoint_missing_scheme,
    "webhook_script_source_not_gitlab": _mutation_webhook_script_source_not_gitlab,
    "whitespace_only_handoff_destination": _mutation_whitespace_only_handoff_destination,
}


@pytest.mark.parametrize("name", sorted(NEGATIVE_MUTATIONS))
def test_negative_case_rejected_by_exported_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any], name: str
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    NEGATIVE_MUTATIONS[name](wire)
    _resign(wire)
    errors = list(runtime_validator.iter_errors(wire))
    assert errors, f"{name}: exported schema accepted a wire parse_config rejects"


# ---------------------------------------------------------------------------
# Positive cases the schema must NOT reject — guards against the regex being over-strict
# (round 1's `[./]*` destination pattern wrongly rejected the legitimate directory name
# "...", since every one of its characters happens to be '.').
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("destination", ["...", "handoff/...", ".hidden", "a/.hidden"])
def test_destination_dot_lookalike_legitimate_names_accepted_by_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any], destination: str
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["channels"][0]["handoff"]["destination"] = destination
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire)) == []


def test_routing_each_one_sided_override_accepted_by_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    wire = copy.deepcopy(pr_review_wire)
    wire["channels"][0]["routing"] = {"workspaceKey": "{{ event.gitlab.projectId }}"}
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire)) == []


def test_webhook_script_with_source_gitlab_accepted_by_schema(
    runtime_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    """fix-2 gap A positive: source == "gitlab" (the only value Python accepts for
    webhook-script) must still validate once the const constraint is in place."""
    wire = copy.deepcopy(pr_review_wire)
    wire["channels"][0]["type"] = "webhook-script"
    wire["channels"][0]["script"] = {"script": "x", "env": {}, "secretEnv": {}, "timeoutSeconds": 10}
    wire["channels"][0]["prompt"] = None
    wire["channels"][0]["handoff"] = None
    _resign(wire)
    assert list(runtime_validator.iter_errors(wire)) == []


# ---------------------------------------------------------------------------
# ExecutionBootstrap — generated on the fly, not a second committed artifact (only
# ach-workspace-config-v2.schema.json is the file ACH vendors).
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def bootstrap_validator() -> Draft202012Validator:
    # RAW product export, no test-side patch (fix-2 gap C re-check): the HTTPS pattern now
    # lives in ExecutionBootstrap's own `Field(json_schema_extra=...)` (public.py), so
    # `model_json_schema()` already carries it before this validator ever sees the dict.
    schema = ExecutionBootstrap.model_json_schema(by_alias=True)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


@pytest.fixture(scope="module")
def public_config_validator() -> Draft202012Validator:
    # Same — PublicInfrastructureBlock's fields carry the constraint natively now.
    schema = PublicConfig.model_json_schema(by_alias=True)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def test_bootstrap_and_public_config_raw_export_carry_endpoint_pattern_natively() -> None:
    """fix-2 gap C, final form: calls `model_json_schema()` directly — no fixture, no
    patch — and asserts the endpoint pattern is already a key in the untouched dict. This is
    the literal claim the re-check was about: the RAW product export, not a test-side
    in-memory patch of it."""
    from ach_runtime.config.schema import ENDPOINT_PATTERN

    boot_props = ExecutionBootstrap.model_json_schema(by_alias=True)["properties"]
    for field in ("facadeEndpoint",):
        assert boot_props[field].get("pattern") == ENDPOINT_PATTERN

    pub_props = PublicConfig.model_json_schema(by_alias=True)["$defs"][
        "PublicInfrastructureBlock"
    ]["properties"]
    for field in ("facadeEndpoint",):
        assert pub_props[field].get("pattern") == ENDPOINT_PATTERN


def test_bootstrap_fixture_validates_against_generated_schema(
    bootstrap_validator: Draft202012Validator,
) -> None:
    with (Path(__file__).parent / "fixtures" / "bootstrap.json").open(encoding="utf-8") as f:
        wire = json.load(f)
    assert list(bootstrap_validator.iter_errors(wire)) == []


def test_bootstrap_unknown_field_rejected_by_generated_schema(
    bootstrap_validator: Draft202012Validator,
) -> None:
    with (Path(__file__).parent / "fixtures" / "bootstrap.json").open(encoding="utf-8") as f:
        wire = json.load(f)
    wire["model"] = {"leaked": True}
    assert list(bootstrap_validator.iter_errors(wire)) != []


@pytest.mark.parametrize("field", ["facadeEndpoint"])
def test_bootstrap_endpoint_without_host_rejected_by_generated_schema(
    bootstrap_validator: Draft202012Validator, field: str
) -> None:
    """fix-2 gap C: before the patch, this was ACCEPTED by the schema and REJECTED only by
    Pydantic's own field_validator — the two code paths disagreed."""
    with (Path(__file__).parent / "fixtures" / "bootstrap.json").open(encoding="utf-8") as f:
        wire = json.load(f)
    wire[field] = "not-an-endpoint"
    assert list(bootstrap_validator.iter_errors(wire)) != []


def test_public_config_fixture_validates_against_generated_schema(
    public_config_validator: Draft202012Validator, pr_review_wire: dict[str, Any]
) -> None:
    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import parse_config

    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, dict(pr_review_wire["engine"]["env"]))
    dumped = pub.model_dump(mode="json", by_alias=True)
    assert list(public_config_validator.iter_errors(dumped)) == []


@pytest.mark.parametrize("field", ["facadeEndpoint"])
def test_public_config_endpoint_without_host_rejected_by_generated_schema(
    public_config_validator: Draft202012Validator, pr_review_wire: dict[str, Any], field: str
) -> None:
    from ach_runtime.config.public import public_config
    from ach_runtime.config.schema import parse_config

    cfg = parse_config(pr_review_wire)
    pub = public_config(cfg, dict(pr_review_wire["engine"]["env"]))
    dumped = pub.model_dump(mode="json", by_alias=True)
    dumped["infrastructure"][field] = "not-an-endpoint"
    assert list(public_config_validator.iter_errors(dumped)) != []


# ---------------------------------------------------------------------------
# Positive cases + the RE2/Go-compilability regression itself (ACH's task-2-ach-report.md:
# a PCRE lookahead compiles fine under Python's `re`/this validator's PCRE-flavored engine,
# so `check_schema`/`iter_errors` passing is NOT evidence Go's RE2-based
# santhosh-tekuri/jsonschema can even COMPILE the pattern — this asserts the literal
# byte-level absence of lookaround syntax, which is the actual cross-language constraint).
# ---------------------------------------------------------------------------


def test_no_pcre_lookaround_in_committed_schema(runtime_schema: dict[str, Any]) -> None:
    """RE2 (every Go JSON Schema library, same as Kubernetes CRD `x-kubernetes-validations`
    `pattern`) cannot COMPILE `(?!...)`/`(?=...)`/`(?<!...)`/`(?<=...)` — not merely
    mismatch at validation time. Round 1 shipped two of these
    (`EngineBlock.env.propertyNames`, `HandoffBlock.destination`); ACH's own Go-side
    `santhosh-tekuri/jsonschema` compile step caught it (task-2-ach-report.md). Checked at
    the raw-text level because a PCRE-flavored Python validator happily compiles and runs
    these patterns — passing `iter_errors` tests is not evidence of RE2 compatibility."""
    text = json.dumps(runtime_schema)
    for lookaround in ("(?!", "(?=", "(?<!", "(?<="):
        assert lookaround not in text, f"found PCRE lookaround {lookaround!r} — RE2 can't compile it"


def test_committed_schema_equals_generated() -> None:
    import importlib.util

    script = Path(__file__).parent.parent.parent / "scripts" / "gen_schema.py"
    spec = importlib.util.spec_from_file_location("gen_schema", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert SCHEMA_PATH.read_text(encoding="utf-8") == module.generate()
