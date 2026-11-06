"""Process hardening shared by every harness role (control, execution, local)."""

from __future__ import annotations

import ctypes
import logging

_LOG = logging.getLogger(__name__)

# linux/prctl.h
_PR_SET_DUMPABLE = 4
_PR_GET_DUMPABLE = 3


def set_non_dumpable() -> bool:
    """Mark this process non-dumpable so same-uid processes cannot read its ``/proc`` entries.

    The harness holds channel, ACH and per-Agent key secrets in its environment. Hook children on
    the control pod, and the agent's shell tool in local mode, run as the same uid and could
    otherwise read those from ``/proc/<pid>/environ``. With ``PR_SET_DUMPABLE=0`` the kernel makes
    this process's ``/proc`` node root-owned, so an unprivileged same-uid peer cannot read it.

    Best-effort and Linux-only: returns True only when the process is confirmed non-dumpable.
    """
    try:
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        if libc.prctl(_PR_SET_DUMPABLE, 0, 0, 0, 0) != 0:
            _LOG.warning("set_non_dumpable: prctl(PR_SET_DUMPABLE, 0) failed; harness env stays readable to same-uid processes")
            return False
        if libc.prctl(_PR_GET_DUMPABLE, 0, 0, 0, 0) != 0:
            _LOG.warning("set_non_dumpable: process is still dumpable after PR_SET_DUMPABLE")
            return False
    except OSError as exc:
        _LOG.warning("set_non_dumpable: prctl unavailable (%s); harness env stays readable to same-uid processes", exc)
        return False
    return True
