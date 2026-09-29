# SPDX-License-Identifier: Apache-2.0
"""Minimal in-cluster client for agent-sandbox SandboxClaims — the harness's registry.

A live sandbox IS a SandboxClaim labelled with the agent. RBAC (rendered by ach) limits the
harness SA to sandboxclaims in its namespace. No kubernetes client dependency: a handful of
REST calls over httpx.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import os
from pathlib import Path
from typing import Any

import httpx

# ── agent-sandbox API surface ────────────────────────────────────────────────────────────
# Read from kubernetes-sigs/agent-sandbox v1.0.4 extensions/api/v1beta1/sandboxclaim_types.go
# (2026-09-29), NOT from a live cluster. VERIFY after agent-sandbox is installed:
#   kubectl explain sandboxclaim.spec --api-version=extensions.agents.x-k8s.io/v1beta1 --recursive
#   kubectl explain sandboxclaim.status --api-version=extensions.agents.x-k8s.io/v1beta1 --recursive
# Any correction belongs in THIS block only.
GROUP_VERSION = "extensions.agents.x-k8s.io/v1beta1"
PLURAL = "sandboxclaims"


def _rfc3339(when: dt.datetime) -> str:
    return when.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def claim_body(
    name: str, labels: dict[str, str], warm_pool: str, shutdown_at: dt.datetime
) -> dict[str, Any]:
    # No spec.env: it forces a cold start and would put values in an API object (P9).
    return {
        "apiVersion": GROUP_VERSION,
        "kind": "SandboxClaim",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "warmPoolRef": {"name": warm_pool},
            "lifecycle": {"shutdownPolicy": "Delete", "shutdownTime": _rfc3339(shutdown_at)},
        },
    }


def shutdown_patch(shutdown_at: dt.datetime) -> dict[str, Any]:
    return {"spec": {"lifecycle": {"shutdownTime": _rfc3339(shutdown_at)}}}


def ready_ip(claim: dict[str, Any]) -> str | None:
    status = claim.get("status", {})
    ready = any(
        c.get("type") == "Ready" and c.get("status") == "True" for c in status.get("conditions", [])
    )
    ips = status.get("sandbox", {}).get("podIPs") or []  # []string in v1.0.4
    return str(ips[0]) if ready and ips else None


# ── end of agent-sandbox API surface ─────────────────────────────────────────────────────

_SA = Path("/var/run/secrets/kubernetes.io/serviceaccount")
AGENT_LABEL = "ach.ackstorm.ai/agent"
SESSION_LABEL = "ach.ackstorm.ai/session"
PERSISTENT_LABEL = "ach.ackstorm.ai/persistent"


class ClaimClient:
    def __init__(
        self,
        namespace: str,
        warm_pool: str,
        *,
        base_url: str | None = None,
        token_path: Path = _SA / "token",
        ca_path: Path = _SA / "ca.crt",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        host = os.environ.get("KUBERNETES_SERVICE_HOST", "kubernetes.default.svc")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        self._pool = warm_pool
        self._token_path = token_path
        self._base = f"/apis/{GROUP_VERSION}/namespaces/{namespace}/{PLURAL}"
        verify: Any = str(ca_path) if transport is None and ca_path.exists() else True
        self._http = httpx.AsyncClient(
            base_url=base_url or f"https://{host}:{port}",
            verify=verify,
            transport=transport,
            timeout=30.0,
        )

    def _headers(self, **extra: str) -> dict[str, str]:
        # Read per call: projected SA tokens rotate.
        return {"Authorization": f"Bearer {self._token_path.read_text().strip()}", **extra}

    async def create(self, name: str, labels: dict[str, str], shutdown_at: dt.datetime) -> None:
        body = claim_body(name, labels, self._pool, shutdown_at)
        (await self._http.post(self._base, json=body, headers=self._headers())).raise_for_status()

    async def get(self, name: str) -> dict[str, Any] | None:
        resp = await self._http.get(f"{self._base}/{name}", headers=self._headers())
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return dict(resp.json())

    async def wait_ready(self, name: str, *, timeout: float, interval: float = 0.5) -> str:
        """Bounded wait for Ready; return the sandbox pod IP."""
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            claim = await self.get(name)
            ip = ready_ip(claim) if claim is not None else None
            if ip is not None:
                return ip
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"sandbox claim {name} not Ready within {timeout}s")
            await asyncio.sleep(interval)

    async def wait_deleted(self, name: str, *, timeout: float, interval: float = 0.5) -> None:
        deadline = asyncio.get_running_loop().time() + timeout
        while await self.get(name) is not None:
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"sandbox claim {name} still present after {timeout}s")
            await asyncio.sleep(interval)

    async def touch(self, name: str, *, shutdown_at: dt.datetime) -> None:
        resp = await self._http.patch(
            f"{self._base}/{name}",
            json=shutdown_patch(shutdown_at),
            headers=self._headers(**{"Content-Type": "application/merge-patch+json"}),
        )
        resp.raise_for_status()

    async def list_agent(self, agent_label: str) -> list[dict[str, Any]]:
        resp = await self._http.get(
            self._base,
            params={"labelSelector": f"{AGENT_LABEL}={agent_label}"},
            headers=self._headers(),
        )
        resp.raise_for_status()
        return list(resp.json().get("items", []))

    async def delete(self, name: str) -> None:
        resp = await self._http.delete(f"{self._base}/{name}", headers=self._headers())
        if resp.status_code != 404:
            resp.raise_for_status()

    async def close(self) -> None:
        await self._http.aclose()
