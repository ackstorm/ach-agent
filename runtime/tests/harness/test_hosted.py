# SPDX-License-Identifier: MIT
"""harness/hosted.py -- HostedSource state machine and the /host/v1 routes (contract v2 5.3)."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

import httpx
import pytest

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.harness import hosted
from ach_runtime.harness.hosted import HostedSource, create_host_app
from ach_runtime.harness.refs import workspace_name
from ach_runtime.harness.tokens import engine_bearer, facade_token, host_token
from ach_runtime.harness.workspaces import (
    Placement,
    WorkspaceAssemblyUnavailable,
    WorkspaceConflict,
    WorkspaceRef,
    WorkspaceUnhosted,
)

UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"
KEY = b"sandbox-key-SECRET-0123456789abcdef"
S3_SECRET = "ek_s3-secret-value"
TOKEN = host_token(KEY, UID)
REF_HEX = "a" * 64
OTHER_HEX = "b" * 64
URL = "http://unit-1:8080"
URL2 = "http://unit-2:8080"
AUTH = {"authorization": f"Bearer {TOKEN}"}


def _bootstrap() -> ExecutionBootstrap:
    return ExecutionBootstrap.model_validate({
        "bootstrapVersion": "workspace-v2",
        "agent": {"name": "gitlab-reviewer", "namespace": "ach", "uid": UID},
        "facadeEndpoint": "http://control.ach.svc:8081/facades",
    })


def _ref(hex_: str = REF_HEX) -> WorkspaceRef:
    return WorkspaceRef("ach", UID, hex_)


def _name(hex_: str = REF_HEX) -> str:
    return workspace_name("gitlab-reviewer", hex_)


def _deadline(seconds: float = 5.0) -> float:
    return time.monotonic() + seconds


@pytest.fixture
def source() -> HostedSource:
    return HostedSource(_bootstrap(), verify_key="pubkey-xyz")


@pytest.fixture
async def client(source: HostedSource):
    app = create_host_app(source, TOKEN)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def _placed(source: HostedSource, client: httpx.AsyncClient, url: str = URL,
                  hex_: str = REF_HEX) -> Placement:
    if source.adopting:
        await _adopted(source)
    task = asyncio.create_task(source.place(_ref(hex_), _name(hex_), deadline=_deadline()))
    await _settle()
    r = await client.put(f"/workspaces/{_name(hex_)}/instance", json={"baseUrl": url}, headers=AUTH)
    assert r.status_code == 202
    return await asyncio.wait_for(task, 2)


def _listed(source: HostedSource) -> list[dict[str, Any]]:
    return source.snapshot()["workspaces"]


async def _adopted(source: HostedSource) -> None:
    assert source.accept_inventory(source.epoch, []) == 202


# 1 ---------------------------------------------------------------------------------------------
async def test_place_publishes_running_and_put_wakes_it(source, client) -> None:
    await _adopted(source)
    task = asyncio.create_task(source.place(_ref(), _name(), deadline=_deadline()))
    await _settle()
    [entry] = _listed(source)
    assert entry["desired"] == "running" and entry["instance"] is None and entry["name"] == _name()
    r = await client.put(f"/workspaces/{_name()}/instance", json={"baseUrl": URL}, headers=AUTH)
    assert r.status_code == 202
    placement = await asyncio.wait_for(task, 2)
    assert placement.name == _name() and placement.base_url == URL and placement.registration >= 1
    assert _listed(source)[0]["instance"] == {"instanceId": None, "baseUrl": URL, "state": "probing"}


async def test_place_twice_conflicts(source, client) -> None:
    await _placed(source, client)
    with pytest.raises(WorkspaceConflict):
        await source.place(_ref(), _name(), deadline=_deadline())


# 2 ---------------------------------------------------------------------------------------------
async def test_place_deadline_unhosted(source, client, monkeypatch) -> None:
    alarms: list[tuple[Any, ...]] = []
    monkeypatch.setattr(hosted, "alarm", lambda *a, **k: alarms.append((a, k)))
    revision = source.revision
    with pytest.raises(WorkspaceUnhosted):
        await source.place(_ref(), _name(), deadline=_deadline(0.05))
    assert _listed(source) == []
    assert source.revision == revision + 2  # published, then removed
    assert alarms == [(("workspace_unhosted",), {"workspace_ref": REF_HEX})]
    r = await client.put(f"/workspaces/{_name()}/instance", json={"baseUrl": URL}, headers=AUTH)
    assert r.status_code == 409 and r.json() == {"error": "not_desired"}


async def test_place_cancelled_leaves_no_entry(source) -> None:
    task = asyncio.create_task(source.place(_ref(), _name(), deadline=_deadline()))
    await _settle()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert _listed(source) == []


# 3 ---------------------------------------------------------------------------------------------
async def test_put_same_url_idempotent_different_url_conflicts(source, client) -> None:
    p = await _placed(source, client)
    path = f"/workspaces/{_name()}/instance"
    assert (await client.put(path, json={"baseUrl": URL}, headers=AUTH)).status_code == 200  # probing
    assert source.observed(p, "i-1")
    assert source.serving(p, "i-1")
    assert (await client.put(path, json={"baseUrl": URL}, headers=AUTH)).status_code == 200  # serving
    r = await client.put(path, json={"baseUrl": URL2}, headers=AUTH)
    assert r.status_code == 409 and r.json() == {"error": "registered"}
    assert _listed(source)[0]["instance"] == {"instanceId": "i-1", "baseUrl": URL, "state": "serving"}


async def test_put_validation(source, client) -> None:
    await _placed(source, client)
    path = f"/workspaces/{_name()}/instance"
    for payload in ({}, {"baseUrl": 3}, {"baseUrl": "ftp://x"}, {"baseUrl": "not a url"}, [1]):
        assert (await client.put(path, json=payload, headers=AUTH)).status_code == 422


# 4 ---------------------------------------------------------------------------------------------
async def test_observed_and_delete_precondition(source, client) -> None:
    p = await _placed(source, client)
    path = f"/workspaces/{_name()}/instance"
    assert source.observed(p, "i-1") is True
    assert source.observed(Placement(p.name, p.base_url, p.registration + 99), "i-x") is False
    for query in ("", "?instanceId=other", "?instanceId=i-from-previous-epoch"):
        assert (await client.delete(path + query, headers=AUTH)).status_code == 204
        assert len(_listed(source)) == 1
    assert (await client.delete(path + "?instanceId=i-1", headers=AUTH)).status_code == 204
    assert _listed(source) == []


async def test_observed_false_for_replaced_registration(source, client) -> None:
    p = await _placed(source, client)
    assert (await client.delete(f"/workspaces/{_name()}/instance", headers=AUTH)).status_code == 204
    assert source.observed(p, "i-1") is False


async def test_delete_before_observed_removes_entry(source, client) -> None:
    await _placed(source, client)
    assert (await client.delete(f"/workspaces/{_name()}/instance", headers=AUTH)).status_code == 204
    assert _listed(source) == []


# 5 ---------------------------------------------------------------------------------------------
async def test_serving_replaced_registration_is_false_and_inert(source, client) -> None:
    p = await _placed(source, client)
    stale = Placement(p.name, p.base_url, p.registration + 1)
    revision = source.revision
    assert source.serving(stale, "i-1") is False
    assert source.revision == revision
    assert _listed(source)[0]["instance"]["state"] == "probing"
    assert source.serving(p, "ignored-id") is True
    assert _listed(source)[0]["instance"]["state"] == "serving"


# 6 ---------------------------------------------------------------------------------------------
async def test_retire_lists_released_and_waits_for_delete(source, client) -> None:
    lost: list[Any] = []

    async def on_lost(name: str, instance: str | None) -> None:
        lost.append((name, instance))

    source.on_lost(on_lost)
    p = await _placed(source, client)
    source.observed(p, "i-1")
    retiring = asyncio.create_task(source.retire(_name()))
    await _settle()
    assert not retiring.done()
    assert _listed(source)[0]["desired"] == "released"
    assert _listed(source)[0]["instance"]["baseUrl"] == URL
    await client.delete(f"/workspaces/{_name()}/instance?instanceId=i-1", headers=AUTH)
    await asyncio.wait_for(retiring, 2)
    await _settle()
    assert lost == [] and _listed(source) == []


async def test_retire_absent_returns_at_once(source) -> None:
    await asyncio.wait_for(source.retire("nothing"), 1)


# 7 ---------------------------------------------------------------------------------------------
async def test_delete_running_calls_on_lost_once(source, client) -> None:
    lost: list[Any] = []

    async def on_lost(name: str, instance: str | None) -> None:
        lost.append((name, instance))

    source.on_lost(on_lost)
    p = await _placed(source, client)
    source.observed(p, "i-1")
    path = f"/workspaces/{_name()}/instance?instanceId=i-1"
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    assert (await client.delete(path, headers=AUTH)).status_code == 204
    await _settle()
    assert lost == [(_name(), "i-1")]


async def test_delete_noops(source, client) -> None:
    lost: list[Any] = []

    async def on_lost(name: str, instance: str | None) -> None:
        lost.append(name)

    source.on_lost(on_lost)
    await _adopted(source)
    assert (await client.delete("/workspaces/unknown/instance", headers=AUTH)).status_code == 204
    task = asyncio.create_task(source.place(_ref(), _name(), deadline=_deadline(0.3)))
    await _settle()
    assert (await client.delete(f"/workspaces/{_name()}/instance", headers=AUTH)).status_code == 204
    assert len(_listed(source)) == 1  # running, no registration: untouched
    with pytest.raises(WorkspaceUnhosted):
        await task
    assert lost == []


# 8 ---------------------------------------------------------------------------------------------
async def test_long_poll(source, client) -> None:
    await _adopted(source)
    cursor = source.cursor
    t0 = time.monotonic()
    assert (await client.get("/workspaces", headers=AUTH)).status_code == 200
    assert (await client.get("/workspaces?after=other.1&waitSeconds=5", headers=AUTH)).status_code == 200
    assert (await client.get("/workspaces?waitSeconds=5", headers=AUTH)).status_code == 200
    assert time.monotonic() - t0 < 1  # other epoch and missing `after` answer immediately

    waiting = asyncio.create_task(client.get(f"/workspaces?after={cursor}&waitSeconds=5", headers=AUTH))
    await _settle()
    assert not waiting.done()
    placing = asyncio.create_task(source.place(_ref(), _name(), deadline=_deadline(0.3)))
    r = await asyncio.wait_for(waiting, 2)
    assert r.json()["cursor"] != cursor and len(r.json()["workspaces"]) == 1
    with pytest.raises(WorkspaceUnhosted):
        await placing

    t0 = time.monotonic()
    r = await client.get(f"/workspaces?after={source.cursor}&waitSeconds=1", headers=AUTH)
    assert r.status_code == 200 and 0.8 < time.monotonic() - t0 < 3


async def test_wait_seconds_clamped(source, client, monkeypatch) -> None:
    seen: list[int] = []

    async def fake_listing(after, wait):
        seen.append(wait)
        return source.snapshot()

    monkeypatch.setattr(source, "listing", fake_listing)
    await client.get("/workspaces?waitSeconds=999", headers=AUTH)
    await client.get("/workspaces?waitSeconds=-4", headers=AUTH)
    assert seen == [60, 0]
    assert (await client.get("/workspaces?waitSeconds=abc", headers=AUTH)).status_code == 422


async def test_close_wakes_pending_long_poll_with_503(source, client) -> None:
    waiting = asyncio.create_task(client.get(f"/workspaces?after={source.cursor}&waitSeconds=30", headers=AUTH))
    await _settle()
    source.close()
    assert (await asyncio.wait_for(waiting, 2)).status_code == 503
    t0 = time.monotonic()
    assert (await client.get(f"/workspaces?after={source.cursor}&waitSeconds=30", headers=AUTH)).status_code == 503
    assert time.monotonic() - t0 < 1


# 9 ---------------------------------------------------------------------------------------------
def _unit(hex_: str = REF_HEX, url: str = URL, name: str | None = None) -> dict[str, str]:
    return {"name": name or _name(hex_), "workspaceRef": hex_, "baseUrl": url}


async def test_inventory(source, client) -> None:
    assert source.snapshot()["adopting"] is True and source.snapshot()["workspaces"] == []
    adopting = asyncio.create_task(source.adopt())
    await _settle()
    assert not adopting.done()

    bad_epoch = await client.put("/inventory", json={"runtimeEpoch": "nope", "units": []}, headers=AUTH)
    assert bad_epoch.status_code == 409
    wrong_name = await client.put("/inventory", json={
        "runtimeEpoch": source.epoch, "units": [_unit(OTHER_HEX, name=_name())]}, headers=AUTH)
    assert wrong_name.status_code == 422 and source.adopting is True
    dup = await client.put("/inventory", json={
        "runtimeEpoch": source.epoch, "units": [_unit(), _unit()]}, headers=AUTH)
    assert dup.status_code == 422 and source.adopting is True
    bad_ref = await client.put("/inventory", json={
        "runtimeEpoch": source.epoch, "units": [_unit("zz", name="x")]}, headers=AUTH)
    assert bad_ref.status_code == 422 and source.adopting is True

    ok = await client.put("/inventory", json={
        "runtimeEpoch": source.epoch, "units": [_unit(), _unit(OTHER_HEX, URL2)]}, headers=AUTH)
    assert ok.status_code == 202 and source.adopting is False
    adopted = await asyncio.wait_for(adopting, 2)
    assert {(r.workspace_ref, p.base_url) for r, p in adopted} == {(REF_HEX, URL), (OTHER_HEX, URL2)}
    listed = _listed(source)
    assert {e["name"] for e in listed} == {_name(), _name(OTHER_HEX)}
    assert all(e["desired"] == "running" and e["instance"]["state"] == "probing" for e in listed)
    assert all(e["instance"]["instanceId"] is None for e in listed)

    again = await client.put("/inventory", json={"runtimeEpoch": source.epoch, "units": []}, headers=AUTH)
    assert again.status_code == 200 and len(_listed(source)) == 2


async def test_inventory_body_validation(source, client) -> None:
    for payload in ({}, {"runtimeEpoch": source.epoch}, {"runtimeEpoch": source.epoch, "units": [1]},
                    {"runtimeEpoch": source.epoch, "units": [{"name": "x"}]},
                    {"runtimeEpoch": 5, "units": []}):
        assert (await client.put("/inventory", json=payload, headers=AUTH)).status_code == 422


# 10 --------------------------------------------------------------------------------------------
async def test_body_limits_and_malformed(source, client) -> None:
    path = f"/workspaces/{_name()}/instance"
    big = b'{"baseUrl": "http://x", "pad": "' + b"a" * (1024 * 1024) + b'"}'
    assert (await client.put(path, content=big, headers=AUTH)).status_code == 422
    assert (await client.put(path, content=b"{not json", headers=AUTH)).status_code == 422
    assert (await client.put("/inventory", content=b"[]", headers=AUTH)).status_code == 422


async def test_503_on_every_route_after_close(source, client) -> None:
    source.close()
    path = f"/workspaces/{_name()}/instance"
    assert (await client.get("/workspaces", headers=AUTH)).status_code == 503
    assert (await client.put(path, json={"baseUrl": URL}, headers=AUTH)).status_code == 503
    assert (await client.delete(path, headers=AUTH)).status_code == 503
    assert (await client.put("/inventory", json={"runtimeEpoch": source.epoch, "units": []},
                             headers=AUTH)).status_code == 503


async def test_401_on_bad_credentials(source, client) -> None:
    path = f"/workspaces/{_name()}/instance"
    for headers in ({}, {"authorization": "Bearer wrong"}, {"authorization": TOKEN}):
        assert (await client.get("/workspaces", headers=headers)).status_code == 401
        assert (await client.put(path, json={"baseUrl": URL}, headers=headers)).status_code == 401
        assert (await client.delete(path, headers=headers)).status_code == 401
        assert (await client.put("/inventory", json={}, headers=headers)).status_code == 401
    source.close()  # 401 takes precedence over 503
    assert (await client.get("/workspaces", headers={})).status_code == 401


# 11 --------------------------------------------------------------------------------------------
async def test_other_credentials_rejected_on_host_routes(source, client) -> None:
    for bearer in (facade_token(KEY, _name()), engine_bearer(KEY, _name())):
        r = await client.get("/workspaces", headers={"authorization": f"Bearer {bearer}"})
        assert r.status_code == 401
    assert host_token(KEY, UID) != facade_token(KEY, UID)


async def test_host_token_is_refused_by_facade_authentication() -> None:
    """The reverse half: every facade routes through `authenticate_facade`, which a host token
    fails even when the host owns every Workspace (the HTTP view is in test_boot)."""
    from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, authenticate_facade

    async def owns_everything(_claim: str) -> bool:
        return True

    for credential in (host_token(KEY, UID), host_token(KEY, _name())):
        with pytest.raises(WorkspaceAuthenticationFailed):
            await authenticate_facade(KEY, credential, owned_workspace=owns_everything)


# 12 --------------------------------------------------------------------------------------------
async def test_launch_spec(source, client) -> None:
    await _adopted(source)
    task = asyncio.create_task(source.place(_ref(), _name(), deadline=_deadline(0.2)))
    await _settle()
    [entry] = _listed(source)
    launch = entry["launch"]
    dumped = json.dumps(launch)
    for secret in (KEY.decode(), S3_SECRET, "ek_", "seed"):
        assert secret not in dumped
    assert launch["port"] == 8080 and launch["command"] == ["python", "-m", "ach_runtime", "execution"]
    env = launch["env"]
    assert set(env) == {"ACH_SANDBOX_VERIFY_KEY", "ACH_WORKSPACE_NAME", "ACH_WORKSPACE_REF", "ACH_BOOTSTRAP_JSON"}
    assert env["ACH_SANDBOX_VERIFY_KEY"] == "pubkey-xyz"
    assert env["ACH_WORKSPACE_NAME"] == _name() and env["ACH_WORKSPACE_REF"] == REF_HEX
    assert all("\n" not in v and len(v.encode()) <= 8 * 1024 for v in env.values())
    assert ExecutionBootstrap.model_validate_json(env["ACH_BOOTSTRAP_JSON"]) == _bootstrap()
    with pytest.raises(WorkspaceUnhosted):
        await task


async def test_oversized_launch_value_fails_before_publishing(source) -> None:
    source._verify_key = "k" * (8 * 1024 + 1)
    revision = source.revision
    with pytest.raises(WorkspaceAssemblyUnavailable):
        await source.place(_ref(), _name(), deadline=_deadline())
    assert _listed(source) == [] and source.revision == revision
    source._verify_key = "multi\nline"
    with pytest.raises(WorkspaceAssemblyUnavailable):
        await source.place(_ref(), _name(), deadline=_deadline())
    assert source.revision == revision
