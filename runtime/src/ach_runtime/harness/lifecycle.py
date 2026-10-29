# SPDX-License-Identifier: MIT
"""Harness-owned persistence callbacks and idle observations."""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import tempfile
import time
import traceback
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ach_runtime.storage.api import PersistReceipt, SnapshotTransientFailure
from ach_runtime.storage.archive import _run_owned, write_capped
from ach_runtime.storage.refs import DOMAIN_SESSION, DOMAIN_WORKSPACE, StateRef
from ach_runtime.harness.transfer import get_archive, put_archive

log = logging.getLogger(__name__)



_CLOSE_ARCHIVE_BYTES = 2 * 1024**3
# Tick delay by consecutive idle ticks (index 0 = activity seen): 1 s while busy, then
# 1 s, 2 s, 5 s while idle -- always capped below a tenth of the smallest idle timeout.
_TICK_DELAYS = (1.0, 1.0, 2.0, 5.0)


async def _owned(func, /, *args, **kwargs):
    result, cancelled, error = await _run_owned(func, *args, **kwargs)
    if cancelled:
        raise asyncio.CancelledError()
    if error is not None:
        raise error
    return result


def _new_close_archive() -> Path:
    fd, path = tempfile.mkstemp(prefix="ach-close-")
    os.close(fd)
    return Path(path)


