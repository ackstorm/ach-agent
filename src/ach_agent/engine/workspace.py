# SPDX-License-Identifier: Apache-2.0
"""Engine-owned shared workspace path and public-context linking."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


class WorkspaceHookFailed(RuntimeError):
    """A workspace path or public-context link could not be prepared."""


def workspace_dir(work_dir: str, session_key: str) -> Path:
    slug = _SLUG_UNSAFE.sub("-", session_key)[:64].strip("-.") or "s"
    return Path(work_dir) / f"{slug}-{hashlib.sha256(session_key.encode()).hexdigest()[:8]}"


def _ensure_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        if path.is_symlink() or not path.is_dir():
            raise WorkspaceHookFailed(f"workspace path is not a directory: {path}")
        return
    path.mkdir(parents=True, exist_ok=True)


def prepare_workspace(home: str, work_dir: str, session_key: str) -> Path:
    root = Path(work_dir)
    _ensure_directory(root)
    workspace = workspace_dir(work_dir, session_key)
    _ensure_directory(workspace)
    state = Path(home) / ".ach-state"
    _ensure_directory(state)
    link = workspace / ".ach-state"
    if link.exists() or link.is_symlink():
        if not link.is_symlink() or link.resolve() != state.resolve():
            raise WorkspaceHookFailed(f"workspace state path is not the public state link: {link}")
    else:
        link.symlink_to(state, target_is_directory=True)
    return workspace
