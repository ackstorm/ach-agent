# SPDX-License-Identifier: Apache-2.0
"""Filesystem path helpers used at boot: PID file, engine home/workDir, harness logs."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import structlog

from ach_agent.config.schema import AgentConfig

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RolePaths:
    """Role-owned filesystem roots for split boot.

    ``public_context`` is the only hydration output visible to the engine. Harness
    state and engine home remain separate even when they share an operator mount.
    """

    harness_state: Path
    engine_home: Path
    work_dir: Path
    public_context: Path
    public_skills: Path


def resolve_role_paths(cfg: AgentConfig) -> RolePaths:
    """Resolve stable split-role paths without broad parent mounts."""

    def trusted(path: str | Path) -> Path:
        # These roots come from operator configuration and are captured before E
        # starts.  Canonicalizing here makes later no-follow checks apply to the
        # actual trusted root even when a deployment uses a relative path or an
        # alias such as /tmp on a platform with a redirected temporary directory.
        return Path(path).expanduser().resolve(strict=False)

    if cfg.persistence.enabled:
        mount = trusted(cfg.persistence.mount_path)
        harness_state = trusted(mount / "state")
        engine_home = trusted(cfg.engine.home or mount / "home")
        public_context = trusted(mount / "public-context")
    else:
        harness_state = trusted("/tmp/ach-harness-state")
        engine_home = trusted(cfg.engine.home or "/tmp/ach-home")
        public_context = trusted("/tmp/ach-public-context")
    work_dir = trusted(cfg.engine.work_dir or engine_home / "workspace")
    return RolePaths(
        harness_state=harness_state,
        engine_home=engine_home,
        work_dir=work_dir,
        public_context=public_context,
        public_skills=public_context / "skills",
    )


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

