# SPDX-License-Identifier: Apache-2.0
"""Pure egress origin-matching logic — no mitmproxy types, no os.environ (design §6).

Scope: credential substitution only. A request to a declared services[] origin gets its
credential replaced; everything else passes through untouched. No destination filtering,
no method/path policy — the upstream token's own scope is the only limit (design §1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ResolvedService:
    name: str
    host: str  # lowercase, from the validated origin
    port: int
    header: str
    prefix: str
    secret: str = field(repr=False)  # never in a repr/log line
    placeholder_env: str


def match_service(
    services: Sequence[ResolvedService], host: str, port: int
) -> ResolvedService | None:
    """Exact origin match; hostname case-insensitive (design §4/§7)."""
    host = host.lower()
    return next((s for s in services if s.host == host and s.port == port), None)
