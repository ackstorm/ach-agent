# SPDX-License-Identifier: MIT
"""Real Harness component assembly for the configured mini-harness path."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from pathlib import Path
import re
from contextlib import AsyncExitStack
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from collections.abc import Callable, Mapping
from typing import Any

import httpx
from fastapi import FastAPI
from starlette.routing import Mount

from ach_runtime import __version__
from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.config.public import ExecutionBootstrap, PublicConfig, public_config
from ach_runtime.config.schema import RuntimeConfig, SecretAlias
from ach_runtime.execution.wire import CancelRequest, RunStatus, StartRequest
from ach_runtime.facades._memory_client import (
    TOOLS_SPEC,
    excluded_mcp_server_ids,
    prepare_ach_memory,
    resolve_ach_memory_auth,
    resolve_endpoint,
    resolve_project,
)
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.a2a import A2AEgressFacade
from ach_runtime.facades.mcp import McpFacade
from ach_runtime.facades.memory import AchMemoryFacade
from ach_runtime.facades.artifacts import ArtifactFacade, ArtifactOperations
from ach_runtime.facades.model import ModelFacade, public_catalog
from ach_runtime.harness.admission import (
    Admission,
    AdmissionTicket,
    RecoveredRun,
    ScriptAdmissionTicket,
    WorkspaceOwnerLost,
)
from ach_runtime.harness.delivery import Delivery, StartOutcomeUnknown
from ach_runtime.observability.runs import register_secrets
from ach_runtime.harness.hydrate import (
    HydratedCatalog,
    HydrationInput,
    Hydrator,
    stage_context,
    standalone_catalog,
)
from ach_runtime.harness.hosted import HostedSource
from ach_runtime.harness.lifecycle import Lifecycle
from ach_runtime.harness.handoff import run_handoff
from ach_runtime.harness.scripts import run_webhook_script
from ach_runtime.harness.transfer import put_archive
from ach_runtime.storage.archive import _run_owned, pack
from ach_runtime.harness.tokens import engine_bearer, engine_verify_key, facade_token, load_harness_key
from ach_runtime.harness.tokens import WorkspaceAuthenticationFailed, authenticate_facade
from ach_runtime.harness.workspaces import (
    WorkspaceAssemblyUnavailable,
    WorkspaceEndpoint,
    WorkspaceManager,
    WorkspaceSource,
    instance_changed,
)
from ach_runtime.storage.config import storage_from_env
from ach_runtime.storage.api import ArtifactProducer
from ach_runtime.storage.refs import StateRef

log = logging.getLogger(__name__)
_TEMPLATE = re.compile(r'\{\{\s*([A-Za-z0-9_.\-]+)(?:\s*\|\s*(?:default\(\s*"([^"]*)"\s*\)|(json)))?\s*\}\}')
_MISSING = object()
# A Workspace object name (`refs.workspace_name`) is long enough to look like a token,
# so it is matched first, as group 1, and kept verbatim.
_SECRET = re.compile(
    r"(?i)(\bagent-[a-z0-9-]+-[0-9a-f]{20}(?![A-Za-z0-9_\-]))"
    r"|(?:://[^/\s@]*@|bearer\s+\S+|authorization\S*\s*[:=]\s*\S+"
    r"|\b\w*(?:token|secret|password|passwd|api[_-]?key|key)\w*\s*[:=]\s*\S+|\b[A-Za-z0-9_\-]{32,}\b"
    r"|\b(?:ek|sk)[-_][A-Za-z0-9_\-]+)"
)


def _failure_reason(exc: BaseException, limit: int = 300) -> str:
    """'<ExcType>: <message>' with credential-looking text redacted and length capped."""
    text = _SECRET.sub(lambda m: m.group(1) or "[redacted]", f"{type(exc).__name__}: {exc}")
    return text if len(text) <= limit else text[:limit] + "..."


def _clock() -> datetime:
    return datetime.now(timezone.utc)


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _alarm(
    reason: str, ticket: AdmissionTicket | ScriptAdmissionTicket | RecoveredRun | None = None
) -> None:
    from ach_runtime.observability import alarm

    known_reasons = {
        "admission queue capacity rejected": "limit_reached",
        "deadline exceeded while queued": "limit_reached",
        "deadline exceeded before permit wait": "limit_reached",
        "deadline exceeded while waiting for script permits": "limit_reached",
        "deadline exceeded immediately before script spawn": "limit_reached",
        "deadline exceeded while waiting for permits": "limit_reached",
        "deadline exceeded immediately before delivery": "limit_reached",
        "invocation deadline exceeded": "limit_reached",
        "deadline cancellation was not confirmed": "limit_reached",
        "recovered Started outcome is unknown": "start_outcome_unknown",
        "run start failed": "start_failed",
        "webhook script runner failed": "start_failed",
        "Workspace owner is not published; run ownership remains held": "run_outcome_unknown",
        "Workspace owner was lost; run outcome is unknown": "run_outcome_unknown",
        "run observation is unavailable; ownership remains held": "run_outcome_unknown",
        "gateway_unavailable": "gateway_unavailable",
        "Harness lifecycle tick failed": "lifecycle_tick_failed",
        "Harness Workspace persistence failed": "persistence_failed",
        "Harness session persistence failed": "persistence_failed",
        "Harness suspend hook failed": "suspend_hook_failed",
    }
    if reason.startswith("recovered run observation failed:"):
        kind = "run_outcome_unknown"
    else:
        kind = known_reasons.get(reason, "run_outcome_unknown")
    alarm(
        kind,
        invocation_id=getattr(ticket, "invocation_id", None),
        workspace_ref=getattr(ticket, "workspace_ref", None),
        session_ref=getattr(ticket, "session_ref", None),
    )


def _delivery_alarm(reason: str, ticket: AdmissionTicket) -> None:
    from ach_runtime.observability import alarm

    alarm("start_outcome_unknown", invocation_id=ticket.invocation_id,
          workspace_ref=ticket.workspace_ref, session_ref=ticket.session_ref)


def _render_prompt(template: str, context: dict[str, Any]) -> str:
    """Legacy deterministic {{ }} substitution: plain, default, json; no Jinja."""
    cursor = 0
    parts: list[str] = []
    for match in _TEMPLATE.finditer(template):
        literal = template[cursor:match.start()]
        if "{{" in literal or "}}" in literal:
            raise WorkspaceAssemblyUnavailable("unsupported prompt template syntax")
        parts.append(literal)
        path, default, as_json = match.groups()
        raw: Any = context
        for segment in path.split("."):
            if isinstance(raw, dict) and segment in raw:
                raw = raw[segment]
            elif isinstance(raw, list) and segment.isdigit() and int(segment) < len(raw):
                raw = raw[int(segment)]
            else:
                raw = _MISSING
                break
        if raw is _MISSING:
            parts.append(default or "" if default is not None else "")
        elif as_json:
            parts.append(json.dumps(raw, ensure_ascii=False, sort_keys=True, default=str))
        elif isinstance(raw, (str, int, float, bool)):
            parts.append(str(raw))
        elif default is not None:
            parts.append(default)
        else:
            parts.append("")
        cursor = match.end()
    suffix = template[cursor:]
    if "{{" in suffix or "}}" in suffix:
        raise WorkspaceAssemblyUnavailable("unsupported prompt template syntax")
    parts.append(suffix)
    return "".join(parts)


@dataclass(slots=True)
class Control:
    config: RuntimeConfig
    key: bytes
    verify_key: str
    catalog: HydratedCatalog
    public: PublicConfig
    storage: Any
    source: WorkspaceSource
    manager: WorkspaceManager
    delivery: Delivery
    admission: Admission
    model_facade: ModelFacade
    mcp_facade: McpFacade
    memory_facade: AchMemoryFacade | None
    a2a_facade: A2AEgressFacade | None
    artifact_facade: ArtifactFacade | None
    mini_client: httpx.AsyncClient
    facade_transport: httpx.AsyncBaseTransport | None = None
    facade_clients: list[httpx.AsyncClient] = field(default_factory=list)
    _prepared: dict[str, tuple[WorkspaceEndpoint, Any]] = field(default_factory=dict)
    _started_run: dict[str, str] = field(default_factory=dict)
    _stopping: bool = False
    lifecycle: Lifecycle | None = None
    _private_env: dict[str, str] = field(default_factory=dict, repr=False)
    _ek: str = field(default="", repr=False)
    # Resolved upstream header values (log redaction) and per-upstream headers for the facades.
    upstream_secrets: tuple[str, ...] = field(default=(), repr=False)
    # False when the catalog carries no per-token cost: run cost is then reported unpriced.
    _priced: bool = True
    _model_headers: dict[str, str] = field(default_factory=dict, repr=False)
    _mcp_headers: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    # I7: resolved memory endpoint/project/private headers/identity, populated by assemble
    # only -- never PublicConfig/StartRequest/catalog. `_memory_configured` distinguishes
    # "no memory block at all" (skip prepare entirely) from "configured but unresolved"
    # (still call prepare_ach_memory, which owns its own degraded text).
    _memory_configured: bool = field(default=False, repr=False)
    _memory_endpoint: str = field(default="", repr=False)
    _memory_project: str = field(default="", repr=False)
    _memory_headers: dict[str, str] = field(default_factory=dict, repr=False)
    _memory_identity: Any = field(default=None, repr=False)
    # Workspaces running a workspace hook: not yet (start) or no longer (stop) published in
    # manager._active, but their hook may call the facades (`ach-mcp`).
    _hook_endpoints: dict[str, WorkspaceEndpoint] = field(default_factory=dict, repr=False)

    async def _owned_live(self, claim: str) -> bool:
        # Kept async for `authenticate_facade`; nothing here awaits (spec 6.3: no I/O).
        for endpoint in (*self.manager.active_endpoints().values(), *self._hook_endpoints.values()):
            if endpoint.name == claim and not self.manager.is_lost(endpoint.instance_id):
                return True
        return False

    async def artifact_operations(self, token: str, invocation_id: str) -> ArtifactOperations:
        """Resolve artifact ownership only from this live Workspace and run records."""
        claim = await authenticate_facade(self.key, token, owned_workspace=self._owned_live)
        endpoint = next(
            (item for item in self.manager.active_endpoints().values() if item.name == claim), None
        )
        if endpoint is None:
            raise WorkspaceAuthenticationFailed(claim)

        prepared = self._prepared.get(invocation_id)
        if prepared is not None:
            prepared_endpoint, request = prepared
            if (
                request.invocation_id != invocation_id
                or request.workspace_ref != endpoint.ref.workspace_ref
                or prepared_endpoint.ref != endpoint.ref
                or prepared_endpoint.name != endpoint.name
                or prepared_endpoint.instance_id != endpoint.instance_id
                or request.deadline_at <= _clock()
            ):
                raise WorkspaceAuthenticationFailed(claim)
            session_ref = request.session_ref
        else:
            recovered = self.admission.recovered_run(invocation_id)
            if recovered is None or (
                recovered.invocation_id != invocation_id
                or recovered.workspace_ref != endpoint.ref.workspace_ref
                or recovered.instance_id != endpoint.instance_id
                or recovered.deadline_at <= _clock()
            ):
                raise WorkspaceAuthenticationFailed(claim)
            session_ref = recovered.session_ref

        owner = StateRef(
            agent=self.config.agent,
            workspace_ref=endpoint.ref.workspace_ref,
            domain="workspace",
        )
        return ArtifactOperations(
            storage=self.storage,
            owner=owner,
            producer=ArtifactProducer(session_ref=session_ref, invocation_id=invocation_id),
            policy=self.config.artifacts,
            workspace_name=claim,
            key=self.key,
            owned_workspace=self._owned_live,
        )

    def _catalog_for(self, endpoint: WorkspaceEndpoint) -> dict[str, Any]:
        facade = self.public.infrastructure.facade_endpoint.rstrip("/")
        token = facade_token(self.key, endpoint.name)
        excluded = set(self.config.capability.filter.exclude.mcp_servers)
        if self.config.memory is not None:
            params = self.config.memory.ach_memory
            servers = {item.id: item.endpoint for item in self.catalog.mcp_servers}
            excluded.update(excluded_mcp_server_ids(
                mcp_server_id=params.mcp_server_id, endpoint=params.endpoint, mcp_servers=servers
            ))
        memory_url: str | None = None
        if self.memory_facade is not None:
            memory_url = f"{facade}/memory/t/{token}/memory/"
        artifact_url: str | None = None
        if self.artifact_facade is not None:
            artifact_url = f"{facade}/artifacts/t/{token}"
        mcp_urls = {
            server.id: f"{facade}/mcp/t/{token}/mcp/{server.id}"
            for server in self.catalog.mcp_servers
            if server.id not in excluded
        }
        if self.a2a_facade is not None:
            mcp_urls["a2a"] = f"{facade}/a2a/t/{token}/a2a/"
        result = public_catalog(
            self.catalog,
            provider_id=self.config.model.type,
            model_url=f"{facade}/model/t/{token}",
            mcp_urls=mcp_urls,
            memory_url=memory_url,
            artifact_url=artifact_url,
        )
        if self._memory_configured:
            # I7: the active memory tools instructions; engine/config.py composes it once
            # with the resolved persona.
            result["memoryToolsSpec"] = TOOLS_SPEC
        return result

    def _headers(self, endpoint: WorkspaceEndpoint) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {engine_bearer(self.key, endpoint.name)}",
            "X-ACH-Instance-Id": endpoint.instance_id,
        }

    async def _mini_json(self, endpoint: WorkspaceEndpoint, method: str, path: str,
                         payload: dict[str, Any] | None = None, *,
                         timeout: float = 10) -> tuple[int, Any]:
        try:
            async with asyncio.timeout(timeout):
                response = await self.mini_client.request(
                    method, endpoint.base_url.rstrip("/") + path,
                    headers=self._headers(endpoint), json=payload, timeout=timeout,
                )
        except (httpx.HTTPError, TimeoutError) as exc:
            raise WorkspaceAssemblyUnavailable("mini-harness connection failed") from exc
        if response.headers.get("X-ACH-Instance-Id") != endpoint.instance_id:
            if instance_changed(response, endpoint):
                self.manager.report_lost(endpoint)
            raise WorkspaceAssemblyUnavailable("mini-harness instance ID did not match observation")
        try:
            body = response.json()
        except ValueError as exc:
            raise WorkspaceAssemblyUnavailable("mini-harness returned invalid JSON") from exc
        return response.status_code, body

    _HEALTH_POLL_INTERVAL_SECONDS = 0.1
    _WATCH_FAST_SECONDS = 5.0
    _HEALTH_RETRYABLE_STATUSES = frozenset({408, 429, 500, 502, 503, 504})

    async def wait_mini_health(self, base_url: str, name: str, deadline: float) -> str:
        """Poll `/healthz` until the unit answers; return the instance ID it reports.

        The ID comes from the process, never from the host (contract 5.6). Only a connection
        failure or a retryable status (408/429/500/502/503/504) is retried; anything else
        fails at once. A unit of another release is a probe failure."""
        headers = {"Authorization": f"Bearer {engine_bearer(self.key, name)}"}
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkspaceAssemblyUnavailable(f"mini-harness health check timed out for {name}")
            try:
                async with asyncio.timeout(min(10.0, remaining)):
                    response = await self.mini_client.get(base_url.rstrip("/") + "/healthz", headers=headers)
            except (httpx.TransportError, TimeoutError):
                response = None
            if response is None or response.status_code in self._HEALTH_RETRYABLE_STATUSES:
                await asyncio.sleep(min(self._HEALTH_POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
                continue
            instance_id = response.headers.get("X-ACH-Instance-Id", "")
            if response.status_code != 200 or not instance_id:
                raise WorkspaceAssemblyUnavailable(f"mini-harness health check rejected: {response.status_code}")
            release = _json_or_empty(response).get("release")
            if release != __version__:
                from ach_runtime.observability import alarm

                alarm("pod_unhealthy")  # the exception text below carries both releases
                raise WorkspaceAssemblyUnavailable(
                    f"execution unit runs release {release!r}, control runs {__version__!r}")
            return instance_id

    async def run_workspace_hook(self, endpoint: WorkspaceEndpoint, phase: str, *,
                                 restored: bool = False) -> None:
        """Run workspaceStart (`phase="start"`) or workspaceStop (`"stop"`) in the pod.
        A fresh pod has no config yet, so start configures it first (idempotent)."""
        hooks = self.config.hooks
        hook = hooks.workspace_start if phase == "start" else hooks.workspace_stop
        if hook is None:
            return
        key = endpoint.ref.workspace_ref
        self._hook_endpoints[key] = endpoint
        try:
            if phase == "start":
                await self.configure_workspace(endpoint)
            status, body = await self._mini_json(
                endpoint, "POST", f"/workspace/hooks/{phase}",
                {"restored": restored} if phase == "start" else None,
                timeout=hook.timeout_seconds + 10,
            )
        finally:
            self._hook_endpoints.pop(key, None)
        if status != 200:
            message = body.get("message") if isinstance(body, dict) else None
            raise WorkspaceAssemblyUnavailable(
                f"workspace{phase.capitalize()} hook failed: {message or status}"
            )

    async def configure_workspace(self, endpoint: WorkspaceEndpoint) -> None:
        status, body = await self._mini_json(
            endpoint, "POST", "/configure",
            {"public": _public_wire(self.public),
             "catalog": self._catalog_for(endpoint)},
        )
        if status != 200 or body != {"configVersion": self.public.config_version}:
            raise WorkspaceAssemblyUnavailable("mini-harness configure did not confirm configVersion")
        status, _ = await self._mini_json(endpoint, "GET", "/readyz")
        if status != 200:
            raise WorkspaceAssemblyUnavailable("mini-harness is not ready after configure")

    async def reconnect_workspace(self, endpoint: WorkspaceEndpoint) -> None:
        if self.lifecycle is not None and self.config.workspace.persistence.enabled:
            writer = await self.storage.current_writer(self.lifecycle.workspace_state_ref(endpoint))
            if writer is not None and writer != endpoint.instance_id:
                # The pod was recreated outside the Harness (e.g. node loss while control was
                # down): its /workspace is empty. Restore exactly as for a fresh activation.
                await self.lifecycle.restore_workspace(endpoint)
        await self.configure_workspace(endpoint)
        try:
            records = await self.delivery.list_runs(endpoint)
        except Exception as exc:
            raise WorkspaceAssemblyUnavailable("mini-harness returned invalid run inventory") from exc
        identities: set[str] = set()
        for record in records:
            if record.workspace_ref != endpoint.ref.workspace_ref:
                raise WorkspaceAssemblyUnavailable("mini-harness run inventory identity did not match its owner")
            if record.invocation_id in identities:
                raise WorkspaceAssemblyUnavailable("mini-harness run inventory contained duplicate identities")
            identities.add(record.invocation_id)
        for record in records:
            if record.state in {"completed", "failed", "cancelled", "skipped"}:
                continue
            try:
                self.admission.recover(
                    invocation_id=record.invocation_id,
                    channel=record.channel,
                    workspace_ref=record.workspace_ref,
                    session_ref=record.session_ref,
                    config_version=record.config_version,
                    deadline_at=record.deadline_at,
                    instance_id=endpoint.instance_id,
                    run_id=record.run_id,
                    observer=lambda record=record: self._observe_run(endpoint, record),
                )
            except (ValueError, RuntimeError) as exc:
                raise WorkspaceAssemblyUnavailable("mini-harness run inventory ownership conflicted") from exc

    async def prepare(self, ticket: AdmissionTicket, endpoint: WorkspaceEndpoint):
        if self.lifecycle is None:
            return await self._prepare(ticket, endpoint)
        async with self.lifecycle.operation(endpoint.ref.workspace_ref):
            return await self._prepare(ticket, endpoint)

    async def _prepare(self, ticket: AdmissionTicket, endpoint: WorkspaceEndpoint):
        from ach_runtime.execution.wire import StartRequest

        if self.lifecycle is not None:
            await self.lifecycle.restore_session(ticket, endpoint)
        await self.configure_workspace(endpoint)
        routed = ticket.routed_event
        channel = next((c for c in self.config.channels if c.name == routed.channel), None)
        if channel is None:
            raise WorkspaceAssemblyUnavailable("admitted channel is no longer configured")
        event = routed.event
        if any(item.download_url for item in (*self.catalog.skills, *self.catalog.prompts, *self.catalog.artifacts)):
            await self._transfer_context(ticket, endpoint)
        if channel.handoff is not None:
            await self._run_handoff(channel.handoff, event, ticket, endpoint)
        context: dict[str, Any] = {
            "payload": event.payload,
            "event": {"id": event.event_id, "channel": event.channel, "type": event.type, **event.context},
            "internal": {
                "channel": {"name": channel.name, "type": channel.type, "source": channel.source or ""},
                "agent": {"name": self.config.agent.name},
                "event": {"id": event.event_id},
                "session": {"key": routed.session_key},
                "memory": {"bank": ""},
            },
        }
        if channel.prompt:
            prompt = _render_prompt(channel.prompt, context)
        elif event.payload.get("text"):
            prompt = str(event.payload["text"])
        else:
            prompt = json.dumps(event.payload, ensure_ascii=False, sort_keys=True)
        memory_available: bool | None = None
        if self._memory_configured:
            # I7: the one `prepare_ach_memory` call per non-script invocation, appended
            # after the rendered user prompt and before the A2A output directive.
            # `load_context` IS the reachability test -- no separate health probe.
            memory_available, memory_section = await prepare_ach_memory(
                self._memory_endpoint, self._memory_project, self._memory_headers,
                identity=self._memory_identity,
            )
            prompt = f"{prompt}\n\n{memory_section}"
        if channel.type == "a2a":
            from ach_runtime.channels.a2a import A2A_OUTPUT_INSTRUCTIONS

            prompt = f"{prompt}\n\n{A2A_OUTPUT_INSTRUCTIONS}"
        request = StartRequest(
            invocationId=ticket.invocation_id, channel=event.channel,
            workspaceRef=ticket.workspace_ref, sessionRef=ticket.session_ref,
            workspaceKey=routed.workspace_key, sessionKey=routed.session_key,
            configVersion=self.public.config_version, prompt=prompt,
            deadlineAt=ticket.deadline_at, memoryAvailable=memory_available,
        )
        self._prepared[ticket.invocation_id] = (endpoint, request)
        return request

    async def _transfer_context(self, ticket: AdmissionTicket, endpoint: WorkspaceEndpoint) -> None:
        # assemble() resolves credentials from its private environment; never put the
        # value in the archive or request body.
        ek = getattr(self, "_ek", "")
        with tempfile.TemporaryDirectory(prefix="ach-context-") as temporary:
            root = Path(temporary) / "context"
            root.mkdir()
            async with httpx.AsyncClient(timeout=30) as client:
                await stage_context(
                    self.catalog, root, excluded_skills=self.config.capability.filter.exclude.skills,
                    client=client, ek=ek,
                )
            archive = Path(temporary) / "context.tar.gz"
            _, cancelled, error = await _run_owned(pack, root, archive, max_bytes=512 * 1024**2)
            if cancelled:
                raise asyncio.CancelledError()
            if error:
                raise error
            async def chunks():
                with archive.open("rb") as source:
                    while block := source.read(64 * 1024):
                        yield block
            result = await put_archive(
                self.mini_client, endpoint, self._headers(endpoint),
                "/execution/v1/sandbox/archive/hydration", chunks(),
                params={"session_ref": ticket.session_ref}, on_mismatch=self.manager.report_lost,
            )
            if result.get("status") != "imported" or result.get("sessionRef") != ticket.session_ref:
                raise WorkspaceAssemblyUnavailable("mini-harness did not confirm context hydration")

    async def _run_handoff(self, cfg: Any, event: Any, ticket: AdmissionTicket,
                           endpoint: WorkspaceEndpoint) -> None:
        with tempfile.TemporaryDirectory(prefix="ach-handoff-") as temporary:
            root = Path(temporary) / "workspace"
            root.mkdir()
            environ = dict(os.environ)
            environ.update(getattr(self, "_private_env", {}))
            await run_handoff(cfg, event, ticket, root, environ=environ)
            archive = Path(temporary) / "handoff.tar.gz"
            _, cancelled, error = await _run_owned(pack, root, archive, max_bytes=512 * 1024**2)
            if cancelled:
                raise asyncio.CancelledError()
            if error:
                raise error
            async def chunks():
                with archive.open("rb") as source:
                    while block := source.read(64 * 1024):
                        yield block
            result = await put_archive(
                self.mini_client, endpoint, self._headers(endpoint),
                "/execution/v1/workspace/handoff", chunks(),
                params={"destination": cfg.destination}, on_mismatch=self.manager.report_lost,
            )
            if result.get("status") != "imported":
                raise WorkspaceAssemblyUnavailable("mini-harness did not confirm handoff import")

    async def run(
        self,
        event: RoutedEvent | Event,
        ticket: AdmissionTicket | ScriptAdmissionTicket,
    ) -> RunStatus | None:
        if isinstance(ticket, ScriptAdmissionTicket):
            if not isinstance(event, Event):
                raise WorkspaceAssemblyUnavailable("script admission did not retain a normalized Event")
            configured = next(
                (item for item in self.config.channels if item.name == event.channel), None
            )
            if configured is None or configured.type != "webhook-script" or configured.script is None:
                raise WorkspaceAssemblyUnavailable("script channel configuration is unavailable")
            environ = dict(os.environ)
            environ.update(self._private_env)
            await run_webhook_script(
                configured.script,
                event,
                ticket.deadline_at,
                environ=environ,
                on_started=ticket.mark_started,
            )
            return None
        if not isinstance(event, RoutedEvent):
            raise WorkspaceAssemblyUnavailable("agent admission did not retain a routed Event")
        from ach_runtime.execution.wire import StartRequest

        started = None
        try:
            started = await self.delivery.start(ticket)
        except StartOutcomeUnknown:
            # Delivery has exhausted its same-pod cached-start recovery. Keep the ticket
            # and permits while observing the pinned pod; never replay a possibly-sent
            # prompt or release uncertain active work.
            pass  # Delivery's fixed anomaly adapter counted this ambiguous Start once.
        except Exception as exc:
            if isinstance(exc, TimeoutError) and ticket.deadline_at <= _clock():
                _alarm("invocation deadline exceeded", ticket)
            else:
                _alarm("run start failed", ticket)
                reason = _failure_reason(exc)
                # The control process prints only the message text, so the reason goes in it.
                log.error(
                    "run start failed: %s", reason,
                    extra={"invocation_id": ticket.invocation_id, "reason": reason},
                )
            self._prepared.pop(ticket.invocation_id, None)
            raise
        prepared = self._prepared.get(ticket.invocation_id)
        if prepared is None:
            raise WorkspaceAssemblyUnavailable(
                "Delivery completed preparation without retaining its request"
            )
        endpoint, request = prepared
        if started is not None:
            ticket.mark_started()
            self._started_run[ticket.invocation_id] = started.run_id
        if not isinstance(request, StartRequest):
            raise WorkspaceAssemblyUnavailable("Delivery retained an invalid StartRequest")
        return await self._observe_run(
            endpoint, request, started_run_id=started.run_id if started else None,
            event=event.event,
        )

    async def _observe_run(self, endpoint: WorkspaceEndpoint, expected: StartRequest | RunStatus,
                           *, started_run_id: str | None = None, event: Event | None = None) -> RunStatus:
        from ach_runtime.harness.delivery import DeliveryError

        cancel_sent = False
        warned = False
        warned_owner = False
        # Poll every 0.25 s for the first seconds of a run, then back off towards 1 s; an
        # observed state change resets to 0.25 s, and a sleep never crosses the deadline.
        fast_until = _clock() + timedelta(seconds=self._WATCH_FAST_SECONDS)
        delay = 0.25
        last_state: str | None = None

        def check_published_owner() -> bool:
            nonlocal warned_owner
            current = self.manager.active_endpoint(endpoint.ref.workspace_ref)
            if self.manager.is_lost(endpoint.instance_id) or (
                current is not None and current.instance_id != endpoint.instance_id
            ):
                self._prepared.pop(expected.invocation_id, None)
                self._started_run.pop(expected.invocation_id, None)
                _alarm("Workspace owner was lost; run outcome is unknown", expected)
                self._record_run(expected, None, started_run_id, event, outcome="unknown")
                raise WorkspaceOwnerLost("Workspace owner lost; run outcome is unknown")
            if current is None:
                if not warned_owner:
                    _alarm("Workspace owner is not published; run ownership remains held", expected)
                    warned_owner = True
                return False
            return True

        while True:
            if _clock() >= expected.deadline_at and not cancel_sent:
                cancel_sent = True
                _alarm("invocation deadline exceeded", expected)
                try:
                    await self.delivery.cancel(endpoint, CancelRequest(
                        invocationId=expected.invocation_id, reason="invocation deadline exceeded"))
                except Exception:
                    pass
            if not check_published_owner():
                await asyncio.sleep(0.25)
                continue
            try:
                status = await asyncio.wait_for(
                    self.delivery.status(endpoint, expected.invocation_id), timeout=5
                )
                self.delivery._validate_status(status, expected)
                if started_run_id is not None and status.run_id != started_run_id:
                    raise DeliveryError("RunStatus runId did not match Started")
                if status.state in {"completed", "failed", "cancelled", "skipped"}:
                    # Delivery verified the response's instance header, so a terminal status
                    # is trustworthy even if the instance was lost in the meantime (T09).
                    self._prepared.pop(expected.invocation_id, None)
                    self._started_run.pop(expected.invocation_id, None)
                    self._record_run(expected, status, started_run_id, event)
                    return status
                if not check_published_owner():
                    await asyncio.sleep(0.25)
                    continue
                if status.state == "unknown":
                    raise DeliveryError("mini-harness reported unknown run state")
                if status.state != last_state:
                    last_state = status.state
                    delay = 0.25
                elif _clock() >= fast_until:
                    delay = min(delay * 2, 1.0)
            except WorkspaceOwnerLost:
                raise
            except Exception:
                if not warned:
                    _alarm("run observation is unavailable; ownership remains held", expected)
                    warned = True
            pause = delay
            if not cancel_sent:
                pause = max(0.0, min(delay, (expected.deadline_at - _clock()).total_seconds()))
            await asyncio.sleep(pause)

    def _record_run(
        self,
        expected: StartRequest | RunStatus,
        status: RunStatus | None,
        started_run_id: str | None,
        event: Event | None,
        *,
        outcome: str | None = None,
    ) -> None:
        from ach_runtime.observability import RunRecord, RunUsage, record_run

        result = status.result if status is not None and isinstance(status.result, dict) else None
        reporting = result.get("reporting") if isinstance(result, dict) else None
        duration = reporting.get("duration_ms") if isinstance(reporting, dict) else None
        if not isinstance(duration, (int, float)) or isinstance(duration, bool) or duration < 0:
            duration = None
        usage = None
        raw_usage = reporting.get("usage") if isinstance(reporting, dict) else None
        if isinstance(raw_usage, dict) and raw_usage.get("source") == "opencode" and raw_usage.get("basis") == "engine_estimate":
            try:
                usage = RunUsage(
                    source="opencode", basis="engine_estimate", input=raw_usage.get("input"),
                    output=raw_usage.get("output"), reasoning=raw_usage.get("reasoning"),
                    cache_read=raw_usage.get("cache_read"), cache_write=raw_usage.get("cache_write"),
                    cost_usd=raw_usage.get("cost_usd"), steps=raw_usage.get("steps"),
                )
            except (TypeError, ValueError):
                usage = None
        if usage is not None and not getattr(self, "_priced", True):
            usage = replace(usage, cost_usd=None)
        record_run(RunRecord(
            agent=self.config.agent.name,
            environment=self.config.identity_environment,
            invocation_id=expected.invocation_id,
            run_id=(status.run_id if status is not None else started_run_id or getattr(expected, "run_id", None)),
            actor=event.actor if event is not None else None,
            event_id=event.event_id if event is not None else None,
            channel=expected.channel,
            workspace_ref=expected.workspace_ref,
            session_ref=expected.session_ref,
            config_version=expected.config_version,
            model=f"{self.public.model.type}/{self.public.model.name}",
            duration_ms=duration,
            outcome=outcome or (status.state if status is not None else "unknown"),
            usage=usage,
        ))

    async def close(self) -> None:
        self._stopping = True
        self.source.close()
        self.manager.cancel_adoption()
        errors: list[BaseException] = []
        closers = [self.admission.close]
        if self.lifecycle is not None:
            closers.append(self.lifecycle.close)
        if self.a2a_facade is not None:
            closers.append(self.a2a_facade.aclose)
        closers.extend((self.delivery.close, self.mini_client.aclose))
        for closer in closers:
            try:
                await closer()
            except BaseException as exc:
                errors.append(exc)
        for client in self.facade_clients:
            try:
                await client.aclose()
            except BaseException as exc:
                errors.append(exc)
        if errors:
            raise errors[0]


def create_facade_app(control: Control) -> FastAPI:
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette

    apps = [control.model_facade.app(), control.mcp_facade.app()]
    memory_app = None
    artifact_app = None
    a2a_app = None
    if control.memory_facade is not None:
        memory_app = control.memory_facade.app()
        apps.append(memory_app)
    if control.artifact_facade is not None:
        artifact_app = control.artifact_facade.app()
        apps.append(artifact_app)
    if control.a2a_facade is not None:
        a2a_app = control.a2a_facade.app()
        apps.append(a2a_app)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with AsyncExitStack() as stack:
            for child in apps:
                await stack.enter_async_context(child.router.lifespan_context(child))
            yield

    app = FastAPI(lifespan=lifespan)
    app.router.routes.clear()
    app.router.routes.extend([
        Mount("/facades/model", app=apps[0]),
        Mount("/facades/mcp", app=apps[1]),
    ])
    if memory_app is not None:
        app.router.routes.append(Mount("/facades/memory", app=memory_app))
    if artifact_app is not None:
        app.router.routes.append(Mount("/facades/artifacts", app=artifact_app))
    if a2a_app is not None:
        app.router.routes.append(Mount("/facades/a2a", app=a2a_app))
    return app


def resolve_headers(headers: Mapping[str, SecretAlias], env: Mapping[str, str]) -> dict[str, str]:
    """Resolve configured upstream headers from env; a missing value fails assembly."""
    resolved: dict[str, str] = {}
    for name, alias in headers.items():
        value = env.get(alias.env, "")
        if not value:
            raise WorkspaceAssemblyUnavailable(f"upstream header {name} needs env {alias.env}, which is unset")
        resolved[name] = value
    return resolved


async def assemble(
    config: RuntimeConfig,
    *,
    environ: dict[str, str] | None = None,
    ach_transport: httpx.AsyncBaseTransport | None = None,
    mini_transport: httpx.AsyncBaseTransport | None = None,
    facade_transport: httpx.AsyncBaseTransport | None = None,
    source_factory: Callable[[ExecutionBootstrap, str], WorkspaceSource] | None = None,
) -> Control:
    facade_endpoint = config.infrastructure.facade_endpoint
    if facade_endpoint is None:
        raise WorkspaceAssemblyUnavailable("infrastructure.facadeEndpoint is required for the control role")
    env = dict(os.environ if environ is None else environ)
    raw_key = load_harness_key().decode("utf-8") if environ is None else env.get("ACH_SANDBOX_KEY")
    if not raw_key:
        raise RuntimeError("ACH_SANDBOX_KEY is required and must not be empty")
    key = raw_key.encode("utf-8")
    verify_key = engine_verify_key(key)
    if config.capability.ach is not None:
        ach = config.capability.ach
        ek = env.get(ach.key.env, "")
        if not ek:
            raise WorkspaceAssemblyUnavailable("ACH identity credential is not configured")
        ach_client = httpx.AsyncClient(transport=ach_transport, timeout=30)
        hydrator = Hydrator(resolve_ek=lambda name: env.get(name, ""), client=ach_client)
        try:
            catalog = await hydrator.load(HydrationInput.from_config(config))
        finally:
            await ach_client.aclose()
        prefix = {"openai": "v1", "gemini": "gemini/v1beta", "anthropic": "anthropic"}[config.model.type]
        model_endpoint = f"{ach.base_url.rstrip('/')}/{prefix}"
        model_headers = {"x-ach-key": ek}
        mcp_headers = {item.id: {"x-ach-key": ek} for item in catalog.mcp_servers}
        upstream_secrets = (ek,)
    else:
        server = config.capability.standalone
        ek = ""
        catalog = standalone_catalog(config)
        model_endpoint = server.model.base_url
        model_headers = resolve_headers(server.model.headers, env)
        mcp_headers = {item.name: resolve_headers(item.headers, env) for item in server.mcp_servers}
        upstream_secrets = tuple(model_headers.values()) + tuple(
            value for headers in mcp_headers.values() for value in headers.values()
        )
    identity = ProcessIdentity(agent=config.agent.name, environment=config.identity_environment)
    public = public_config(config, config.engine.env)
    storage = storage_from_env(config, env)
    mcp_servers = {item.id: item.endpoint for item in catalog.mcp_servers}
    excluded = frozenset(config.capability.filter.exclude.mcp_servers)
    memory: AchMemoryFacade | None = None
    memory_excluded: set[str] = set()
    memory_configured = config.memory is not None
    memory_endpoint = ""
    memory_project = ""
    memory_headers: dict[str, str] = {}
    if config.memory is not None:
        params = config.memory.ach_memory
        memory_endpoint = resolve_endpoint(endpoint=params.endpoint, mcp_server_id=params.mcp_server_id,
                                           mcp_servers=mcp_servers)
        memory_project = resolve_project(params, config.agent.name, namespace=config.agent.namespace)
        ok, headers = resolve_ach_memory_auth(params.auth, ek,
                                              resolve_secret=lambda alias: env.get(alias))
        if ok:
            memory_headers = headers
        memory_excluded = excluded_mcp_server_ids(
            mcp_server_id=params.mcp_server_id, endpoint=params.endpoint, mcp_servers=mcp_servers)
        if ok and memory_endpoint:
            memory = AchMemoryFacade(
                endpoint=memory_endpoint, headers=headers, project=memory_project,
                key=key, owned_workspace=lambda claim: control._owned_live(claim), identity=identity,
                exclude_tools=frozenset(config.capability.filter.exclude.tools),
            )
    # Facades close over Control for the owned-live check, so construct the bundle first.
    mini_client = httpx.AsyncClient(transport=mini_transport, timeout=10)
    bootstrap = ExecutionBootstrap(bootstrapVersion="workspace-v3", agent=config.agent, facadeEndpoint=facade_endpoint)
    source = source_factory(bootstrap, verify_key) if source_factory else HostedSource(bootstrap, verify_key=verify_key)
    manager = WorkspaceManager(
        config, source,
        restore_workspace=(
            _missing_transfer
            if config.workspace.persistence.enabled or config.workspace.session.persistence.enabled
            else _noop_restore
        ),
        commit_workspace=(
            _missing_transfer
            if config.workspace.persistence.enabled or config.workspace.session.persistence.enabled
            else _noop_commit
        ),
        reconnect_workspace=_noop_reconnect,
    )
    # Facade callbacks are bound after bundle construction; the manager is replaced with
    # an otherwise identical instance whose reconnect callback owns the real HTTP handshake.
    control = Control.__new__(Control)
    control.config, control.key, control.verify_key = config, key, verify_key
    control.catalog, control.public, control.storage = catalog, public, storage
    control._private_env = dict(env)
    control._ek = ek
    control.upstream_secrets = upstream_secrets
    control._model_headers, control._mcp_headers = model_headers, mcp_headers
    control.source, control.manager, control.delivery = source, manager, None
    control.admission, control.model_facade, control.mcp_facade = None, None, None
    control.memory_facade, control.mini_client = memory, mini_client
    control.a2a_facade = None
    control.artifact_facade = None
    control.facade_transport = facade_transport
    control.facade_clients = []
    control._prepared, control._started_run, control._stopping = {}, {}, False
    control._hook_endpoints = {}
    control.lifecycle = None
    manager.bind_callbacks(reconnect=control.reconnect_workspace, wait_mini_health=control.wait_mini_health)
    delivery = Delivery(manager, key=key, public=public, prepare=control.prepare,
                        clock=_clock, alarm=_delivery_alarm, transport=mini_transport)
    control.delivery = delivery
    control.admission = Admission(
        namespace=config.agent.namespace, agent_uid=config.agent.uid,
        max_concurrent_invocations=config.limits.max_concurrent_invocations,
        max_queued_total=config.limits.max_queued_total,
        idempotency_window_seconds=config.limits.idempotency_window_seconds,
        max_invocation_seconds=config.limits.max_invocation_seconds,
        workspace_max_concurrent_sessions=config.workspace.max_concurrent_sessions,
        channel_concurrency={channel.name: channel.concurrency for channel in config.channels},
        max_concurrent_scripts=config.limits.max_concurrent_scripts,
        clock=_clock, runner=control.run, alarm=_alarm,
    )
    facade_timeout = httpx.Timeout(None, connect=30.0)
    mcp_client = httpx.AsyncClient(transport=facade_transport, timeout=facade_timeout)
    model_client = httpx.AsyncClient(transport=facade_transport, timeout=facade_timeout)
    control.facade_clients.extend((mcp_client, model_client))
    mcp = McpFacade(
        servers=mcp_servers, server_headers=mcp_headers, key=key, owned_workspace=control._owned_live,
        exclude=excluded | frozenset(memory_excluded),
        exclude_tools=frozenset(config.capability.filter.exclude.tools), identity=identity,
        client=mcp_client,
    )
    model = ModelFacade(
        endpoint=model_endpoint, headers=model_headers, api=config.model.type, model=config.model.name,
        key=key,
        owned_workspace=control._owned_live, identity=identity,
        client=model_client,
    )
    control.model_facade, control.mcp_facade = model, mcp
    control.memory_facade = memory
    control._memory_configured = memory_configured
    control._memory_endpoint = memory_endpoint
    control._memory_project = memory_project
    control._memory_headers = memory_headers
    control._memory_identity = identity
    if catalog.a2a_agents:
        control.a2a_facade = A2AEgressFacade(
            agents=catalog.a2a_agents,
            ek=ek,
            identity=identity,
            key=key,
            owned_workspace=control._owned_live,
            exclude_tools=frozenset(config.capability.filter.exclude.tools),
        )
    if config.artifacts.enabled:
        control.artifact_facade = ArtifactFacade(
            key=key,
            owned_workspace=control._owned_live,
            operations=control.artifact_operations,
            exclude_tools=frozenset(config.capability.filter.exclude.tools),
        )
    metadata = catalog.model.metadata
    control._priced = metadata is not None and metadata.input_cost_per_token is not None
    register_secrets(upstream_secrets)
    lifecycle = Lifecycle(control)
    control.lifecycle = lifecycle
    return control


# Hook keys added after 0.1.12. A warm workspace pod keeps its image until it closes and an
# execution pod rejects unknown /configure keys, so these are omitted while unset: a newer
# control must still configure an older execution pod during a rollout.
_ADDED_HOOK_KEYS = ("workspaceStart", "workspaceStop")


def _public_wire(public: Any) -> dict[str, Any]:
    wire = public.model_dump(by_alias=True, mode="json")
    hooks = wire.get("hooks")
    if isinstance(hooks, dict):
        for key in _ADDED_HOOK_KEYS:
            if hooks.get(key) is None:
                hooks.pop(key, None)
    # Added in 0.1.18 with a default: sent only when the config sets it explicitly.
    if "max_stall_seconds" not in public.limits.model_fields_set:
        wire["limits"].pop("maxStallSeconds", None)
    # Added in 0.1.22: sent only when set, so a warm older execution pod still parses it.
    if not public.engine.deny_tools:
        wire["engine"].pop("denyTools", None)
    return wire


async def _noop_restore(endpoint: WorkspaceEndpoint) -> None:
    return None


async def _noop_commit(endpoint: WorkspaceEndpoint):
    return None


async def _noop_reconnect(endpoint: WorkspaceEndpoint) -> None:
    return None


async def _missing_transfer(endpoint: WorkspaceEndpoint):
    raise WorkspaceAssemblyUnavailable("persistent Workspace transfer is not available in this integration task")
