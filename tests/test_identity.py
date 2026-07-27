# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import pytest

from ach_agent import identity
from ach_agent.identity import ProcessIdentity


@pytest.fixture(autouse=True)
def _reset_identity() -> None:
    identity.reset_for_testing()
    yield
    identity.reset_for_testing()


def test_configure_and_current_return_immutable_snapshot() -> None:
    identity.configure("classifier", "platform")
    assert identity.current() == ProcessIdentity(agent="classifier", environment="platform")


def test_identity_headers_strip_case_insensitively_and_process_wins() -> None:
    identity.configure("classifier", "platform")
    headers = identity.with_identity_headers(
        {
            "Accept": "application/json",
            "X-Ach-Agent": "spoofed-agent",
            "x-ACH-environment": "spoofed-environment",
        }
    )
    assert headers == {
        "Accept": "application/json",
        "x-ach-agent": "classifier",
        "x-ach-environment": "platform",
    }


def test_explicit_bootstrap_identity_does_not_mutate_process_state() -> None:
    identity.configure("committed", "stable")
    headers = identity.with_identity_headers(
        {"x-ach-key": "ek-test"},
        ProcessIdentity(agent="hydrate-agent", environment="requested-environment"),
    )
    assert headers == {
        "x-ach-key": "ek-test",
        "x-ach-agent": "hydrate-agent",
        "x-ach-environment": "requested-environment",
    }
    assert identity.current() == ProcessIdentity(agent="committed", environment="stable")
