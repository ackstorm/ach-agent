# SPDX-License-Identifier: Apache-2.0
"""Pure egress authorization logic — no mitmproxy types, no os.environ (design §6).

Path handling (design §7): only the path is authorized — the query is split off first.
The raw escaped path is checked segment by segment and rejected on dot segments (literal
or encoded), empty segments (repeated slashes), backslashes, encoded slash/backslash/dot,
any "%25" (double encoding), and invalid escapes. No normalization, no case folding: a
path is either safe as sent or denied, so the forwarded representation is the one that
was authorized.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field

from ach_agent.config.schema import EgressServiceAccess

_INVALID_ESCAPE_RE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_ENCODED_CONTROL_RE = re.compile(r"%(2e|2f|5c|25)", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedService:
    name: str
    host: str  # lowercase, from the validated origin
    port: int
    header: str
    prefix: str
    secret: str = field(repr=False)  # never in a repr/log line
    placeholder_env: str
    access: EgressServiceAccess | None


def match_service(
    services: Sequence[ResolvedService], host: str, port: int
) -> ResolvedService | None:
    """Exact origin match; hostname case-insensitive (design §4/§7)."""
    host = host.lower()
    return next((s for s in services if s.host == host and s.port == port), None)


def _path_is_safe(path: str) -> bool:
    if not path.startswith("/") or "\\" in path:
        return False
    if _INVALID_ESCAPE_RE.search(path) or _ENCODED_CONTROL_RE.search(path):
        return False
    segments = path.split("/")[1:]
    # A trailing "" is the legitimate trailing slash; any other empty segment is "//".
    return all(s not in ("", ".", "..") for s in segments[:-1]) and segments[-1] not in (".", "..")


def is_authorized(access: EgressServiceAccess | None, method: str, target: str) -> bool:
    """design §5/§6 step 5: decide before any credential is injected.

    `target` is the request-target as mitmproxy's `flow.request.path` gives it (path +
    query). No access block → every method and safe path at the origin (§4).
    """
    path = target.partition("?")[0]
    if not _path_is_safe(path):
        return False
    if access is None:
        return True
    return any(
        method in rule.methods
        and (path == rule.path_exact if rule.path_exact else path.startswith(rule.path_prefix))
        for rule in access.allow
    )
