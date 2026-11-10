# SPDX-License-Identifier: Apache-2.0
"""Command line entry point for the mini execution process."""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ach-runtime")
    commands = parser.add_subparsers(dest="command", required=True)
    execution = commands.add_parser("execution", help="run the mini execution server")
    execution.add_argument("--config", type=Path, default=None)
    execution.add_argument("--host", default="0.0.0.0")
    execution.add_argument("--port", type=int, default=8080)
    execution.add_argument("--workspace-dir", type=Path, default=Path("/sandbox/ach/workspace"))
    execution.add_argument("--sessions-root", type=Path, default=Path("/sandbox/ach/sessions"))
    control = commands.add_parser("control", help="run the Harness control process")
    control.add_argument("--config", type=Path, default=Path("/etc/ach-runtime/config.json"))
    agent = commands.add_parser("agent", help="run the whole agent in one process (not for production)")
    agent.add_argument("--config", type=Path, required=True)
    agent.add_argument("--host", default="0.0.0.0")
    agent.add_argument("--port", type=int, default=8080)
    agent.add_argument("--data-dir", type=Path, default=Path("/var/lib/ach-runtime"))
    run = commands.add_parser("run", help="run a manifest-defined agent locally with the TUI attached")
    run.add_argument("--config", type=Path, required=True, help="the agent.yaml manifest")
    run.add_argument("--workspace", type=Path, default=Path.cwd(), help="the agent's working directory (default: cwd)")
    run.add_argument("--name", default=None, help="agent name (default: the manifest's directory name)")
    run.add_argument("--data-dir", type=Path, default=Path("/tmp/ach-run"))
    inspect = commands.add_parser(
        "inspect", help="print, one per line: the mode (ach|standalone), ach.profile (empty if unset), "
                        "then the env names the manifest's headers read")
    inspect.add_argument("--config", type=Path, required=True, help="the agent.yaml manifest")
    promote = commands.add_parser("promote", help="print the Agent CR for an agent.yaml and its deploy.yaml")
    promote.add_argument("--config", type=Path, required=True, help="the agent.yaml manifest")
    promote.add_argument("--deploy", type=Path, required=True, help="the deploy.yaml file")
    promote.add_argument("--name", default=None, help="agent name (default: the manifest's directory name)")
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
    elif args.command == "run":
        # Lazy: the execution image imports this module and has no PyYAML.
        from ach_runtime.config.schema import ConfigError
        from ach_runtime.harness.hydrate import HydrationError
        from ach_runtime.harness.run import RunError, run_local
        from ach_runtime.manifest import ManifestError

        log_path = args.data_dir / "run.log"
        try:
            return asyncio.run(run_local(
                args.config, workspace=args.workspace, name=args.name, data_dir=args.data_dir, log_path=log_path,
            ))
        except (ConfigError, HydrationError, ManifestError, RunError) as exc:
            # run_local has restored the terminal's stderr by now (its finally block). The log
            # lives in the container's /tmp and goes with it, so its tail is shown here.
            print(f"ach-agent: {exc}", file=sys.stderr)
            if log_path.is_file():
                print("ach-agent: last log lines:", file=sys.stderr)
                print(*log_path.read_text(errors="replace").splitlines()[-20:], sep="\n", file=sys.stderr)
            return 1
    elif args.command == "inspect":
        from ach_runtime.manifest import ManifestError, load_manifest, secret_env_names

        try:
            manifest = load_manifest(args.config)
        except ManifestError as exc:
            print(f"ach-agent: {exc}", file=sys.stderr)
            return 2
        print("standalone" if manifest.ach is None else "ach")
        print((manifest.ach and manifest.ach.profile) or "")
        for name in secret_env_names(manifest):
            print(name)
    elif args.command == "promote":
        from ach_runtime.manifest import ManifestError
        from ach_runtime.promote import PromoteError, promote

        try:
            sys.stdout.write(promote(args.config, args.deploy, name=args.name or args.config.resolve().parent.name))
        except (ManifestError, PromoteError) as exc:
            print(f"ach-agent: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
