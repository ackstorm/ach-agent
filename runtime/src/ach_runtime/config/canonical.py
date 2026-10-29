# SPDX-License-Identifier: MIT
"""configVersion canonicalization — RFC 8785/JCS pinned by
.superpowers/sdd/2026-09-30-workspace-runtime/config-canonicalization-proposal.md and
contract §5/§11.

`configVersion = lowercase_hex(SHA256(JCS_UTF8(W minus its top-level "configVersion")))`.
W is the complete resolved camelCase wire object the operator emits — nested keys named
configVersion are NOT removed, only the top-level member. Numeric profile: integers and
finite floats with abs(value) <= 9007199254740991 (binary64 safe-integer bound); NaN/Infinity,
overflowing exponents and out-of-range numbers are rejected rather than coerced. Ingress is
ordinary `json.loads`: malformed JSON and trailing data after the root value are rejected by
the standard library itself; the non-object-root and numeric-bound checks below are this
config boundary's own, not the library's. A duplicate object key resolves to its last value
(not rejected), and raw Unicode/surrogate-escape correctness is not independently validated
— contract §5 no longer claims either guarantee.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

import rfc8785

NUMERIC_BOUND = 9007199254740991


class CanonicalizationError(ValueError):
    """Raised when a wire value violates the shared numeric/Unicode profile."""


def _check_number(value: float | int, path: str) -> None:
    if isinstance(value, bool):  # bool is not an integer (explicit per proposal)
        return
    if isinstance(value, int):
        if abs(value) > NUMERIC_BOUND:
            raise CanonicalizationError(f"{path}: integer out of safe range: {value!r}")
        return
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            raise CanonicalizationError(f"{path}: NaN/Infinity is not a finite number")
        if abs(value) > NUMERIC_BOUND:
            raise CanonicalizationError(f"{path}: float out of safe range: {value!r}")
        return


def validate_json_value(value: Any, path: str = "$") -> None:
    """Recursively validate a decoded JSON value against the shared numeric profile.

    Applies to both freshly-decoded JSON (load_config_json) and already-decoded dict/list
    structures passed to parse_config.
    """
    if isinstance(value, dict):
        for key, sub in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError(f"{path}: object keys must be strings: {key!r}")
            validate_json_value(sub, f"{path}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            validate_json_value(item, f"{path}[{i}]")
    elif value is None or isinstance(value, (bool, str)):
        pass
    elif isinstance(value, (int, float)):
        _check_number(value, path)
    else:
        raise CanonicalizationError(f"{path}: not a JSON-representable scalar: {type(value)!r}")


def canonical_config_bytes(wire: dict[str, Any]) -> bytes:
    """RFC 8785/JCS bytes of `wire` with its top-level "configVersion" member removed.

    Nested members literally named configVersion are preserved — only the root member is
    excluded (contract §5/§11).
    """
    if not isinstance(wire, dict):
        raise CanonicalizationError("canonicalization root must be a JSON object")
    validate_json_value(wire)
    stripped = {k: v for k, v in wire.items() if k != "configVersion"}
    try:
        return rfc8785.dumps(stripped)
    except (rfc8785.CanonicalizationError, UnicodeEncodeError) as exc:
        # rfc8785 itself rejects a string it cannot losslessly re-encode as UTF-8 (e.g. a lone
        # surrogate value) as CanonicalizationError — the one raw-Unicode guarantee still
        # enforced, by the library, not a custom scanner. A lone surrogate OBJECT KEY instead
        # reaches here as UnicodeEncodeError, from the library's own key-sorting encode call
        # (which only catches AttributeError itself). Both are re-raised as our own
        # CanonicalizationError so callers (schema.py) keep catching a single exception type
        # at this boundary.
        raise CanonicalizationError(str(exc)) from exc


def compute_config_version(wire: dict[str, Any]) -> str:
    """64 lowercase hex characters, no prefix — SHA-256 of canonical_config_bytes(wire)."""
    return hashlib.sha256(canonical_config_bytes(wire)).hexdigest()


def load_config_json(raw: bytes) -> dict[str, Any]:
    """Ordinary bytes -> dict loader for the resolved RuntimeConfig wire JSON.

    Decodes UTF-8 and parses with the standard `json` module: malformed JSON and trailing data
    after the root value are rejected by the standard library itself; the non-object-root
    check just below is this config boundary's own. A duplicate object key resolves to its
    last value (not rejected — no framework for that exists). `json.loads` accepts
    NaN/Infinity/-Infinity as a non-standard extension; validate_json_value below rejects them
    (and any other out-of-range number) as part of the shared numeric profile.
    """
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CanonicalizationError(f"invalid UTF-8 in config bytes: {exc}") from exc

    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CanonicalizationError(f"malformed JSON config: {exc}") from exc

    if not isinstance(value, dict):
        raise CanonicalizationError("config root must be a JSON object")
    validate_json_value(value)
    return value
