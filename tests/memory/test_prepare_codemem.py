# SPDX-License-Identifier: Apache-2.0
"""Tests for prepare_codemem — fail-open PATH probe (MEM-02, D-02)."""

from ach_agent.config.schema import CodememMemory, CodememParams
from ach_agent.memory.adapter import prepare_codemem


def test_available_when_on_path(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: "/usr/bin/codemem")
    params = CodememParams(db_path="/var/lib/codemem/a.db", project="ach-agent")
    cfg = CodememMemory(type="codemem", codemem=params)
    assert prepare_codemem(cfg) == (True, "/var/lib/codemem/a.db")


def test_degrades_when_absent(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda name: None)
    params = CodememParams(db_path="/var/lib/codemem/a.db", project="ach-agent")
    cfg = CodememMemory(type="codemem", codemem=params)
    assert prepare_codemem(cfg) == (False, "")
