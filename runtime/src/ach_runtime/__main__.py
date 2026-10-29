# SPDX-License-Identifier: MIT
"""Command line entry point for the mini execution process."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Sequence
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    execution = commands.add_parser("execution", help="run the mini execution server")
    execution.add_argument("--config", type=Path, default=None)
    execution.add_argument("--host", default="0.0.0.0")
    execution.add_argument("--port", type=int, default=8080)
    execution.add_argument("--workspace-dir", type=Path, default=Path("/workspace"))
    execution.add_argument("--sessions-root", type=Path, default=Path("/var/lib/ach-runtime/sessions"))
    control = commands.add_parser("control", help="run the Harness control process")
    control.add_argument("--config", type=Path, default=Path("/etc/ach-runtime/config.json"))
    agent = commands.add_parser("agent", help="run the whole agent in one process (not for production)")
    agent.add_argument("--config", type=Path, required=True)
    agent.add_argument("--host", default="0.0.0.0")
    agent.add_argument("--port", type=int, default=8080)
    agent.add_argument("--data-dir", type=Path, default=Path("/var/lib/ach-runtime"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "execution":
        from ach_runtime.execution.boot import run_execution

        asyncio.run(run_execution(args.config, host=args.host, port=args.port,
                                  workspace_dir=args.workspace_dir, sessions_root=args.sessions_root))
    elif args.command == "control":
        from ach_runtime.harness.boot import run_control

        asyncio.run(run_control(args.config))
    elif args.command == "agent":
        from ach_runtime.harness.agent import run_agent

        asyncio.run(run_agent(args.config, data_dir=args.data_dir, host=args.host, port=args.port))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
