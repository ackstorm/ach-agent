# SPDX-License-Identifier: Apache-2.0
"""Boot-time security preflight — harden the harness, refuse unsafe hosts.

Threat: opencode (the agente) runs in the SAME container as the harness. Even with
clean-slate env hygiene (engine.build_opencode_env), a same-UID peer could read the
harness's decrypted secrets via /proc/<harness>/environ, /proc/<harness>/mem, or
PTRACE_ATTACH.

Two layers:
  Class A — ENFORCE (our own syscalls, verified, hard-fail, NO override):
    * PR_SET_DUMPABLE=0     -> reowns /proc/self/{environ,mem,maps} to root:root and
      blocks same-UID ptrace. Closes the /proc env + memory theft vector.
    * PR_SET_NO_NEW_PRIVS=1 -> inherited by opencode; blocks privilege regain via
      setuid / file-capability binaries, so dumpable=0 cannot be undone by a
      re-privileged child.
  Class B — GATE (host properties we can only detect, fail-closed; override via
    ACH_INSECURE_ALLOW_DEGRADED=1 for local/dev only):
    * not running as root (a root peer reads anything)
    * no CAP_SYS_PTRACE in the effective set (ptrace defeats dumpable=0)
    * no CAP_SYS_ADMIN in the effective set
    Soft (warn-only, defense-in-depth): seccomp filter active.

Linux-only (prctl + /proc). On non-Linux (mac dev) it no-ops with a warning —
production is always a Linux container.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# prctl option numbers (linux/prctl.h)
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39

# capability bit positions (linux/capability.h)
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21

DEGRADED_ENV = "ACH_INSECURE_ALLOW_DEGRADED"


def parse_status(text: str) -> dict[str, str]:
    """Parse /proc/<pid>/status into {field: value} (value = text after the colon)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _cap_has(cap_eff: int, bit: int) -> bool:
    return bool(cap_eff & (1 << bit))


def evaluate_gates(
    uid: int, cap_eff: int, seccomp: int, cap_bnd: int = 0
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Pure gate evaluator. Returns (hard_failures, warnings) as (name, detail) lists.

    Hard gates are exactly the properties that would defeat Class A hardening for a
    same-UID peer. Everything else (seccomp, bounding set) is defense-in-depth and
    warn-only. A non-empty bounding set (cap_drop: ALL not applied) is a nudge, NOT a
    hard failure: no_new_privs already blocks a non-root child from acquiring any of
    those caps via setuid/file-cap binaries, so the bounding set is a second wall.
    """
    hard: list[tuple[str, str]] = []
    warn: list[tuple[str, str]] = []
    if uid == 0:
        hard.append(("not_root", "running as uid 0 — a root peer can read any secret"))
    if _cap_has(cap_eff, CAP_SYS_PTRACE):
        hard.append(("no_cap_sys_ptrace", "CAP_SYS_PTRACE held — defeats PR_SET_DUMPABLE=0"))
    if _cap_has(cap_eff, CAP_SYS_ADMIN):
        hard.append(("no_cap_sys_admin", "CAP_SYS_ADMIN held — broad host access"))
    if seccomp != 2:
        warn.append(("seccomp_filter", f"no seccomp filter active (Seccomp={seccomp})"))
    if cap_bnd != 0:
        warn.append(
            (
                "cap_bounding_set",
                f"bounding set not empty (CapBnd={cap_bnd:#x}) — consider cap_drop: ALL",
            )
        )
    return hard, warn


def _prctl(option: int, arg2: int = 0) -> int:
    """Thin ctypes wrapper over prctl(2). Returns the raw syscall result.

    SET options return 0 on success / -1 on error; GET options return the value.
    """
    libc = ctypes.CDLL(None)
    return int(libc.prctl(option, arg2, 0, 0, 0))


def harden_self() -> None:
    """Class A: set + verify dumpable=0 and no_new_privs=1. Hard-fail on any miss.

    Our own syscalls — a failure is a broken platform, not deploy policy, so there is
    NO override. Must run before any secret enters process memory.
    """
    if sys.platform != "linux":
        log.warning("preflight: non-Linux platform, process hardening skipped")
        return
    if _prctl(PR_SET_DUMPABLE, 0) != 0 or _prctl(PR_GET_DUMPABLE) != 0:
        log.error("preflight: PR_SET_DUMPABLE=0 failed to apply — refusing to start")
        sys.exit(1)
    if _prctl(PR_SET_NO_NEW_PRIVS, 1) != 0 or _prctl(PR_GET_NO_NEW_PRIVS) != 1:
        log.error("preflight: PR_SET_NO_NEW_PRIVS=1 failed to apply — refusing to start")
        sys.exit(1)
    log.info("preflight: process hardened", dumpable=0, no_new_privs=1)


def check_gates() -> list[tuple[str, str]]:
    """Read real host properties, log soft warnings, return the hard-gate failures."""
    if sys.platform != "linux":
        return []
    status = parse_status(Path("/proc/self/status").read_text(encoding="utf-8"))
    cap_eff = int(status.get("CapEff", "0"), 16)
    cap_bnd = int(status.get("CapBnd", "0"), 16)
    seccomp = int(status.get("Seccomp", "0") or "0")
    hard, warn = evaluate_gates(os.getuid(), cap_eff, seccomp, cap_bnd)
    for name, detail in warn:
        log.warning("preflight: soft check", gate=name, detail=detail)
    return hard


def run_preflight() -> None:
    """Boot entrypoint: enforce Class A, then evaluate Class B gates (fail-closed).

    ACH_INSECURE_ALLOW_DEGRADED=1 downgrades Class B hard failures to warnings
    (local/dev only). It NEVER affects Class A.
    """
    harden_self()
    failures = check_gates()
    if not failures:
        log.info("preflight: host gates passed")
        return
    degraded_ok = os.environ.get(DEGRADED_ENV) == "1"
    for name, detail in failures:
        if degraded_ok:
            log.warning("preflight: DEGRADED — gate bypassed", gate=name, detail=detail)
        else:
            log.error("preflight: host gate failed", gate=name, detail=detail)
    if not degraded_ok:
        log.error(
            "preflight: refusing to start on an unsafe host — fix the securityContext "
            "or set ACH_INSECURE_ALLOW_DEGRADED=1 to override (dev only)"
        )
        sys.exit(1)
