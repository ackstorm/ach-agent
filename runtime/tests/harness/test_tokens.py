# SPDX-License-Identifier: MIT
"""RED-first: exact legacy HMAC/Ed25519 vectors and rejection paths for the Workspace
harness-key primitives (owned slice of Task 2 -- workspace-auth-dispatch)."""

from __future__ import annotations

import subprocess
import sys

import pytest

from ach_runtime.harness.tokens import (
    WorkspaceAuthenticationFailed,
    authenticate_facade,
    engine_bearer,
    engine_verify_key,
    facade_token,
    host_token,
    load_harness_key,
    verify_engine_bearer,
    verify_facade_token,
)

KEY = b"0" * 64
OTHER_KEY = b"1" * 64


def test_engine_verify_key_matches_legacy_vector() -> None:
    assert engine_verify_key(KEY) == "lZLI1hcEx9ydNM7EoaQ213ri9oNsmILvrFE6AB2YO94"


def test_engine_verify_key_deterministic_across_calls() -> None:
    assert engine_verify_key(KEY) == engine_verify_key(KEY)


def test_engine_bearer_roundtrip() -> None:
    verify_key = engine_verify_key(KEY)
    bearer = engine_bearer(KEY, "ws-alpha")
    assert verify_engine_bearer(verify_key, bearer) == "ws-alpha"


def test_engine_bearer_rejects_wrong_key() -> None:
    verify_key = engine_verify_key(KEY)
    bearer = engine_bearer(OTHER_KEY, "ws-alpha")
    assert verify_engine_bearer(verify_key, bearer) is None


def test_engine_bearer_rejects_tampered_claim() -> None:
    verify_key = engine_verify_key(KEY)
    _claim, _, sig = engine_bearer(KEY, "ws-alpha").rpartition(".")
    assert verify_engine_bearer(verify_key, f"ws-other.{sig}") is None


def test_engine_bearer_rejects_malformed() -> None:
    verify_key = engine_verify_key(KEY)
    assert verify_engine_bearer(verify_key, "not-a-bearer") is None
    assert verify_engine_bearer(verify_key, "") is None


def test_facade_token_roundtrip() -> None:
    token = facade_token(KEY, "ws-alpha")
    assert verify_facade_token(KEY, token) == "ws-alpha"


def test_facade_token_rejects_wrong_key() -> None:
    token = facade_token(OTHER_KEY, "ws-alpha")
    assert verify_facade_token(KEY, token) is None


def test_facade_token_rejects_tampered_mac() -> None:
    claim, _, mac = facade_token(KEY, "ws-alpha").rpartition(".")
    tampered = mac[:-1] + ("0" if mac[-1] != "0" else "1")
    assert verify_facade_token(KEY, f"{claim}.{tampered}") is None


def test_facade_token_rejects_malformed() -> None:
    assert verify_facade_token(KEY, "no-dot-here") is None


def test_load_harness_key_encodes_utf8_string(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACH_SANDBOX_KEY", "legacy-k-value")
    assert load_harness_key() == b"legacy-k-value"


def test_load_harness_key_missing_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACH_SANDBOX_KEY", raising=False)
    with pytest.raises(RuntimeError):
        load_harness_key()


def test_load_harness_key_empty_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACH_SANDBOX_KEY", "")
    with pytest.raises(RuntimeError):
        load_harness_key()


async def test_authenticate_facade_verifies_hmac_then_owner() -> None:
    calls: list[str] = []

    async def owned(claim: str) -> bool:
        calls.append(claim)
        return claim == "ws-alpha"

    token = facade_token(KEY, "ws-alpha")
    claim = await authenticate_facade(KEY, token, owned_workspace=owned)
    assert claim == "ws-alpha"
    assert calls == ["ws-alpha"]


async def test_authenticate_facade_denies_foreign_owner_without_downstream() -> None:
    calls: list[str] = []

    async def owned(claim: str) -> bool:
        calls.append(claim)
        return False

    token = facade_token(KEY, "ws-other")
    with pytest.raises(WorkspaceAuthenticationFailed):
        await authenticate_facade(KEY, token, owned_workspace=owned)
    assert calls == ["ws-other"]


async def test_authenticate_facade_denies_missing_hmac_without_downstream() -> None:
    calls: list[str] = []

    async def owned(claim: str) -> bool:
        calls.append(claim)
        return True

    with pytest.raises(WorkspaceAuthenticationFailed):
        await authenticate_facade(KEY, "garbage-token", owned_workspace=owned)
    assert calls == []


def test_key_and_bearer_stable_across_module_reload() -> None:
    script = (
        "import importlib\n"
        "from ach_runtime.harness import tokens\n"
        "KEY = b'0' * 64\n"
        "CLAIM = 'ws-alpha'\n"
        "key_before = tokens.engine_verify_key(KEY)\n"
        "bearer_before = tokens.engine_bearer(KEY, CLAIM)\n"
        "importlib.reload(tokens)\n"
        "key_after = tokens.engine_verify_key(KEY)\n"
        "bearer_after = tokens.engine_bearer(KEY, CLAIM)\n"
        "assert key_before == key_after == 'lZLI1hcEx9ydNM7EoaQ213ri9oNsmILvrFE6AB2YO94'\n"
        "assert bearer_before == bearer_after\n"
    )
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_verify_facade_token_rejects_non_ascii_instead_of_raising() -> None:
    assert verify_facade_token(KEY, "workspace.é") is None
    assert verify_facade_token(KEY, "é." + facade_token(KEY, "ws").rpartition(".")[2]) is None


def test_host_token_known_answer_and_distinct_from_other_tokens() -> None:
    # Same vector as ach internal/workspacehost/client_test.go TestHostToken_KnownAnswer:
    # either side drifting fails its own suite.
    key = b"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    uid = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
    expected = "71b872fe8ceb7cd93d81b390517b7124a57dab020154a8565ccf273fae02a8ef"
    assert host_token(key, uid) == expected
    assert facade_token(key, uid) != expected
    assert engine_bearer(key, uid) != expected
