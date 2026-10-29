# SPDX-License-Identifier: MIT
"""Opaque refs/digests over workspace and session identity (contract §2/§11).

`workspace_key`/`session_key` are channel-authored opaque strings that may contain ':',
'/', or Unicode — never split or interpolated into a path or host name directly. Every
name/path derived from them goes through a SHA-256 digest over an unambiguous JSON array
(order is inherent to a JSON array, so no key-canonicalization step is needed, unlike the
object-shaped `config.canonical` wire digest).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence

_WORKSPACE_NAME_PREFIX = "agent-"
_WORKSPACE_REF_HEX_LEN = 20
_AGENT_NAME_MAX_LEN = 25
_AGENT_NAME_HASH_LEN = 8
# DNS-1123 subdomain: the ACHAgent metadata.name shape.
_AGENT_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$")


def _digest(parts: Sequence[str]) -> str:
    encoded = json.dumps(list(parts), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def workspace_ref(namespace: str, agent_uid: str, workspace_key: str) -> str:
    """Full 64-hex-char digest identifying a Workspace — kept in full for collision rejection."""
    return _digest([namespace, agent_uid, workspace_key])


def workspace_name(agent_name: str, workspace_ref_hex: str) -> str:
    """`agent-<agent-name-part>-<first20hex-of-workspace_ref>` (contract §11) — at most 52 chars.

    Shares the control workload's `agent-<agent name>` stem so an agent's execution units sort
    next to its control unit; the ACHAgent CRD rejects names ending in `-<20 hex>`, so a control
    name can never equal a Workspace name. Names over 25 chars become their first 16 chars +
    "-" + 8 hex of sha256(full name). Dots map to "-" so the name is a DNS-1035 label.
    Uniqueness comes from the ref (it already digests namespace + agent UID); ownership checks
    use the full UID, never this name. Short enough that a host can append "-<revision hash>"
    and still fit a 63-char label value."""
    return _dns_name(_WORKSPACE_NAME_PREFIX, _AGENT_NAME_MAX_LEN, agent_name, workspace_ref_hex)


def _dns_name(prefix: str, max_part: int, agent_name: str, workspace_ref_hex: str) -> str:
    if len(agent_name) > 253 or not _AGENT_NAME_RE.fullmatch(agent_name):
        raise ValueError(f"agent name is not a DNS-1123 name: {agent_name!r}")
    part = agent_name.replace(".", "-")
    if len(part) > max_part:
        digest = hashlib.sha256(agent_name.encode("utf-8")).hexdigest()[:_AGENT_NAME_HASH_LEN]
        part = f"{part[: max_part - _AGENT_NAME_HASH_LEN - 1].rstrip('-')}-{digest}"
    return f"{prefix}{part}-{workspace_ref_hex[:_WORKSPACE_REF_HEX_LEN]}"


def session_ref(workspace_ref_hex: str, session_key: str) -> str:
    """Session digest scoped BY the Workspace ref, not raw namespace/agent_uid again."""
    return _digest([workspace_ref_hex, session_key])


def invocation_digest(agent_uid: str, channel: str, event_id: str) -> str:
    """Stable invocation id — ACHAgent UID + channel + eventId (contract §2)."""
    return _digest([agent_uid, channel, event_id])
