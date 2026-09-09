# SPDX-License-Identifier: Apache-2.0
"""Backend-neutral memory helpers.

These live in memory/common.py so no backend imports another backend's module to get them.
The import-hygiene test at the bottom is the one that keeps it that way.
"""

from __future__ import annotations

import pytest

from ach_agent.config.schema import SecretSource
from ach_agent.memory.common import (
    inc_memory_degraded,
    probe_memory_endpoint,
    resolve_memory_secret,
)


def test_no_auth_configured_proceeds_unauthenticated() -> None:
    assert resolve_memory_secret(None) == (True, None)


def test_auth_configured_and_set_resolves(monkeypatch) -> None:
    monkeypatch.setenv("MEM_TOK", "sekret")
    assert resolve_memory_secret(SecretSource(env="MEM_TOK")) == (True, "sekret")


def test_auth_configured_but_unset_degrades_rather_than_proceeding(monkeypatch) -> None:
    """The three-way gate exists for exactly this case: collapsing it into "no auth" would
    silently turn a misconfigured secret into an anonymous call against a real backend."""
    monkeypatch.delenv("MEM_TOK", raising=False)
    assert resolve_memory_secret(SecretSource(env="MEM_TOK")) == (False, None)


@pytest.mark.asyncio
async def test_probe_returns_false_and_never_raises_on_a_dead_endpoint() -> None:
    """Fail-open (D-02): an unreachable backend is a degraded note, never an aborted event."""
    assert await probe_memory_endpoint("http://127.0.0.1:1", timeout=0.5) is False


def test_inc_memory_degraded_is_silent_when_metrics_are_unavailable() -> None:
    """A metric must never be the reason an event fails."""
    inc_memory_degraded()  # must not raise


def test_common_imports_no_backend_module() -> None:
    """memory/common.py holds what every backend needs and no backend owns. A backend import
    here would recreate exactly the coupling this module was extracted to break."""
    from pathlib import Path

    src = Path("src/ach_agent/memory/common.py").read_text(encoding="utf-8")
    for backend in ("hindsight", "codemem", "ach_memory"):
        assert f"memory.{backend}" not in src, backend
