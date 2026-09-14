from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest

from ach_agent.boot.ipc import bind_listener, channel_socket_path, engine_socket_path


def test_socket_paths_are_distinct_under_one_root(tmp_path: Path) -> None:
    assert channel_socket_path(tmp_path) == tmp_path / "channels" / "channel.sock"
    assert engine_socket_path(tmp_path) == tmp_path / "engine" / "agent.sock"
    assert channel_socket_path(tmp_path) != engine_socket_path(tmp_path)


def test_bind_listener_creates_private_nonblocking_socket(tmp_path: Path) -> None:
    path = engine_socket_path(tmp_path)
    listener = bind_listener(path)
    try:
        assert not listener.getblocking()
        assert stat_mode(path) == 0o600
        assert stat_mode(path.parent) == 0o700
    finally:
        listener.close()
        path.unlink()


def test_bind_listener_replaces_stale_owned_socket(tmp_path: Path) -> None:
    path = channel_socket_path(tmp_path)
    path.parent.mkdir(parents=True)
    old = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    old.bind(str(path))
    old.close()
    listener = bind_listener(path)
    listener.close()
    path.unlink()


def test_bind_listener_preserves_existing_writable_parent_mode(tmp_path: Path) -> None:
    path = engine_socket_path(tmp_path)
    path.parent.mkdir(parents=True)
    os.chmod(path.parent, 0o770)

    listener = bind_listener(path)
    try:
        assert stat_mode(path.parent) == 0o770
    finally:
        listener.close()
        path.unlink()


def test_bind_listener_rejects_symlink(tmp_path: Path) -> None:
    path = engine_socket_path(tmp_path)
    path.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("keep")
    path.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        bind_listener(path)
    assert target.read_text() == "keep"


def stat_mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777
