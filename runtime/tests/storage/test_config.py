# SPDX-License-Identifier: MIT
"""`storage_from_env` factory (dispatch global-storage-task-1-brief.md §Step 1).

Loads its own fresh copy of tests/config/fixtures/pr-review-runtime.json — the sibling
tests/config/conftest.py fixture is not visible to this directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.public import ExecutionBootstrap, public_config
from ach_runtime.config.schema import RuntimeConfig, parse_config
from ach_runtime.storage.api import StorageUnavailable
from ach_runtime.storage.config import storage_from_env
from ach_runtime.storage.refs import StateRef
from ach_runtime.storage.s3 import S3Storage

_FIXTURE_PATH = Path(__file__).parent.parent / "config" / "fixtures" / "pr-review-runtime.json"
_WORKSPACE_REF = "a" * 64


@pytest.fixture
def pr_review_wire() -> dict[str, Any]:
    with _FIXTURE_PATH.open(encoding="utf-8") as f:
        return json.load(f)


def _config_with_flags(
    wire: dict[str, Any], *, workspace: bool, session: bool, artifacts: bool
) -> RuntimeConfig:
    wire["workspace"]["persistence"]["enabled"] = workspace
    wire["workspace"]["session"]["persistence"]["enabled"] = session
    wire["artifacts"]["enabled"] = artifacts
    wire["configVersion"] = compute_config_version(wire)
    return parse_config(wire)


def _ref(cfg: RuntimeConfig) -> StateRef:
    return StateRef(agent=cfg.agent, workspace_ref=_WORKSPACE_REF, domain="workspace")


class _ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _RecordingClient:
    """Records `get_object` kwargs, then reports the registry as never-persisted."""

    def __init__(self) -> None:
        self.read_kwargs: dict[str, str] | None = None

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.read_kwargs = {"Bucket": Bucket, "Key": Key}
        raise _ClientError("NoSuchKey")


def test_storage_from_env_disabled_skips_sdk(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=False, session=False, artifacts=False)
    client_mock = MagicMock()
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    assert storage_from_env(cfg, {}) is None
    client_mock.assert_not_called()

    configured_env = {
        "ACH_STORAGE_S3_BUCKET": "runtime-bucket",
        "ACH_STORAGE_S3_REGION": "us-east-1",
        "ACH_STORAGE_S3_ENDPOINT_URL": "http://seaweedfs:8333",
        "ACH_STORAGE_S3_PREFIX": "tenant/runtime",
    }
    assert storage_from_env(cfg, configured_env) is None
    client_mock.assert_not_called()


@pytest.mark.parametrize("domain", ["workspace", "session", "artifacts"])
@pytest.mark.parametrize("env", [{}, {"ACH_STORAGE_S3_BUCKET": ""}])
def test_storage_from_env_each_domain_requires_bucket(
    pr_review_wire: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    env: dict[str, str],
) -> None:
    flags = {"workspace": False, "session": False, "artifacts": False, domain: True}
    cfg = _config_with_flags(pr_review_wire, **flags)
    client_mock = MagicMock()
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    with pytest.raises(StorageUnavailable):
        storage_from_env(cfg, env)
    client_mock.assert_not_called()


async def test_storage_from_env_standard_s3(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=True, session=False, artifacts=False)
    recording_client = _RecordingClient()
    client_mock = MagicMock(return_value=recording_client)
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    storage = storage_from_env(cfg, {"ACH_STORAGE_S3_BUCKET": "runtime-bucket"})

    client_mock.assert_called_once_with("s3")
    assert isinstance(storage, S3Storage)
    assert storage._require_client() == (client_mock.return_value, "runtime-bucket")

    assert await storage.restore(_ref(cfg)) is None
    assert recording_client.read_kwargs == {
        "Bucket": "runtime-bucket",
        "Key": _ref(cfg).registry_key("ach"),
    }


async def test_storage_from_env_compatible_settings(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=True, session=False, artifacts=False)
    recording_client = _RecordingClient()
    client_mock = MagicMock(return_value=recording_client)
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    env = {
        "ACH_STORAGE_S3_BUCKET": "runtime-bucket",
        "ACH_STORAGE_S3_REGION": "us-east-1",
        "ACH_STORAGE_S3_ENDPOINT_URL": "http://seaweedfs:8333",
        "ACH_STORAGE_S3_PREFIX": "tenant/runtime",
    }
    storage = storage_from_env(cfg, env)

    client_mock.assert_called_once_with(
        "s3", region_name="us-east-1", endpoint_url="http://seaweedfs:8333"
    )
    assert isinstance(storage, S3Storage)

    assert await storage.restore(_ref(cfg)) is None
    assert recording_client.read_kwargs == {
        "Bucket": "runtime-bucket",
        "Key": _ref(cfg).registry_key("tenant/runtime"),
    }


def test_storage_from_env_sdk_failure_propagates(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=True, session=False, artifacts=False)
    sentinel = RuntimeError("sentinel-sdk-failure")
    client_mock = MagicMock(side_effect=sentinel)
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    with pytest.raises(RuntimeError, match="sentinel-sdk-failure"):
        storage_from_env(cfg, {"ACH_STORAGE_S3_BUCKET": "runtime-bucket"})


async def test_storage_from_env_operation_failure_propagates(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=True, session=False, artifacts=False)

    class _FailingClient:
        def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
            raise RuntimeError("sentinel-operation-failure")

    client_mock = MagicMock(return_value=_FailingClient())
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    storage = storage_from_env(cfg, {"ACH_STORAGE_S3_BUCKET": "runtime-bucket"})

    with pytest.raises(RuntimeError, match="sentinel-operation-failure"):
        await storage.restore(_ref(cfg))


def test_storage_from_env_preserves_config_projection(
    pr_review_wire: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = _config_with_flags(pr_review_wire, workspace=True, session=False, artifacts=False)
    client_mock = MagicMock()
    monkeypatch.setattr("ach_runtime.storage.config.boto3.client", client_mock)

    engine_env = dict(cfg.engine.env)
    bootstrap = ExecutionBootstrap(
        bootstrapVersion=cfg.schema_version,
        agent=cfg.agent,
        facadeEndpoint=cfg.infrastructure.facade_endpoint,
    )

    before_dump = cfg.model_dump(by_alias=True)
    before_version = cfg.config_version
    before_public = public_config(cfg, engine_env).model_dump(by_alias=True)
    before_bootstrap = bootstrap.model_dump(by_alias=True)

    sentinel_env = {
        "ACH_STORAGE_S3_BUCKET": "runtime-bucket",
        "ACH_STORAGE_S3_CREDENTIALS_SECRET": "sentinel-backend-name",
        "AWS_ACCESS_KEY_ID": "sentinel-access-key",
        "AWS_SECRET_ACCESS_KEY": "sentinel-secret-key",
    }
    storage_from_env(cfg, sentinel_env)

    after_dump = cfg.model_dump(by_alias=True)
    after_version = cfg.config_version
    after_public = public_config(cfg, engine_env).model_dump(by_alias=True)
    after_bootstrap = bootstrap.model_dump(by_alias=True)

    assert after_dump == before_dump
    assert after_version == before_version
    assert after_public == before_public
    assert after_bootstrap == before_bootstrap

    for document in (after_dump, after_public, after_bootstrap):
        serialized = json.dumps(document)
        assert "sentinel-backend-name" not in serialized
        assert "sentinel-access-key" not in serialized
        assert "sentinel-secret-key" not in serialized
