#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Regenerate docs/schemas/ach-workspace-config-v3.schema.json from RuntimeConfig.

ACH vendors this file with a drift gate (internal/agentrender/schema_test.go, Task 2B) — this
script is the single source of truth, never hand-edited. `--check` fails (exit 1) instead of
writing when the committed file is stale.

    python scripts/gen_schema.py          # regenerate docs/schemas/ach-workspace-config-v3.schema.json
    python scripts/gen_schema.py --check  # fail if the committed file is stale (CI / make schema-check)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from ach_runtime.config.schema import (  # noqa: E402
    CONVERSATIONAL_GITLAB_EVENTS,
    _RESERVED_ENGINE_ENV_EXACT,
    _RESERVED_ENGINE_ENV_PREFIXES,
    RuntimeConfig,
)

SCHEMA_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "schemas" / "ach-workspace-config-v3.schema.json"
)

# review I2 (round 1) + two pre-commit correctness re-checks: constraints Pydantic's own
# field types export automatically (patterns, min/max, enums) are left alone; this patches
# the gaps that are genuinely schema-representable, generated from the SAME Python
# constants/sets the runtime validator uses (no second hand-written list to drift), not a
# parallel schema authored independently of the model.
#
# Round-2 correctness re-check #1: channel type/block coherence, the webhook-script nested
# gitlabEvents requirement, and the webhook conversational-events restriction ARE
# representable (if/then/properties/not/enum) and ARE patched below (`_patch_channel_config`).
#
# Round-2 correctness re-check #2 (ACH's task-2-ach-report.md, confirmed against
# santhosh-tekuri/jsonschema): every pattern below is written WITHOUT lookahead/lookbehind
# — `(?!...)`/`(?=...)`/`(?<!...)`/`(?<=...)` are PCRE-only; Go's RE2 (which every Go JSON
# Schema implementation is built on, same as Kubernetes CRD `x-kubernetes-validations`
# `pattern`) cannot compile them at all, not merely mismatch at runtime. Every negation
# below is expressed as JSON Schema's own `not`/`allOf` composition instead — the schema
# validator performs the negation, not the regex engine, so the regex itself never needs
# lookaround. Non-capturing groups `(?:...)`, alternation, anchors and character classes are
# all ordinary RE2 and used freely.
#
# Only two things remain genuinely outside JSON Schema's reach, regex dialect aside: (1)
# the configVersion digest itself — needs computation, not a structural check; (2) real filesystem
# containment/symlink resolution for handoff.destination — the contract already defers this
# to the handoff implementation, not to config validation.


def _nonempty_non_whitespace_string() -> dict:
    # minLength alone accepts an all-whitespace string ("   "); "pattern" matching is an
    # UNANCHORED substring search in JSON Schema, so "\\S" alone means "contains at least
    # one non-whitespace character anywhere" — exactly the `.strip()` check schema.py makes.
    # No lookaround: a single character class, RE2-safe.
    return {"type": "string", "minLength": 1, "pattern": r"\S"}


def _not_matching(pattern: str) -> dict:
    return {"not": {"pattern": pattern}}


def _engine_env_property_names_schema() -> dict:
    # allOf, not a single `(?!...)` pattern: a bare valid-identifier shape, ANDed with "not
    # one of the exact reserved names" (enum — no regex at all) and "not one of the reserved
    # prefixes" (plain alternation, no lookaround).
    prefixes = "|".join(_RESERVED_ENGINE_ENV_PREFIXES)
    return {
        "allOf": [
            {"pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"},
            {"not": {"enum": sorted(_RESERVED_ENGINE_ENV_EXACT)}},
            _not_matching(rf"^(?:{prefixes})"),
        ]
    }


def _handoff_destination_schema() -> dict:
    # Nonempty, not absolute, no NUL, no ".." path segment, and not a pure dot/slash spelling
    # of the Workspace root itself — but "..." (three literal dots, an ordinary directory
    # name) must NOT be caught by that last rule: `PurePosixPath` only ever drops a
    # component that is EXACTLY "." (and leaves ".." alone), it is not a generic
    # "starts with dot" rule. Each condition is its own unanchored-or-anchored `not`
    # pattern — not one PCRE lookahead chain — so every single pattern is plain RE2.
    return {
        "type": "string",
        "minLength": 1,
        "allOf": [
            _not_matching(r"^/"),  # not absolute
            _not_matching(r"\x00"),  # no NUL byte anywhere
            _not_matching(r"(?:^|/)\.\.(?:/|$)"),  # no ".." path segment anywhere
            _not_matching(r"^(?:\.?/)*\.?$"),  # not a pure "/"-joined ""/"." collapse to root
            # fix-2 gap B: minLength/path conditions alone admitted "   " — Python's check is
            # `.strip()`-based, same unanchored-substring-search trick as RoutingBlock's "\S".
            {"pattern": r"\S"},
        ],
    }


def _patch_routing_block(defs: dict) -> None:
    routing = defs["RoutingBlock"]["properties"]
    for key in ("workspaceKey", "sessionKey"):
        routing[key] = {**_nonempty_non_whitespace_string(), "title": routing[key].get("title", key)}


def _patch_engine_block(defs: dict) -> None:
    defs["EngineBlock"]["properties"]["env"]["propertyNames"] = _engine_env_property_names_schema()


