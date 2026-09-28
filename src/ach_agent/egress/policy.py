# SPDX-License-Identifier: Apache-2.0
"""Pure egress authorization logic — no mitmproxy types (design §6).

Path handling follows design §7: reject dot segments, encoded slashes/backslashes,
repeated-slash ambiguities, and invalid escapes before matching, comparing raw
canonical escaped paths with no case folding.
"""

from __future__ import annotations

import re

from ach_agent.config.schema import EgressBlock, EgressServiceBlock

_UNSAFE_PATH_RE = re.compile(
    r"\.\.|%2e%2e|%2f|%5c|\\\\|//|%00", re.IGNORECASE
)


def match_service(egress: EgressBlock, host: str, port: int) -> EgressServiceBlock | None:
    """Exact origin match, hostname case-insensitive (design §4/§7)."""
    host_lower = host.lower()
    for svc in egress.services:
        # origin was validated as https://<host>[:port] at config load (Task 1).
        origin_host, _, origin_port_s = svc.origin.removeprefix("https://").partition(":")
        origin_port = int(origin_port_s) if origin_port_s else 443
        if origin_host.lower() == host_lower and origin_port == port:
            return svc
    return None


def is_authorized(svc: EgressServiceBlock, method: str, path: str) -> bool:
    """design §5 step: evaluate access policy before credential injection.

    No access block -> every method/path at this origin is authorized (§4: "all
    supported requests at that origin can use the token").
    """
    if _UNSAFE_PATH_RE.search(path):
        return False
    if svc.access is None:
        return True
    for rule in svc.access.allow:
        if method not in rule.methods:
            continue
        if rule.path_exact and path == rule.path_exact:
            return True
        if rule.path_prefix and path.startswith(rule.path_prefix):
            return True
    return False
