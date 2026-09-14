"""Role-owned Unix socket paths and safe listener creation."""

from __future__ import annotations

import errno
import os
import socket
import stat
from pathlib import Path


def channel_socket_path(root: Path = Path("/run/ach-agent")) -> Path:
    return root / "channels" / "channel.sock"


def engine_socket_path(root: Path = Path("/run/ach-agent")) -> Path:
    return root / "engine" / "agent.sock"


def _reject_existing(path: Path) -> None:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        raise RuntimeError(f"refusing symlink socket path: {path}")
    if not stat.S_ISSOCK(info.st_mode):
        raise RuntimeError(f"refusing non-socket path: {path}")
    if info.st_uid != os.getuid():
        raise PermissionError(f"socket path has wrong owner: {path}")
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.05)
    try:
        try:
            probe.connect(str(path))
        except OSError as exc:
            if exc.errno not in (errno.ECONNREFUSED, errno.ENOENT, errno.ENOTCONN):
                raise RuntimeError(f"could not probe socket path: {path}") from exc
        else:
            raise RuntimeError(f"live listener already exists: {path}")
    finally:
        probe.close()
    path.unlink()


def bind_listener(path: Path) -> socket.socket:
    """Safely replace a stale owned Unix socket and return a nonblocking listener."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if path.exists() or path.is_symlink():
        _reject_existing(path)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        listener.bind(str(path))
        os.chmod(path, 0o600)
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        path.unlink(missing_ok=True)
        raise
