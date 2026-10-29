# SPDX-License-Identifier: MIT
"""Legacy HMAC/Ed25519 primitives, reused verbatim for the Workspace facade and engine
auth paths (owned slice of Task 2 -- workspace-auth-dispatch).

Copied only from old-ach-agent/src/ach_agent/sandbox/tokens.py: _b64, _unb64, _mac,
_signing_key and the cryptography calls. The claim topology does not carry over -- a
claim here is the safe derived Workspace name, not a sandbox agent/key-digest slug -- so
old digest()/claim_name() are not ported.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from collections.abc import Awaitable, Callable

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

_ENGINE_SEED_LABEL = b"ach-sandbox-engine"


class WorkspaceAuthenticationFailed(Exception):
    """Facade token verified but the claimed Workspace is not owned/live."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(key: bytes, purpose: str, claim: str) -> str:
    return hmac.new(key, f"{purpose}:{claim}".encode(), hashlib.sha256).hexdigest()


def facade_token(key: bytes, claim: str) -> str:
    """Workspace -> harness gateway token; self-describing so any harness holding K verifies it."""
    return f"{claim}.{_mac(key, 'facade', claim)}"


def host_token(key: bytes, agent_uid: str) -> str:
    """Host -> control `/host/v1` bearer (contract 5.2). Not a facade token and not an engine bearer."""
    return _mac(key, "host", agent_uid)


def verify_facade_token(key: bytes, token: str) -> str | None:
    claim, sep, mac = token.rpartition(".")
    if not sep or not claim:
        return None
    # Bytes, not str: compare_digest raises on non-ASCII str instead of returning False.
    return claim if hmac.compare_digest(mac.encode(), _mac(key, "facade", claim).encode()) else None


def _signing_key(key: bytes) -> Ed25519PrivateKey:
    seed = hmac.new(key, _ENGINE_SEED_LABEL, hashlib.sha256).digest()
    return Ed25519PrivateKey.from_private_bytes(seed)


def engine_verify_key(key: bytes) -> str:
    """Public half, rendered into the SandboxTemplate as ACH_SANDBOX_VERIFY_KEY."""
    public = _signing_key(key).public_key()
    return _b64(public.public_bytes(Encoding.Raw, PublicFormat.Raw))


def engine_bearer(key: bytes, claim: str) -> str:
    """Harness -> mini-harness bearer for one claim (Ed25519 is deterministic)."""
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


def load_harness_key(env_name: str = "ACH_SANDBOX_KEY") -> bytes:
    """Explicit K source for this slice: no random/ek fallback, no deployment assumed."""
    value = os.environ.get(env_name)
    if not value:
        raise RuntimeError(f"{env_name} is required and must not be empty")
    return value.encode()


async def authenticate_facade(
    key: bytes, token: str, *, owned_workspace: Callable[[str], Awaitable[bool]]
) -> str:
    """Legacy gateway _claim semantics: verify HMAC, then the owned-live callback."""
    claim = verify_facade_token(key, token)
    if claim is None or not await owned_workspace(claim):
        raise WorkspaceAuthenticationFailed(claim or "<malformed token>")
    return claim
