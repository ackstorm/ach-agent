# SPDX-License-Identifier: Apache-2.0
"""Resolve egress service secrets into ResolvedService (design §6).

Fail-closed: missing, empty, or malformed secrets raise before the proxy starts —
"Missing, empty, or malformed secrets fail readiness before the engine starts" (§6).
"""

from __future__ import annotations

from ach_agent.config.schema import EgressBlock, resolve_secret
from ach_agent.egress.policy import ResolvedService


class EgressConfigError(Exception):
    """An egress service's secret cannot be resolved safely."""


def resolve_services(egress: EgressBlock) -> list[ResolvedService]:
    resolved: list[ResolvedService] = []
    for svc in egress.services:
        value = resolve_secret(svc.auth.secret)  # stripped, or None when unset
        if not value:
            raise EgressConfigError(f"egress service {svc.name!r}: secret.env is unset or empty")
        if any(ch in value for ch in ("\r", "\n", "\x00")):
            raise EgressConfigError(f"egress service {svc.name!r}: secret has invalid header characters")
        # origin validated as https://<host>[:port] at config load (Task 1).
        host, _, port = svc.origin.removeprefix("https://").partition(":")
        resolved.append(
            ResolvedService(
                name=svc.name,
                host=host.lower(),
                port=int(port) if port else 443,
                header=svc.auth.header,
                prefix=svc.auth.prefix,
                secret=value,
                placeholder_env=svc.auth.placeholder_env,
                access=svc.access,
            )
        )
    return resolved
