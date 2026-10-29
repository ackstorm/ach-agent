# SPDX-License-Identifier: MIT
"""Environment-to-S3Storage factory assembling the operator-global backend (contract §7,
2026-10-01 global-storage-assembly-spec D1). Backend, bucket, region, endpoint and prefix are
operator settings delivered through the Harness's private pod env — never through
config.json/PublicConfig. This factory only builds an existing `S3Storage` from that env; it
is not startup wiring — later control assembly owns actually calling it.
"""

from __future__ import annotations

from collections.abc import Mapping

import boto3

from ach_runtime.config.schema import RuntimeConfig
from ach_runtime.storage.api import StorageUnavailable
from ach_runtime.storage.s3 import S3Storage

_DEFAULT_PREFIX = "ach"


def storage_from_env(config: RuntimeConfig, env: Mapping[str, str]) -> S3Storage | None:
    """Build the operator-configured S3Storage backend, or None when nothing needs it.

    Any of workspace persistence, session persistence or artifacts being enabled requires a
    nonempty `ACH_STORAGE_S3_BUCKET` — fails closed with the existing `StorageUnavailable`,
    never a volatile fallback. SDK/operation errors propagate as-is; no probe is performed
    during construction, and credentials are never passed explicitly — the ordinary boto3
    ambient chain applies.
    """
    required = (
        config.workspace.persistence.enabled
        or config.workspace.session.persistence.enabled
        or config.artifacts.enabled
    )
    if not required:
        return None

    bucket = env.get("ACH_STORAGE_S3_BUCKET")
    if not bucket:
        raise StorageUnavailable("ACH_STORAGE_S3_BUCKET is required when storage is enabled")

    client_kwargs: dict[str, str] = {}
    region = env.get("ACH_STORAGE_S3_REGION")
    if region:
        client_kwargs["region_name"] = region
    endpoint = env.get("ACH_STORAGE_S3_ENDPOINT_URL")
    if endpoint:
        client_kwargs["endpoint_url"] = endpoint

    client = boto3.client("s3", **client_kwargs)
    prefix = env.get("ACH_STORAGE_S3_PREFIX") or _DEFAULT_PREFIX
    return S3Storage(bucket=bucket, prefix=prefix, client=client)
