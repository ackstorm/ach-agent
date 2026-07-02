# SPDX-License-Identifier: Apache-2.0
import sys

import pytest

from ach_agent.security import preflight


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_harden_self_sets_dumpable_and_no_new_privs():
    preflight.harden_self()
    assert preflight._prctl(preflight.PR_GET_DUMPABLE) == 0
    assert preflight._prctl(preflight.PR_GET_NO_NEW_PRIVS) == 1


def test_run_preflight_fail_closed_exits(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [("not_root", "uid 0")])
    monkeypatch.delenv(preflight.DEGRADED_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        preflight.run_preflight()
    assert exc.value.code == 1


def test_run_preflight_degraded_override_does_not_exit(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [("not_root", "uid 0")])
    monkeypatch.setenv(preflight.DEGRADED_ENV, "1")
    preflight.run_preflight()  # must return without raising


def test_run_preflight_clean_host_does_not_exit(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [])
    monkeypatch.delenv(preflight.DEGRADED_ENV, raising=False)
    preflight.run_preflight()  # must return without raising
