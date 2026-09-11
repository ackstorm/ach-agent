"""Small Linux process supervisor for native engine launches.

The supervisor is deliberately one purpose: keep a native engine's orphaned descendants in
its process tree until the owning ``ManagedServer`` has joined cleanup.  ``tini`` remains the
container init/reaper; this local supervisor only provides per-launch subreaper ownership.
"""

from __future__ import annotations

import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def command(argv: list[str]) -> list[str]:
    """Build a launch command that does not depend on ambient ``PYTHONPATH``."""
    return [sys.executable, str(Path(__file__).resolve()), "--", *argv]


_PR_SET_CHILD_SUBREAPER = 36
_DESCENDANT_CLEANUP_TIMEOUT_S = 10.0


def _set_child_subreaper() -> None:
    """Make this one supervisor the adoption point for its orphaned descendants."""
    libc = ctypes.CDLL(None, use_errno=True)
    result = libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def _children_of(pid: int, *, include_zombies: bool = False) -> list[int]:
    if not Path("/proc").is_dir():
        return []
    children: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "stat").read_bytes()
            fields = raw[raw.rfind(b")") + 2 :].split()
            state = fields[0]
            parent = int(fields[1])
        except (OSError, ValueError, IndexError, UnicodeDecodeError):
            continue
        if parent == pid and (include_zombies or state != b"Z"):
            children.append(int(entry.name))
    return children


def _reap_children(exclude_pid: int) -> None:
    # Reap adopted descendants by PID.  waitpid(-1) could accidentally reap the native
    # leader between child.poll() and this call, losing Popen's authoritative exit status.
    for pid in _children_of(os.getpid(), include_zombies=True):
        if pid == exclude_pid:
            continue
        try:
            os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            continue


def _signal_children(sig: signal.Signals) -> None:
    """Signal direct adopted children; descendants are reparented as each exits."""
    pending = _children_of(os.getpid())
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(_children_of(pid))
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            continue


def run(argv: list[str]) -> int:
    _set_child_subreaper()
    stop_requested = False
    child: subprocess.Popen[bytes] | None = None

    def request_stop(_sig: int, _frame: object) -> None:
        nonlocal stop_requested
        stop_requested = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    child = subprocess.Popen(argv)
    leader_exited_at: float | None = None
    while True:
        if stop_requested:
            _signal_children(signal.SIGTERM)
            if child.poll() is None:
                try:
                    child.terminate()
                except ProcessLookupError:
                    pass
        child_rc = child.poll()
        if child_rc is not None and leader_exited_at is None:
            leader_exited_at = time.monotonic()
            _signal_children(signal.SIGTERM)
        if (
            leader_exited_at is not None
            and time.monotonic() - leader_exited_at >= _DESCENDANT_CLEANUP_TIMEOUT_S
            and _children_of(os.getpid())
        ):
            _signal_children(signal.SIGKILL)
        _reap_children(child.pid)
        if child_rc is not None and not _children_of(os.getpid()):
            return child_rc
        # Keep the supervisor alive after the engine leader exits so adopted descendants
        # remain attributable to this launch until ManagedServer.stop() signals this root.
        time.sleep(0.02)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] != "--":
        print("usage: process_supervisor -- COMMAND [ARGS...]", file=sys.stderr)
        return 2
    try:
        return run(sys.argv[2:])
    except OSError as exc:
        if exc.errno != errno.EINTR:
            print(f"process supervisor failed: {exc}", file=sys.stderr)
        return 125


if __name__ == "__main__":
    raise SystemExit(main())