def _patch_handoff_block(defs: dict) -> None:
    defs["HandoffBlock"]["properties"]["destination"] = {
        **_handoff_destination_schema(),
        "title": defs["HandoffBlock"]["properties"]["destination"].get("title", "Destination"),
    }


def _not_null() -> dict:
    return {"not": {"type": "null"}}


_ALL_CHANNEL_BLOCKS = ("webhook", "cron", "queue", "a2a", "script")


def _patch_channel_config(defs: dict) -> None:
    """Mirror `ChannelConfig._check_type_block_coherence` (schema.py) in full: required
    transport block per type, every foreign block forced null, `webhook-script`'s nested
    `webhook.gitlabEvents` requirement and its `prompt`/`handoff` prohibition, and `webhook`'s
    restriction to the conversational GitLab event kinds (`CONVERSATIONAL_GITLAB_EVENTS`).
    """
    channel = defs["ChannelConfig"]
    required_by_type = {
        "webhook": ["source", "webhook"],
        "webhook-script": ["source", "webhook", "script"],
        "cron": ["cron"],
        "queue": ["queue"],
        "a2a": ["a2a"],
    }
    # "webhook-script" is the one type allowed to carry BOTH its own block and "webhook"
    # (ScriptBlock.check_type_block_coherence's one exception); every other foreign block
    # must be null for every type, mirroring the Python `for foreign in (...)` loop exactly.
    foreign_null_by_type = {
        type_: [b for b in _ALL_CHANNEL_BLOCKS if b not in fields]
        for type_, fields in required_by_type.items()
    }

    def _merge_webhook_constraint(extra: dict) -> dict:
        # "properties"/"required" are no-ops against a null instance in Draft 2020-12 (they
        # only constrain objects) — so a nested enrichment must be AND-ed with the "not
        # null" base via allOf, never substituted for it, or a null `webhook` would silently
        # satisfy the nested requirement instead of being rejected.
        return {"allOf": [_not_null(), extra]}

    allOf = []
    for type_, fields in required_by_type.items():
        then: dict = {
            "required": fields,
            "properties": {f: _not_null() for f in fields},
        }
        for foreign in foreign_null_by_type[type_]:
            then["properties"].setdefault(foreign, {"type": "null"})
        if type_ == "webhook-script":
            # fix-2 gap A: webhook-script requires source == "gitlab" specifically (schema.py:
            # "if self.source != 'gitlab': raise") — plain "webhook" only requires source
            # non-null (gitlab/github/generic all valid there), so this is NOT shared with the
            # generic non-null `source` constraint the fields loop above already set.
            then["properties"]["source"] = {"const": "gitlab"}
            # The required "webhook" block must itself carry gitlabEvents; webhook-script
            # forbids prompt/handoff (routed project/system events have no conversational
            # target for those to apply to).
            then["properties"]["webhook"] = _merge_webhook_constraint(
                {
                    "required": ["gitlabEvents"],
                    "properties": {"gitlabEvents": {"not": {"type": "null"}}},
                }
            )
            then["properties"]["prompt"] = {"type": "null"}
            then["properties"]["handoff"] = {"type": "null"}
        if type_ == "webhook":
            # If gitlabEvents is present at all, every entry must be a conversational kind —
            # project/system kinds belong to webhook-script (schema.py's `extra` check).
            then["properties"]["webhook"] = _merge_webhook_constraint(
                {
                    "properties": {
                        "gitlabEvents": {"items": {"enum": sorted(CONVERSATIONAL_GITLAB_EVENTS)}}
                    }
                }
            )
        allOf.append({"if": {"required": ["type"], "properties": {"type": {"const": type_}}}, "then": then})
    channel["allOf"] = allOf


def _patch_shared_handoff_requires_single_session(schema: dict) -> None:
    """Mirror `RuntimeConfig._shared_handoff_requires_single_session` (schema.py): a channel
    handoff always replaces the shared Workspace directory, so any channel carrying one
    forces `workspace.maxConcurrentSessions == 1`.
    """
    schema.setdefault("allOf", []).append(
        {
            "if": {
                "required": ["channels"],
                "properties": {
                    "channels": {
                        "contains": {
                            "type": "object",
                            "required": ["handoff"],
                            "properties": {"handoff": {"not": {"type": "null"}}},
                        }
                    }
                },
            },
            "then": {
                "properties": {
                    "workspace": {"type": "object", "properties": {"maxConcurrentSessions": {"const": 1}}}
                }
            },
        }
    )


def generate() -> str:
    schema = RuntimeConfig.model_json_schema(by_alias=True)
    defs = schema["$defs"]
    _patch_routing_block(defs)
    _patch_engine_block(defs)
    _patch_handoff_block(defs)
    _patch_channel_config(defs)
    _patch_shared_handoff_requires_single_session(schema)
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://ach-runtime.ackstorm.ai/schemas/ach-workspace-config-v3.schema.json",
        "title": "ach-runtime workspace-v3 RuntimeConfig",
        **schema,
    }
    return json.dumps(schema, indent=2, sort_keys=False) + "\n"


def main(argv: list[str]) -> int:
    generated = generate()
    if "--check" in argv:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else ""
        if current != generated:
            sys.stderr.write(
                f"schema drift: {SCHEMA_PATH} is stale — run `python scripts/gen_schema.py`\n"
            )
            return 1
        return 0
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(generated, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
