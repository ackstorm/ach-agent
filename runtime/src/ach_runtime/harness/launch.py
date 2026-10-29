# SPDX-License-Identifier: MIT
"""Launch environment of one execution unit (contract v2 5.5), shared by both Workspace sources."""

from __future__ import annotations

import json

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable, WorkspaceRef

ENV_VALUE_MAX = 8 * 1024  # contract 5.5: single-line, at most 8 KiB each


def bootstrap_json(bootstrap: ExecutionBootstrap) -> str:
    return json.dumps(bootstrap.model_dump(by_alias=True, mode="json"), separators=(",", ":"), sort_keys=True)


def launch_env(bootstrap_json: str, verify_key: str, ref: WorkspaceRef, name: str) -> dict[str, str]:
    env = {
        "ACH_SANDBOX_VERIFY_KEY": verify_key,
        "ACH_WORKSPACE_NAME": name,
        "ACH_WORKSPACE_REF": ref.workspace_ref,
        "ACH_BOOTSTRAP_JSON": bootstrap_json,
    }
    for key, value in env.items():
        if "\n" in value or len(value.encode()) > ENV_VALUE_MAX:
            raise WorkspaceAssemblyUnavailable(f"launch env {key} must be single-line and under 8 KiB")
    return env
