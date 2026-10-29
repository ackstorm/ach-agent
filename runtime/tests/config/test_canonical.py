# SPDX-License-Identifier: MIT
"""configVersion canonicalization — golden corpus from
.superpowers/sdd/2026-09-30-workspace-runtime/config-canonicalization-proposal.md,
vendored at tests/config/fixtures/config-canonicalization.json. ACH vendors the same bytes
in internal/agentrender/testdata/config-canonicalization.json with a drift gate (Task 2B).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from ach_runtime.config.canonical import (
    CanonicalizationError,
    canonical_config_bytes,
    compute_config_version,
    load_config_json,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _load_corpus() -> list[dict]:
    with (FIXTURES_DIR / "config-canonicalization.json").open(encoding="utf-8") as f:
        return json.load(f)["cases"]


CORPUS = _load_corpus()
GOLDEN_CASES = [c for c in CORPUS if "canonicalHex" in c]
ERROR_CASES = [c for c in CORPUS if c.get("error")]


@pytest.mark.parametrize("case", GOLDEN_CASES, ids=[c["name"] for c in GOLDEN_CASES])
def test_golden_canonical_bytes_and_hash(case: dict) -> None:
    canonical = canonical_config_bytes(case["input"])
    assert canonical == bytes.fromhex(case["canonicalHex"])
    assert canonical.hex() == case["canonicalHex"]
    assert compute_config_version(case["input"]) == case["sha256Hex"]


@pytest.mark.parametrize("case", ERROR_CASES, ids=[c["name"] for c in ERROR_CASES])
def test_error_corpus_rejected_by_load_config_json(case: dict) -> None:
    if "rawJson" in case:
        raw = case["rawJson"].encode("utf-8")
    else:
        raw = base64.b64decode(case["rawBase64"])
    with pytest.raises(CanonicalizationError):
        load_config_json(raw)


def test_load_config_json_accepts_well_formed_object() -> None:
    assert load_config_json(b'{"a":1,"b":[1,2,3],"c":null}') == {"a": 1, "b": [1, 2, 3], "c": None}


def test_reorder_object_keys_same_hash() -> None:
    a = {"a": 1, "z": 2, "nested": {"x": 1, "y": 2}}
    b = {"nested": {"y": 2, "x": 1}, "z": 2, "a": 1}
    assert compute_config_version(a) == compute_config_version(b)


def test_reorder_array_entries_inside_object_still_equal() -> None:
    a = {"items": [{"a": 1, "b": 2}, {"c": 3, "d": 4}]}
    b = {"items": [{"b": 2, "a": 1}, {"d": 4, "c": 3}]}
    assert compute_config_version(a) == compute_config_version(b)


def test_reverse_array_order_changes_hash() -> None:
    a = {"items": [1, 2, 3]}
    b = {"items": [3, 2, 1]}
    assert compute_config_version(a) != compute_config_version(b)


def test_top_level_configVersion_value_does_not_affect_hash() -> None:
    a = {"a": 1, "configVersion": "x" * 64}
    b = {"a": 1, "configVersion": "y" * 64}
    assert compute_config_version(a) == compute_config_version(b)


def test_nested_configVersion_change_changes_hash() -> None:
    a = {"model": {"params": {"configVersion": "one"}}}
    b = {"model": {"params": {"configVersion": "two"}}}
    assert compute_config_version(a) != compute_config_version(b)


def test_composed_decomposed_unicode_differ() -> None:
    composed = {"name": "café"}
    decomposed = {"name": "café"}
    assert compute_config_version(composed) != compute_config_version(decomposed)


def test_remove_null_or_empty_member_changes_hash() -> None:
    present = {"a": 1, "b": None, "c": [], "d": {}}
    absent = {"a": 1}
    assert compute_config_version(present) != compute_config_version(absent)


def test_uid_change_changes_hash() -> None:
    a = {"agent": {"uid": "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"}}
    b = {"agent": {"uid": "00000000-0000-0000-0000-000000000000"}}
    assert compute_config_version(a) != compute_config_version(b)


def test_policy_change_changes_hash() -> None:
    a = {"limits": {"maxSteps": 30}}
    b = {"limits": {"maxSteps": 31}}
    assert compute_config_version(a) != compute_config_version(b)


def test_infra_alias_ref_change_changes_hash() -> None:
    a = {"ach": {"identity": {"env": "ACH_SECRET_IDENTITY"}}}
    b = {"ach": {"identity": {"env": "ACH_SECRET_IDENTITY_V2"}}}
    assert compute_config_version(a) != compute_config_version(b)


@pytest.mark.parametrize(
    "value",
    [9007199254740992, -9007199254740992, 1e400, float("nan"), float("inf"), float("-inf")],
)
def test_out_of_range_or_nonfinite_numbers_rejected(value: float) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_config_bytes({"a": value})


def test_safe_bound_and_subnormal_accepted() -> None:
    canonical_config_bytes({"a": 9007199254740991, "b": -9007199254740991, "c": 5e-324})


def test_surrogate_object_key_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_config_bytes({"\ud800": 1})


def test_nested_surrogate_object_key_rejected() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_config_bytes({"nested": {"\ud800": 1}})


def test_root_must_be_object() -> None:
    with pytest.raises(CanonicalizationError):
        canonical_config_bytes([1, 2, 3])  # type: ignore[arg-type]


def test_bool_is_not_an_integer() -> None:
    # bools pass through untouched by the numeric bound check; JCS still spells them true/false.
    assert canonical_config_bytes({"a": True}) == b'{"a":true}'
