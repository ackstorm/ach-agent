# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import re

from ach_agent.sandbox.tokens import (
    claim_name,
    digest,
    engine_bearer,
    engine_verify_key,
    facade_token,
    verify_engine_bearer,
    verify_facade_token,
)

K = b"k" * 32


def test_digest_is_stable_128_bit_hex() -> None:
    assert digest("gitlab:mr:1") == digest("gitlab:mr:1")
    assert re.fullmatch(r"[0-9a-f]{32}", digest("x"))


def test_claim_name_is_dns_label_without_dots() -> None:
    name = claim_name("My_Very.Long.Agent.Name.That.Keeps.Going.On", digest("k"))
    assert len(name) <= 63
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name)


def test_facade_token_derives_and_verifies() -> None:
    claim = claim_name("bot", digest("k"))
    token = facade_token(K, claim)
    assert verify_facade_token(K, token) == claim
    assert verify_facade_token(b"other" * 8, token) is None
    assert verify_facade_token(K, token[:-1] + ("0" if token[-1] != "0" else "1")) is None
    assert verify_facade_token(K, "x") is None


def test_engine_bearer_is_deterministic_and_verifiable_with_the_public_key() -> None:
    claim = claim_name("bot", digest("k"))
    bearer = engine_bearer(K, claim)
    assert bearer == engine_bearer(K, claim)  # a restarted harness re-derives it
    public = engine_verify_key(K)
    assert verify_engine_bearer(public, bearer) == claim
    assert verify_engine_bearer(engine_verify_key(b"x" * 32), bearer) is None
    assert verify_engine_bearer(public, f"other-claim.{bearer.rpartition('.')[2]}") is None
    assert verify_engine_bearer(public, "garbage") is None
    assert bearer != facade_token(K, claim)


def test_cross_repo_vector() -> None:
    # ach renders ACH_SANDBOX_VERIFY_KEY from K with the same derivation (Go:
    # ed25519.NewKeyFromSeed(HMAC-SHA256(K, "ach-sandbox-engine")).Public(), base64url, no pad).
    assert engine_verify_key(b"0" * 64) == "lZLI1hcEx9ydNM7EoaQ213ri9oNsmILvrFE6AB2YO94"
