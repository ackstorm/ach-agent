# SPDX-License-Identifier: MIT
"""A fresh agent is visible from its first scrape: identity series and zeroed known children."""

from __future__ import annotations

import subprocess
import sys
from typing import get_args

from ach_runtime.observability.runs import AnomalyKind, RunOutcome

# The metrics live in the process-global registry, so scrape a fresh interpreter: other tests
# in this process may already have incremented some children.
_SCRAPE = """
from prometheus_client import generate_latest
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.observability.metrics import IdentityRegistry, initialise
initialise(["gitlab", "nightly"])
print(generate_latest(IdentityRegistry(identity=ProcessIdentity(agent="a1", environment="prod"))).decode())
"""


def _scrape() -> str:
    return subprocess.run([sys.executable, "-c", _SCRAPE], check=True, capture_output=True, text=True).stdout


def _sample(name: str, value: str, **labels: str) -> str:
    labels |= {"agent": "a1", "environment": "prod"}
    rendered = ",".join(f'{key}="{labels[key]}"' for key in sorted(labels))
    return f"{name}{{{rendered}}} {value}"


def test_fresh_agent_exports_identity_and_zeroed_children_before_any_invocation():
    text = _scrape().splitlines()
    assert _sample("ach_agent_info", "1.0") in text
    for kind in get_args(AnomalyKind):
        assert _sample("ach_agent_anomalies_total", "0.0", kind=kind) in text, kind
    for channel in ("gitlab", "nightly"):
        for outcome in get_args(RunOutcome):
            assert _sample("ach_agent_runs_total", "0.0", channel=channel, outcome=outcome) in text, (channel, outcome)
    assert _sample("ach_agent_cost_unpriced_total", "0.0", reason="usage_missing") in text
    assert not any(line.startswith("ach_agent_turns_total{") for line in text)  # model unknown until the first turn
