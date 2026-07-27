# SPDX-License-Identifier: Apache-2.0
"""Process-authoritative agent identity for metrics and governed egress."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

_IDENTITY_HEADER_NAMES = frozenset({"x-ach-agent", "x-ach-environment"})


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    agent: str
    environment: str


_current = ProcessIdentity(agent="", environment="")


def configure(agent: str, environment: str) -> None:
    """Commit validated identity for this harness process."""
    global _current
    _current = ProcessIdentity(agent=agent, environment=environment)


def current() -> ProcessIdentity:
    """Return the current immutable identity snapshot."""
    return _current


def with_identity_headers(
    headers: Mapping[str, str], identity: ProcessIdentity | None = None
) -> dict[str, str]:
    """Replace any caller identity with exactly one authoritative header pair."""
    source = current() if identity is None else identity
    result = {
        key: value
        for key, value in headers.items()
        if key.lower() not in _IDENTITY_HEADER_NAMES
    }
    result["x-ach-agent"] = source.agent
    result["x-ach-environment"] = source.environment
    return result


def reset_for_testing() -> None:
    """Clear module state between tests; production never calls this."""
    configure("", "")
