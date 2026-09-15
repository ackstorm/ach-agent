# SPDX-License-Identifier: Apache-2.0
"""Filesystem path helpers used at boot: PID file, engine home/workDir, harness logs."""

from __future__ import annotations

import os
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

import structlog

from ach_agent.config.schema import AgentConfig, CodememMemory

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RolePaths:
    """Role-owned filesystem roots for split boot and startup transfer."""

    harness_state: Path
    engine_home: Path
    work_dir: Path
    transfer_root: Path


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def resolve_role_paths(cfg: AgentConfig, *, split_mode: bool = True) -> RolePaths:
    """Resolve the three operator data roots used by the split roles."""

    def trusted(path: str | Path) -> Path:
        # These roots come from operator configuration and are captured before E
        # starts.  Canonicalizing here makes later no-follow checks apply to the
        # actual trusted root even when a deployment uses a relative path or an
        # alias such as /tmp on a platform with a redirected temporary directory.
        return Path(path).expanduser().resolve(strict=False)

    if split_mode:
        mount = trusted(cfg.persistence.mount_path if cfg.persistence.enabled else "/tmp/ach-agent")
        harness_state = trusted(mount / "state")
        engine_home = trusted(cfg.engine.home or mount / "home")
        home_root = trusted(mount / "home")
        # Keep the historical standalone location inside HOME. Distributed roles
        # mount this same physical directory separately for Harness, while Engine
        # receives the parent HOME mount; native session directory identities stay
        # stable across a placement change.
        work_dir = trusted(cfg.engine.work_dir or engine_home / "workspace")
        workspace_root = trusted(mount / "home" / "workspace")
        if not _within(engine_home, home_root):
            raise ValueError(f"engine.home must be within {home_root} in distributed mode")
        if not _within(work_dir, workspace_root):
            raise ValueError(f"engine.workDir must be within {workspace_root} in distributed mode")
        transfer_root = trusted("/run/ach-agent/transfer")
    else:
        # Standalone retains its established defaults and explicit paths.
        if cfg.persistence.enabled:
            mount = trusted(cfg.persistence.mount_path)
            harness_state = trusted(mount / "state")
            engine_home = trusted(cfg.engine.home or mount / "home")
        else:
            harness_state = trusted("/tmp/ach-harness-state")
            engine_home = trusted(cfg.engine.home or "/tmp/ach-home")
        work_dir = trusted(cfg.engine.work_dir or engine_home / "workspace")
        transfer_root = trusted("/tmp/ach-agent-transfer")
    return RolePaths(
        harness_state=harness_state,
        engine_home=engine_home,
        work_dir=work_dir,
        transfer_root=transfer_root,
    )


def new_hydration_batch(transfer_root: Path) -> Path:
    """Create an empty startup hydration batch without changing path ownership."""
    transfer_root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=".ach-harness-shared-files-", dir=transfer_root))


def stage_legacy_codemem(
    cfg: AgentConfig, paths: RolePaths, batch: Path, *, split_mode: bool
) -> str:
    """Back up the historical H-state codemem DB into the one-shot batch."""
    memory = cfg.memory
    if not split_mode or not cfg.persistence.enabled or not isinstance(memory, CodememMemory):
        return ""
    params = memory.codemem
    target = (
        Path(params.db_path).expanduser().resolve()
        if params.db_path
        else paths.engine_home / "state" / "codemem.db"
    )
    if not _within(target, paths.engine_home):
        raise ValueError("memory.codemem.dbPath must be within engine.home in distributed mode")
    if params.db_path:
        return str(target)
    source = Path(cfg.persistence.mount_path).expanduser().resolve() / "state" / "codemem.db"
    if not source.exists() or source == target:
        return str(target)
    staged = batch / "codemem.db"
    staged.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as src:
            with sqlite3.connect(staged) as dst:
                src.backup(dst)
    except (OSError, sqlite3.Error) as exc:
        staged.unlink(missing_ok=True)
        raise ValueError(f"cannot preserve legacy codemem database {source}: {exc}") from exc
    return str(target)


def write_pid_file(pid_path: Path) -> None:
    """Write PID file for single-replica guard (Pitfall 11).

    Tolerate a non-writable path in dev by logging and continuing.
    """
    try:
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        log.info("PID file written", path=str(pid_path))
    except OSError as exc:
        log.warning(
            "PID file not writable — continuing without it (dev mode)",
            path=str(pid_path),
            error=str(exc),
        )


def resolve_engine_paths(cfg: AgentConfig) -> tuple[str, str]:
    """Resolve the opencode HOME and the agent workDir from the contract.

    Both are definable (engine.home / engine.workDir). When omitted:
      - home → <mountPath>/home if persistence.enabled (persistent), else /tmp/ach-home.
      - work_dir → <home>/workspace.
    Static state (config, skills, sessions) lives under HOME; HOME under mountPath persists.
    """
    home = cfg.engine.home
    if not home:
        home = f"{cfg.persistence.mount_path}/home" if cfg.persistence.enabled else "/tmp/ach-home"
    work_dir = cfg.engine.work_dir or f"{home}/workspace"
    return home, work_dir


def ach_state_dir(home: str) -> Path:
    """The single hydration state root: <home>/.ach-state (prompts + artifacts)."""
    return Path(home) / ".ach-state"


def link_ach_state(home: str, work_dir: str) -> Path:
    """Create <home>/.ach-state and, when workDir differs, a <workDir>/.ach-state symlink.

    The symlink gives the agent's shell (cwd = workDir) one stable path to hydrated
    artifacts; HOME stays the canonical read-only root. Best-effort: a symlink failure
    (e.g. unsupported FS) is non-fatal — the agent can still reach state under HOME.
    """
    state = ach_state_dir(home)
    state.mkdir(parents=True, exist_ok=True)
    if work_dir and Path(work_dir).resolve() != Path(home).resolve():
        link = Path(work_dir) / ".ach-state"
        link.parent.mkdir(parents=True, exist_ok=True)
        if not link.exists():
            try:
                link.symlink_to(state, target_is_directory=True)
            except OSError as e:
                log.warning("workDir .ach-state symlink failed (non-fatal)", error=str(e))
    return state


def harness_log_dir() -> Path:
    """Volatile dir for transient harness logs (e.g. the --tui attach log).

    Lives under /tmp, never the opencode HOME — harness logs are throwaway and must not
    pollute the persistent home/state tree.
    """
    d = Path("/tmp/ach-harness")
    d.mkdir(parents=True, exist_ok=True)
    return d
