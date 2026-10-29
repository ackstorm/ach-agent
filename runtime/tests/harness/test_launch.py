# SPDX-License-Identifier: MIT
import pytest

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.harness.launch import bootstrap_json, launch_env
from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable, WorkspaceRef

UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
REF = WorkspaceRef("ach", UID, "a" * 64)
BOOT = ExecutionBootstrap.model_validate_json(
    '{"bootstrapVersion":"workspace-v2","agent":{"name":"a","namespace":"ach","uid":"%s"},'
    '"facadeEndpoint":"http://127.0.0.1:8081/facades"}' % UID
)


def test_launch_env_keys_and_round_trip() -> None:
    env = launch_env(bootstrap_json(BOOT), "verify", REF, "agent-a-xyz")
    assert set(env) == {"ACH_SANDBOX_VERIFY_KEY", "ACH_WORKSPACE_NAME", "ACH_WORKSPACE_REF", "ACH_BOOTSTRAP_JSON"}
    assert env["ACH_WORKSPACE_REF"] == REF.workspace_ref
    assert "\n" not in env["ACH_BOOTSTRAP_JSON"]
    assert ExecutionBootstrap.model_validate_json(env["ACH_BOOTSTRAP_JSON"]) == BOOT


@pytest.mark.parametrize("verify_key", ["x" * (8 * 1024 + 1), "two\nlines"])
def test_launch_env_rejects_oversized_or_multiline(verify_key: str) -> None:
    with pytest.raises(WorkspaceAssemblyUnavailable):
        launch_env(bootstrap_json(BOOT), verify_key, REF, "agent-a-xyz")