def _close_archive_digest(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as archive:
        while chunk := archive.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _retryable_archive_error(exc: BaseException) -> bool:
    if isinstance(exc, (httpx.TransportError, TimeoutError)):
        return True
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {
        408, 429, 500, 502, 503, 504,
    }


class Lifecycle:
    """Connect the existing WorkspaceManager callbacks to global Storage."""

    def __init__(self, control: Any, *, alarm: Callable[[str], None] | None = None) -> None:
        self.control = control
        self._alarm = alarm or self._default_alarm
        self._leases: dict[tuple[str, str | None, str], Any] = {}
        self._bases: dict[tuple[str, str | None], str | None] = {}
        self._last_session: dict[tuple[str, str], datetime] = {}
        self._last_workspace: dict[str, datetime] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self._tick_task: asyncio.Task[None] | None = None
        self._saw_activity = False
        self._failed_session_saves: set[tuple[str, str, str]] = set()
        self._failed_workspace_saves: set[tuple[str, str]] = set()
        self._failed_session_retry_at: dict[tuple[str, str, str], float] = {}
        self._failed_workspace_retry_at: dict[tuple[str, str], float] = {}
        self._confirmed_session_closes: set[tuple[str, str, str]] = set()
        self._pending_session_retire: set[tuple[str, str, str]] = set()
        control.manager.bind_callbacks(restore=self.restore_workspace, commit=self.commit_workspace)

    @staticmethod
    def _default_alarm(reason: str) -> None:
        from ach_runtime.harness.control import _alarm

        _alarm(reason)

    def _report_failure(self, error: BaseException, reason: str) -> None:
        self._alarm(reason)
        try:
            setattr(error, "_ach_anomaly_reported", True)
        except Exception:
            pass
        if reason not in {
            "Harness Workspace persistence failed",
            "Harness session persistence failed",
        }:
            return
        detail = error
        if isinstance(error, SnapshotTransientFailure) and error.__cause__ is not None:
            detail = error.__cause__
        from ach_runtime.harness.control import _failure_reason

        log.error(
            "%s: error_type=%s cause=%s",
            reason,
            type(error).__name__,
            _failure_reason(detail, limit=300),
        )

    async def start(self) -> None:
        """Start the single Harness-owned idle observer after owned inventory."""
        if self._closed:
            raise RuntimeError("Lifecycle is already closed")
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = asyncio.create_task(self._tick_loop(), name="harness-lifecycle")

    def _admitted_work(self) -> bool:
        """Local check (no request): does any open Workspace hold admitted work?"""
        return any(self.control.admission.has_work(ref) for ref in list(self.control.manager.active_endpoints()))

    def _tick_delay(self, idle_ticks: int) -> float:
        workspace = self.control.config.workspace
        cap = max(1.0, 0.1 * min(workspace.idle_timeout_seconds, workspace.session.idle_timeout_seconds))
        return min(_TICK_DELAYS[min(idle_ticks, len(_TICK_DELAYS) - 1)], cap)

    async def _tick_loop(self) -> None:
        idle_ticks = 0
        failures = 0
        while not self._closed:
            self._saw_activity = False
            try:
                await self.tick(datetime.now(timezone.utc))
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                self._alarm("Harness lifecycle tick failed")
                self._log_tick_failure(exc, failures)
            if failures:
                # A failing tick is not activity, and admitted work must not cut its wait:
                # a deterministic failure walks the backoff ladder instead of spinning at 1 Hz.
                idle_ticks = failures
            else:
                idle_ticks = 0 if self._saw_activity or self._admitted_work() else idle_ticks + 1
            delay = self._tick_delay(idle_ticks)
            # Sleep in <= 1 s slices so newly admitted work ends an idle backoff within 1 s.
            slept = 0.0
            while True:
                step = min(1.0, delay - slept)
                await asyncio.sleep(step)
                slept += step
                if slept >= delay or (not failures and self._admitted_work()):
                    break

    @staticmethod
    def _log_tick_failure(exc: BaseException, failures: int) -> None:
        """Log the redacted cause of every failed tick; the first of a streak also gets the
        traceback frames (code locations only -- the raw message would bypass redaction)."""
        from ach_runtime.harness.control import _failure_reason

        frames = "".join(traceback.format_tb(exc.__traceback__)) if failures == 1 else ""
        log.error(
            "Harness lifecycle tick failed: consecutive_failures=%d cause=%s%s",
            failures, _failure_reason(exc, limit=300), f"\n{frames}" if frames else "",
        )

    def _ref(self, endpoint: Any, domain: str, session_ref: str | None = None) -> StateRef:
        return StateRef(agent=self.control.config.agent, workspace_ref=endpoint.ref.workspace_ref,
                        domain=domain, session_ref=session_ref)

    def workspace_state_ref(self, endpoint: Any) -> StateRef:
        return self._ref(endpoint, DOMAIN_WORKSPACE)

    async def restore_workspace(self, endpoint: Any) -> None:
        """Called by the manager for a fresh, reactivated or recovered pod: restore the
        persisted workspace (when enabled), then run the workspaceStart hook. A failing
        start hook is alarmed but never blocks the workspace (a reactivated pod that failed
        to start would stay unusable until control restarts); gate runs in sessionStart."""
        restored = False
        if self.control.config.workspace.persistence.enabled:
            restored = await self._restore_workspace_archive(endpoint)
        try:
            await self.control.run_workspace_hook(endpoint, "start", restored=restored)
        except Exception as exc:
            from ach_runtime.observability import alarm

            log.warning("workspaceStart hook failed: %s", exc)
            alarm("workspace_start_failed", workspace_ref=endpoint.ref.workspace_ref)

    async def _restore_workspace_archive(self, endpoint: Any) -> bool:
        retention_days = self.control.config.workspace.persistence.retention_days
        ref = self.workspace_state_ref(endpoint)
        try:
            receipt = await self.control.storage.restore(ref, retention_days=retention_days)
            if receipt is not None:
                await put_archive(self.control.mini_client, endpoint, self.control._headers(endpoint),
                                  "/workspace/archive", receipt.open(),
                                  on_mismatch=self.control.manager.report_lost)
                self._bases[(ref.workspace_ref, None)] = receipt.etag
            lease = await self.control.storage.acquire_writer(ref, endpoint.instance_id)
            self._leases[(ref.workspace_ref, None, endpoint.instance_id)] = lease
            self._bases[(ref.workspace_ref, None)] = lease.base_etag
        except Exception:
            from ach_runtime.observability import alarm

            alarm("restore_failed", workspace_ref=ref.workspace_ref)
            raise
        return receipt is not None

    async def _persist_close(
        self, endpoint: Any, ref: StateRef, archive_path: str, *, retention_days: int
    ) -> PersistReceipt:
        """Capture one complete archive and retry only that close's transient writes."""
        deadline = time.monotonic() + self.control.config.workspace.shutdown_timeout_seconds
        snapshot_id = uuid.uuid4().hex
        lease_key = (ref.workspace_ref, ref.session_ref, endpoint.instance_id)
        base_key = (ref.workspace_ref, ref.session_ref)
        lease = self._leases.get(lease_key)
        if lease is None:
            lease = await self.control.storage.acquire_writer(ref, endpoint.instance_id)
            self._leases[lease_key] = lease
        if base_key not in self._bases:
            self._bases[base_key] = lease.base_etag
        expected_base = self._bases[base_key]

        path = await _owned(_new_close_archive)
        delay = 1.0

        async def retry_wait(error: BaseException) -> None:
            nonlocal delay
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise SnapshotTransientFailure("close retry window exhausted") from error
            await asyncio.sleep(min(delay, 30.0, remaining))
            delay = min(delay * 2, 30.0)
            if deadline - time.monotonic() <= 0:
                raise SnapshotTransientFailure("close retry window exhausted") from error

        async def file_chunks():
            handle = await _owned(path.open, "rb")
            try:
                while chunk := await _owned(handle.read, 64 * 1024):
                    yield chunk
            finally:
                await _owned(handle.close)

        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SnapshotTransientFailure("close archive export retry window exhausted")
                try:
                    async with asyncio.timeout(remaining):
                        await write_capped(
                            get_archive(
                                self.control.mini_client, endpoint, self.control._headers(endpoint),
                                archive_path, on_mismatch=self.control.manager.report_lost,
                            ),
                            path,
                            max_bytes=_CLOSE_ARCHIVE_BYTES,
                        )
                    break
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:
                    if not _retryable_archive_error(exc):
                        raise
                    await retry_wait(exc)

            checksum, size = await _owned(_close_archive_digest, path)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SnapshotTransientFailure("close persistence retry window exhausted")
                try:
                    async with asyncio.timeout(remaining):
                        receipt = await self.control.storage.persist(
                            ref, snapshot_id, file_chunks(), lease, expected_base,
                            retention_days=retention_days,
                        )
                    if (
                        receipt.ref != ref
                        or receipt.snapshot_id != snapshot_id
                        or receipt.writer_epoch != lease.epoch
                        or not receipt.etag
                        or receipt.generation <= 0
                        or receipt.version <= 0
                        or receipt.checksum != checksum
                        or receipt.size != size
                    ):
                        raise RuntimeError("Storage returned an invalid close receipt")
                    self._bases[base_key] = receipt.etag
                    return receipt
                except asyncio.CancelledError:
                    raise
                except SnapshotTransientFailure as exc:
                    await retry_wait(exc)
                except TimeoutError as exc:
                    await retry_wait(exc)
        finally:
            await _owned(path.unlink, missing_ok=True)

    async def commit_workspace(self, endpoint: Any) -> PersistReceipt | None:
        """Called by the manager on a clean idle close: run the workspaceStop hook, then
        snapshot the workspace (when enabled). A failing stop hook never blocks the close.
        A retried close runs the hook again, so stop hooks must be idempotent."""
        try:
            await self.control.run_workspace_hook(endpoint, "stop")
        except Exception as exc:
            from ach_runtime.observability import alarm

            log.warning("workspaceStop hook failed: %s", exc)
            alarm("workspace_stop_failed", workspace_ref=endpoint.ref.workspace_ref)
        if not self.control.config.workspace.persistence.enabled:
            return None
        ref = self._ref(endpoint, DOMAIN_WORKSPACE)
        try:
            return await self._persist_close(
                endpoint, ref, "/workspace/archive",
                retention_days=self.control.config.workspace.persistence.retention_days,
            )
        except Exception as exc:
            self._report_failure(exc, "Harness Workspace persistence failed")
            raise

    async def restore_session(self, ticket: Any, endpoint: Any) -> None:
        """Restore only the admitted opaque session ref; a live local session wins."""
        if not self.control.config.workspace.session.persistence.enabled:
            return
        retention_days = self.control.config.workspace.session.persistence.retention_days
        ref = self._ref(endpoint, DOMAIN_SESSION, ticket.session_ref)
        try:
            status, info = await self.control._mini_json(
                endpoint, "GET", f"/sessions/{ticket.session_ref}/info"
            )
            if status == 200 and isinstance(info, dict):
                if info.get("active") or info.get("initialized"):
                    return
            elif status != 404:
                raise RuntimeError("mini-harness returned invalid session information")
            receipt = await self.control.storage.restore(ref, retention_days=retention_days)
            if receipt is None:
                return
            await put_archive(
                self.control.mini_client, endpoint, self.control._headers(endpoint),
                f"/sessions/{ticket.session_ref}/archive", receipt.open(),
                params={"deadline_at": ticket.deadline_at.isoformat()},
                on_mismatch=self.control.manager.report_lost,
            )
            lease = await self.control.storage.acquire_writer(ref, endpoint.instance_id)
            self._leases[(ref.workspace_ref, ref.session_ref, endpoint.instance_id)] = lease
            self._bases[(ref.workspace_ref, ref.session_ref)] = lease.base_etag
        except Exception:
            from ach_runtime.observability import alarm

            alarm("restore_failed", workspace_ref=ref.workspace_ref, session_ref=ref.session_ref)
            raise

    @asynccontextmanager
    async def operation(self, workspace_ref: str):
        lock = self._locks.setdefault(workspace_ref, asyncio.Lock())
        async with lock:
            yield

    async def _ensure_workspace_writer(self, endpoint: Any) -> None:
        if not self.control.config.workspace.persistence.enabled:
            return
        key = (endpoint.ref.workspace_ref, None, endpoint.instance_id)
        if key in self._leases:
            return
        ref = self._ref(endpoint, DOMAIN_WORKSPACE)
        lease = await self.control.storage.acquire_writer(ref, endpoint.instance_id)
        self._leases[key] = lease
        self._bases[(ref.workspace_ref, None)] = lease.base_etag

    async def _save_session(self, endpoint: Any, session_ref: str) -> None:
        enabled = self.control.config.workspace.session.persistence.enabled
        ref = self._ref(endpoint, DOMAIN_SESSION, session_ref)
        if not enabled:
            try:
                async for _ in get_archive(
                    self.control.mini_client, endpoint, self.control._headers(endpoint),
                    f"/sessions/{session_ref}/archive", on_mismatch=self.control.manager.report_lost,
                ):
                    pass
            except Exception as exc:
                self._report_failure(exc, "Harness session archive export failed")
                raise
        else:
            try:
                await self._persist_close(
                    endpoint, ref, f"/sessions/{session_ref}/archive",
                    retention_days=self.control.config.workspace.session.persistence.retention_days,
                )
            except Exception as exc:
                self._report_failure(exc, "Harness session persistence failed")
                raise
        await self._report_suspend_hook(endpoint, session_ref)
        await self._retire_session(endpoint, session_ref)

    async def _retire_session(self, endpoint: Any, session_ref: str) -> None:
        """Postcommit retire (I6): never re-Suspends or re-exports — a transient transport
        failure here must retry only this idempotent call, not another snapshot/commit."""
        try:
            status, body = await self.control._mini_json(
                endpoint, "POST", f"/sessions/{session_ref}/retire"
            )
        except Exception as exc:
            self._mark_retire_pending(exc)
            self._report_failure(exc, "Harness session retire failed")
            raise
        if (
            status != 200 or not isinstance(body, dict)
            or body.get("status") != "retired" or body.get("sessionRef") != session_ref
        ):
            error = RuntimeError("mini-harness did not confirm session retire")
            self._mark_retire_pending(error)
            self._report_failure(error, "Harness session retire failed")
            raise error

    @staticmethod
    def _mark_retire_pending(error: BaseException) -> None:
        try:
            setattr(error, "_ach_retire_pending", True)
        except Exception:
            pass

    async def _attempt_session_save(
        self, endpoint: Any, session_ref: str, failed_key: tuple[str, str, str]
    ) -> bool:
        """Shared retry entry for both the idle-session and stopped-session close paths.
        A session already committed but stuck on a failed postcommit retire only retries
        the idempotent retire call on the next tick — never another snapshot/commit."""
        try:
            if failed_key in self._pending_session_retire:
                await self._retire_session(endpoint, session_ref)
            else:
                await self._save_session(endpoint, session_ref)
        except Exception as exc:
            self._failed_session_saves.add(failed_key)
            retire_pending = getattr(exc, "_ach_retire_pending", False)
            if retire_pending:
                self._pending_session_retire.add(failed_key)
            if isinstance(exc, SnapshotTransientFailure) or retire_pending:
                self._failed_session_retry_at[failed_key] = time.monotonic() + 30.0
            if not getattr(exc, "_ach_anomaly_reported", False):
                self._alarm("Harness session persistence failed")
            return False
        self._pending_session_retire.discard(failed_key)
        self._failed_session_saves.discard(failed_key)
        self._failed_session_retry_at.pop(failed_key, None)
        return True

    async def _report_suspend_hook(self, endpoint: Any, session_ref: str) -> None:
        try:
            status, info = await self.control._mini_json(
                endpoint, "GET", f"/sessions/{session_ref}/info"
            )
        except Exception:
            return
        if status == 200 and isinstance(info, dict) and info.get("suspendHookFailed") is True:
            self._alarm("Harness suspend hook failed")

    async def tick(self, now: datetime) -> None:
        """Snapshot settled native sessions before starting Workspace idle time."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("lifecycle time must be timezone-aware")
        endpoints = list(self.control.manager.active_endpoints().items())
        if not endpoints:
            return
        batch = asyncio.gather(
            *(self._tick_workspace(workspace_ref, endpoint, now)
              for workspace_ref, endpoint in endpoints),
            return_exceptions=True,
        )
        try:
            results = await asyncio.shield(batch)
        except asyncio.CancelledError:
            try:
                await asyncio.shield(batch)
            except asyncio.CancelledError:
                # A repeated cancellation still must not leave Workspace work
                # detached while close proceeds to close the shared clients.
                await batch
            raise
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _tick_workspace(self, workspace_ref: str, endpoint: Any, now: datetime) -> None:
        lock = self._locks.setdefault(workspace_ref, asyncio.Lock())
        async with lock:
            if self.control.manager.active_endpoint(workspace_ref) != endpoint:
                return
            workspace_key = (workspace_ref, endpoint.instance_id)
            try:
                health_status, _ = await self.control._mini_json(
                    endpoint, "GET", "/healthz"
                )
            except Exception:
                health_status = None
            if health_status != 200:
                self._saw_activity = True
                await self.control.manager.recover(endpoint.ref, endpoint.instance_id)
                return
            if workspace_key in self._failed_workspace_saves:
                retry_at = self._failed_workspace_retry_at.get(workspace_key)
                if retry_at is None or time.monotonic() < retry_at:
                    return
            try:
                await self._ensure_workspace_writer(endpoint)
            except Exception:
                self._alarm("Harness Workspace persistence failed")
                return
            status, sessions = await self.control._mini_json(endpoint, "GET", "/sessions")
            if status != 200 or not isinstance(sessions, list):
                raise RuntimeError("mini-harness returned invalid session inventory")
            runs = await self.control.delivery.list_runs(endpoint)
            active_refs: set[str] = set()
            uncertain = False
            for item in sessions:
                if not isinstance(item, dict) or not isinstance(item.get("sessionRef"), str):
                    raise RuntimeError("mini-harness returned invalid session inventory")
                session_ref = item["sessionRef"]
                info_status, info = await self.control._mini_json(
                    endpoint, "GET", f"/sessions/{session_ref}/info"
                )
                if info_status != 200 or not isinstance(info, dict):
                    raise RuntimeError("mini-harness returned invalid session information")
                session_close_key = (workspace_ref, session_ref, endpoint.instance_id)
                if info.get("uncertain"):
                    self._confirmed_session_closes.discard(session_close_key)
                    uncertain = True
                    continue
                native_live = bool(info.get("active"))
                has_work = self.control.admission.has_work(workspace_ref, session_ref)
                has_run = any(r.session_ref == session_ref and r.state not in {"completed", "failed", "cancelled", "skipped"} for r in runs)
                key = (workspace_ref, session_ref)
                failed_key = (workspace_ref, session_ref, endpoint.instance_id)
                if has_work or has_run:
                    self._saw_activity = True
                    self._confirmed_session_closes.discard(session_close_key)
                    self._last_session[key] = now
                    active_refs.add(session_ref)
                    continue
                if failed_key in self._failed_session_saves:
                    retry_at = self._failed_session_retry_at.get(failed_key)
                    if retry_at is None or time.monotonic() < retry_at:
                        active_refs.add(session_ref)
                        continue
                    self._failed_session_saves.discard(failed_key)
                    self._failed_session_retry_at.pop(failed_key, None)
                if native_live:
                    self._confirmed_session_closes.discard(session_close_key)
                    active_refs.add(session_ref)
                    last = self._last_session.setdefault(key, now)
                    idle = (now - last).total_seconds()
                    if idle >= self.control.config.workspace.session.idle_timeout_seconds:
                        if not await self._attempt_session_save(endpoint, session_ref, failed_key):
                            active_refs.add(session_ref)
                            continue
                        if self.control.config.workspace.session.persistence.enabled:
                            self._confirmed_session_closes.add(session_close_key)
                        self._last_session.pop(key, None)
                else:
                    self._last_session.pop(key, None)
                    initialized = bool(info.get("initialized"))
                    native_session_id = info.get("nativeSessionId")
                    persistence_enabled = self.control.config.workspace.session.persistence.enabled
                    if (persistence_enabled and (initialized or native_session_id)
                            and session_close_key not in self._confirmed_session_closes):
                        if not await self._attempt_session_save(endpoint, session_ref, failed_key):
                            active_refs.add(session_ref)
                            continue
                        self._confirmed_session_closes.add(session_close_key)
            queued = self.control.admission.has_work(workspace_ref)
            if active_refs or uncertain or queued:
                self._saw_activity = self._saw_activity or uncertain or queued
                self._last_workspace.pop(workspace_ref, None)
                return
            last_idle = self._last_workspace.setdefault(workspace_ref, now)
            if (now - last_idle).total_seconds() >= self.control.config.workspace.idle_timeout_seconds:
                try:
                    await self.control.manager.close(endpoint.ref)
                except Exception as exc:
                    self._failed_workspace_saves.add(workspace_key)
                    if isinstance(exc, SnapshotTransientFailure):
                        self._failed_workspace_retry_at[workspace_key] = time.monotonic() + 30.0
                    if not getattr(exc, "_ach_anomaly_reported", False):
                        self._alarm("Harness Workspace persistence failed")
                    return
                self._failed_workspace_saves.discard(workspace_key)
                self._failed_workspace_retry_at.pop(workspace_key, None)
                self._last_workspace.pop(workspace_ref, None)

    async def close(self) -> None:
        self._closed = True
        if self._tick_task is not None:
            self._tick_task.cancel()
            await asyncio.gather(self._tick_task, return_exceptions=True)
