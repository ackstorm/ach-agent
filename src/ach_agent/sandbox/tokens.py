# SPDX-License-Identifier: Apache-2.0
"""Per-sandbox identifiers and secrets — derived from K, never stored.

Kubernetes is the registry (a live sandbox is a labelled SandboxClaim), so a restarted
harness re-derives everything here from the per-agent key K, which only the harness holds.

Two directions, two schemes:
  * sandbox → harness (FacadeGateway): HMAC facade token. Only the harness verifies it.
  * harness → sandbox (mini-harness API): Ed25519 bearer. The sandbox verifies it with the
    PUBLIC key from its SandboxTemplate env, so no secret ever enters the sandbox and no
    other pod can bind a warm sandbox first (no NetworkPolicy — decision 1).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_SLUG = re.compile(r"[^a-z0-9]+")
_ENGINE_SEED_LABEL = b"ach-sandbox-engine"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def claim_name(agent: str, key_digest: str) -> str:
    slug = _SLUG.sub("-", agent.lower())[:26].strip("-") or "agent"
    return f"ach-{slug}-{key_digest}"


def _mac(key: bytes, purpose: str, claim: str) -> str:
    return hmac.new(key, f"{purpose}:{claim}".encode(), hashlib.sha256).hexdigest()


def facade_token(key: bytes, claim: str) -> str:
    """Sandbox → harness gateway token; self-describing so any harness holding K verifies it."""
    return f"{claim}.{_mac(key, 'facade', claim)}"


def verify_facade_token(key: bytes, token: str) -> str | None:
    claim, sep, mac = token.rpartition(".")
    if not sep or not claim:
        return None
    return claim if hmac.compare_digest(mac, _mac(key, "facade", claim)) else None


def _signing_key(key: bytes) -> Ed25519PrivateKey:
    seed = hmac.new(key, _ENGINE_SEED_LABEL, hashlib.sha256).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def engine_verify_key(key: bytes) -> str:
    """Public half, rendered by ach into the SandboxTemplate as ACH_SANDBOX_VERIFY_KEY."""
    public = _signing_key(key).public_key()
    return _b64(public.public_bytes(Encoding.Raw, PublicFormat.Raw))


def engine_bearer(key: bytes, claim: str) -> str:
    """Harness → mini-harness bearer for one claim (Ed25519 is deterministic)."""
    return f"{claim}.{_b64(_signing_key(key).sign(f'ach-engine:{claim}'.encode()))}"


def verify_engine_bearer(verify_key: str, bearer: str) -> str | None:
    """Return the claim a bearer was signed for, or None."""
    claim, sep, sig = bearer.rpartition(".")
    if not sep or not claim:
        return None
    try:
        Ed25519PublicKey.from_public_bytes(_unb64(verify_key)).verify(
            _unb64(sig), f"ach-engine:{claim}".encode()
        )
    except (InvalidSignature, ValueError):
        return None
    return claim
