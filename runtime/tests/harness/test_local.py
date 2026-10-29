# SPDX-License-Identifier: MIT
import time
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.harness.local import LocalRoutingTransport, LocalSource
from ach_runtime.harness.refs import workspace_name
from ach_runtime.harness.tokens import engine_bearer, engine_verify_key
from ach_runtime.harness.workspaces import WorkspaceAssemblyUnavailable, WorkspaceConflict, WorkspaceRef

pytestmark = pytest.mark.asyncio

KEY = b"local-test-key"
UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
REF = WorkspaceRef("ach", UID, "a" * 64)
NAME = workspace_name("a", REF.workspace_ref)
BOOT = ExecutionBootstrap.model_validate_json(
    '{"bootstrapVersion":"workspace-v2","agent":{"name":"a","namespace":"ach","uid":"%s"},'
    '"facadeEndpoint":"http://127.0.0.1:8081/facades"}' % UID
)


@pytest.fixture
def parts(tmp_path: Path):
    transport = LocalRoutingTransport()
    source = LocalSource(BOOT, verify_key=engine_verify_key(KEY), data_dir=tmp_path, transport=transport)
    return source, transport, tmp_path


async def _health(transport: LocalRoutingTransport, base_url: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=transport) as client:
        return await client.get(base_url + "/healthz", headers={"Authorization": f"Bearer {engine_bearer(KEY, NAME)}"})


def _deadline() -> float:
    return time.monotonic() + 5


async def test_place_serves_a_real_execution_app_behind_a_local_host(parts) -> None:
    source, transport, root = parts
    placement = await source.place(REF, NAME, deadline=_deadline())
    assert (placement.name, placement.base_url, placement.registration) == (NAME, f"http://{NAME}.local", 1)
    response = await _health(transport, placement.base_url)
    assert response.status_code == 200 and response.headers["X-ACH-Instance-Id"]
    assert (root / "workspaces" / NAME / "workspace").is_dir() and (root / "workspaces" / NAME / "sessions").is_dir()
    assert source.observed(placement, "any") and source.serving(placement, "any")
    await source.aclose()


async def test_replacement_starts_empty_with_new_identity(parts) -> None:
    source, transport, root = parts
    first = await source.place(REF, NAME, deadline=_deadline())
    first_id = (await _health(transport, first.base_url)).headers["X-ACH-Instance-Id"]
    leftover = root / "workspaces" / NAME / "workspace" / "leftover.txt"
    leftover.write_text("stale")
    await source.retire(NAME)
    assert not (root / "workspaces" / NAME).exists()
    with pytest.raises(httpx.ConnectError):
        await _health(transport, first.base_url)
    second = await source.place(REF, NAME, deadline=_deadline())
    assert second.registration == 2 and not leftover.exists()
    assert (await _health(transport, second.base_url)).headers["X-ACH-Instance-Id"] != first_id
    await source.aclose()


async def test_place_twice_is_a_conflict(parts) -> None:
    source, _, _ = parts
    await source.place(REF, NAME, deadline=_deadline())
    with pytest.raises(WorkspaceConflict):
        await source.place(REF, NAME, deadline=_deadline())
    await source.aclose()


async def test_failed_place_leaves_no_directory_and_frees_the_slot(parts, monkeypatch) -> None:
    source, _, root = parts

    def boom(*_args, **_kwargs):
        raise RuntimeError("cannot build")

    monkeypatch.setattr("ach_runtime.harness.local.build_execution_app", boom)
    # WorkspaceAssemblyUnavailable is one of the errors on which the manager releases the slot.
    with pytest.raises(WorkspaceAssemblyUnavailable) as caught:
        await source.place(REF, NAME, deadline=_deadline())
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert not (root / "workspaces" / NAME).exists()
    monkeypatch.undo()
    await source.place(REF, NAME, deadline=_deadline())  # nothing left behind: the name is free
    await source.aclose()


async def test_adopt_wipes_local_state_and_returns_nothing(parts) -> None:
    source, _, root = parts
    stale = root / "workspaces" / "agent-a-stale" / "workspace"
    stale.mkdir(parents=True)
    (stale / "f").write_text("x")
    assert await source.adopt() == []
    assert not (root / "workspaces" / "agent-a-stale").exists()


async def test_aclose_retires_every_unit(parts) -> None:
    source, transport, _ = parts
    placement = await source.place(REF, NAME, deadline=_deadline())
    await source.aclose()
    with pytest.raises(httpx.ConnectError):
        await _health(transport, placement.base_url)
