# SPDX-License-Identifier: Apache-2.0
"""Small role probe used by container startup/readiness/liveness checks."""

from __future__ import annotations

import argparse
import os
import sys

import httpx

from ach_agent.boot.ipc import channel_socket_path, engine_socket_path


def _request(role: str, check: str) -> bool:
    path = "/readyz" if check == "readiness" else "/healthz"
    try:
        if role == "engine":
            transport = httpx.HTTPTransport(uds=str(engine_socket_path()))
            with httpx.Client(
                transport=transport, base_url="http://ach-internal", timeout=2.0
            ) as client:
                response = client.get(path)
        elif role == "harness":
            transport = httpx.HTTPTransport(uds=str(channel_socket_path()))
            with httpx.Client(
                transport=transport, base_url="http://ach-internal", timeout=2.0
            ) as client:
                response = client.get(path)
        else:
            port = os.environ.get("ACH_CHANNELS_PORT", "8080")
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2.0) as client:
                response = client.get(path)
        return response.status_code == 200
    except (OSError, httpx.HTTPError):
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ach-agent-healthcheck")
    parser.add_argument("--role", choices=("channels", "harness", "engine"), required=True)
    parser.add_argument("--check", choices=("startup", "readiness", "liveness"), required=True)
    args = parser.parse_args(argv)
    return 0 if _request(args.role, args.check) else 1


if __name__ == "__main__":
    sys.exit(main())
