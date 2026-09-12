# SPDX-License-Identifier: Apache-2.0
"""Bounded in-memory admission and completion correlation."""

from __future__ import annotations

import asyncio
import copy
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from pydantic import JsonValue

from ach_agent.channels.envelopes import Admission, Completion, EventRef, Submission
from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.router import RouterAdmitResult


class RegistryBusy(RuntimeError):
    """The local completion metadata bound is full; retry admission later."""


@dataclass(slots=True)
class _Record:
    completion: Completion
    owner: asyncio.Task[Submission]
    changed: asyncio.Event
    retained_at: float | None = None
    retained_bytes: int = 0


RouterHandle = Callable[[MessageEvent], Awaitable[RouterAdmitResult]]
TextSink = Callable[[str], None]
ToolSink = Callable[[Any], None]


class CompletionRegistry:
    """Correlate admitted events without making accepted work cancellation-sensitive."""

    def __init__(
        self,
        router_handle: RouterHandle,
        *,
        agent: str = "default",
        max_active_entries: int = 1024,
        max_completed_entries: int = 1024,
        retention_seconds: float = 300.0,
        max_result_bytes: int = 1_048_576,
        max_completed_bytes: int = 16_777_216,
        max_waiters: int = 1024,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            min(
                max_active_entries,
                max_completed_entries,
                max_result_bytes,
                max_completed_bytes,
                max_waiters,
            )
            < 1
        ):
            raise ValueError("registry limits must be positive")
        if retention_seconds < 0:
            raise ValueError("retention_seconds must be non-negative")
        self._router_handle = router_handle
        self._agent = agent
        self._max_active = max_active_entries
        self._max_completed = max_completed_entries
        self._retention = retention_seconds
        self._max_result_bytes = max_result_bytes
        self._max_completed_bytes = max_completed_bytes
        self._max_waiters = max_waiters
        self._waiter_count = 0
        self._completed_bytes = 0
        self._clock = clock
        self._records: dict[tuple[str, str, str], _Record] = {}
        self._completed: dict[tuple[str, str, str], _Record] = {}
        self._sinks: dict[tuple[str, str, str], tuple[TextSink | None, ToolSink | None]] = {}
        self._max_sinks = max_active_entries

    def ref_for(self, event: MessageEvent) -> EventRef:
        return EventRef(
            agent=self._agent,
            channel_name=event.channel_name,
            idempotency_key=event.idempotency_key,
        )

    def register_sinks(
        self, ref: EventRef, *, on_text: TextSink | None = None, on_tool: ToolSink | None = None
    ) -> None:
        """Keep local streaming callbacks outside the serializable event."""
        key = self._key(ref)
        if key not in self._sinks and len(self._sinks) >= self._max_sinks:
            raise RegistryBusy("completion registry sink limit reached")
        self._sinks[key] = (on_text, on_tool)

    def sinks(self, ref: EventRef) -> tuple[TextSink | None, ToolSink | None]:
        return self._sinks.get(self._key(ref), (None, None))

    def discard_sinks(self, ref: EventRef) -> None:
        self._sinks.pop(self._key(ref), None)

    async def submit(self, event: MessageEvent) -> Submission:
        ref = self.ref_for(event)
        self._purge_expired()
        key = self._key(ref)
        existing = self._records.get(key) or self._completed.get(key)
        if existing is not None:
            if key in self._records:
                outcome = await asyncio.shield(existing.owner)
                if outcome.admission is Admission.ACCEPTED:
                    return Submission(
                        admission=Admission.DUPLICATE,
                        completion=existing.completion.model_copy(deep=True),
                    )
                return outcome
            return Submission(
                admission=Admission.DUPLICATE,
                completion=existing.completion.model_copy(deep=True),
            )
        if len(self._records) >= self._max_active:
            raise RegistryBusy("completion registry active-entry limit reached")

        completion = Completion(
            ref=ref,
            invocation_id=uuid.uuid4().hex,
            state="queued",
        )
        changed = asyncio.Event()
        owner = asyncio.create_task(self._admit(event, ref))
        owner.add_done_callback(self._retrieve_owner_exception)
        self._records[key] = _Record(completion=completion, owner=owner, changed=changed)
        return await asyncio.shield(owner)

    async def _admit(self, event: MessageEvent, ref: EventRef) -> Submission:
        key = self._key(ref)
        record = self._records[key]
        try:
            result = await self._router_handle(event)
        except BaseException:
            self._mark_unavailable(record, "router admission failed")
            self._records.pop(key, None)
            raise
        if result is RouterAdmitResult.ACCEPTED:
            return Submission(
                admission=Admission.ACCEPTED,
                completion=record.completion.model_copy(deep=True),
            )
        self._mark_unavailable(record, "router rejected admission")
        self._records.pop(key, None)
        if result is RouterAdmitResult.FULL_QUEUE:
            return Submission(admission=Admission.FULL_QUEUE)
        unavailable = record.completion.model_copy(
            update={"state": "outcome_unavailable", "error": "completion not retained"}
        )
        return Submission(admission=Admission.DUPLICATE, completion=unavailable)

    def lookup(self, ref: EventRef) -> Completion:
        self._purge_expired()
        key = self._key(ref)
        record = self._records.get(key) or self._completed.get(key)
        if record is None:
            return self._unavailable(ref)
        return record.completion.model_copy(deep=True)

    async def wait(self, ref: EventRef) -> Completion:
        if self._waiter_count >= self._max_waiters:
            return self._unavailable(ref, "waiter limit reached")
        self._waiter_count += 1
        try:
            while True:
                self._purge_expired()
                key = self._key(ref)
                record = self._records.get(key) or self._completed.get(key)
                if record is None:
                    return self._unavailable(ref)
                if record.completion.state in {"completed", "failed", "outcome_unavailable"}:
                    return record.completion.model_copy(deep=True)
                await record.changed.wait()
        finally:
            self._waiter_count -= 1

    async def finish(
        self, ref: EventRef, result: JsonValue = None, error: str | None = None
    ) -> None:
        key = self._key(ref)
        record = self._records.get(key)
        if record is None:
            return
        candidate = Completion(
            ref=ref,
            invocation_id=record.completion.invocation_id,
            state="failed" if error is not None else "completed",
            result=copy.deepcopy(result),
            error=error,
        )
        if len(candidate.model_dump_json().encode()) > self._max_result_bytes:
            candidate = candidate.model_copy(
                update={"state": "outcome_unavailable", "result": None, "error": "result too large"}
            )
        record.completion = candidate
        record.retained_at = self._clock()
        record.retained_bytes = len(candidate.model_dump_json().encode())
        self._records.pop(key)
        self._completed[key] = record
        self._completed_bytes += record.retained_bytes
        record.changed.set()
        self._sinks.pop(key, None)
        self._trim_completed()

    async def finish_event(self, event: MessageEvent, error: str) -> None:
        await self.finish(self.ref_for(event), error=error)

    async def mark_running(self, ref: EventRef) -> None:
        """Record that admitted execution has started."""
        record = self._records.get(self._key(ref))
        if record is not None and record.completion.state == "queued":
            record.completion = record.completion.model_copy(update={"state": "running"})

    @property
    def active_count(self) -> int:
        return len(self._records)

    def _unavailable(self, ref: EventRef, error: str = "completion not retained") -> Completion:
        return Completion(
            ref=ref,
            invocation_id="",
            state="outcome_unavailable",
            error=error,
        )

    @staticmethod
    def _retrieve_owner_exception(task: asyncio.Task[Submission]) -> None:
        if task.cancelled():
            return
        task.exception()

    @staticmethod
    def _mark_unavailable(record: _Record, error: str) -> None:
        record.completion = record.completion.model_copy(
            update={"state": "outcome_unavailable", "error": error}
        )
        record.changed.set()

    def _purge_expired(self) -> None:
        now = self._clock()
        for key, record in list(self._completed.items()):
            if record.retained_at is not None and now - record.retained_at >= self._retention:
                del self._completed[key]
                self._completed_bytes -= record.retained_bytes

    def _trim_completed(self) -> None:
        while (
            len(self._completed) > self._max_completed
            or self._completed_bytes > self._max_completed_bytes
        ):
            oldest = min(self._completed, key=lambda key: self._completed[key].retained_at or 0)
            self._completed_bytes -= self._completed[oldest].retained_bytes
            del self._completed[oldest]

    @staticmethod
    def _key(ref: EventRef) -> tuple[str, str, str]:
        return (ref.agent, ref.channel_name, ref.idempotency_key)


class CompletionHandler:
    """Channel-facing adapter that preserves the router admission enum."""

    def __init__(self, registry: CompletionRegistry) -> None:
        self.registry = registry
        self.completion_port = registry

    async def handle(self, event: MessageEvent) -> RouterAdmitResult:
        submission = await self.registry.submit(event)
        return {
            Admission.ACCEPTED: RouterAdmitResult.ACCEPTED,
            Admission.DUPLICATE: RouterAdmitResult.DUPLICATE,
            Admission.FULL_QUEUE: RouterAdmitResult.FULL_QUEUE,
        }[submission.admission]
