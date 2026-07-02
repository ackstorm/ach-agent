# SPDX-License-Identifier: Apache-2.0
from ach_agent.security.preflight import (
    CAP_SYS_ADMIN,
    CAP_SYS_PTRACE,
    evaluate_gates,
    parse_status,
)


def test_parse_status_extracts_fields():
    text = "Name:\tpython\nCapEff:\t0000000000000000\nSeccomp:\t2\n"
    status = parse_status(text)
    assert status["Name"] == "python"
    assert status["CapEff"] == "0000000000000000"
    assert status["Seccomp"] == "2"


def test_evaluate_gates_clean_host_passes():
    hard, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=2)
    assert hard == []
    assert warn == []


def test_evaluate_gates_root_is_hard_failure():
    hard, _ = evaluate_gates(uid=0, cap_eff=0, seccomp=2)
    assert [name for name, _ in hard] == ["not_root"]


def test_evaluate_gates_ptrace_cap_is_hard_failure():
    hard, _ = evaluate_gates(uid=1000, cap_eff=1 << CAP_SYS_PTRACE, seccomp=2)
    assert "no_cap_sys_ptrace" in [name for name, _ in hard]


def test_evaluate_gates_admin_cap_is_hard_failure():
    hard, _ = evaluate_gates(uid=1000, cap_eff=1 << CAP_SYS_ADMIN, seccomp=2)
    assert "no_cap_sys_admin" in [name for name, _ in hard]


def test_evaluate_gates_no_seccomp_is_soft_warning_only():
    hard, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=0)
    assert hard == []
    assert [name for name, _ in warn] == ["seccomp_filter"]


def test_evaluate_gates_nonempty_bounding_set_is_soft_warning_only():
    # Docker default bounding set (cap_drop: ALL not applied) — nudge, never a hard failure.
    hard, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=2, cap_bnd=0xA80425FB)
    assert hard == []
    assert "cap_bounding_set" in [name for name, _ in warn]


def test_evaluate_gates_empty_bounding_set_no_bounding_warn():
    # cap_drop: ALL applied → CapBnd == 0 → no bounding-set warning.
    _, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=2, cap_bnd=0)
    assert "cap_bounding_set" not in [name for name, _ in warn]
