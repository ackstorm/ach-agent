# SPDX-License-Identifier: Apache-2.0
"""Accepted-event journal: an event admitted by this control is kept in agent storage
until its run reached a sandbox (an agent event) or finished (a script event), so the next
control replays what a restart would otherwise lose. Also keeps each cron channel's last
fired tick for the boot catch-up."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import quote

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.observability import alarm
from ach_runtime.storage.s3 import S3Storage

log = logging.getLogger(__name__)
_VERSION = 1


@dataclass(frozen=True)
class JournalEntry:
    invocation_id: str
    accepted_at: datetime
    kind: Literal["agent", "script"]
    event: Event
    workspace_key: str
    session_key: str
    rate_keys: tuple[str, ...]

    @property
    def routed_event(self) -> RoutedEvent:
        return RoutedEvent(event=self.event, workspace_key=self.workspace_key,
                           session_key=self.session_key, rate_keys=self.rate_keys)


class EventJournal:
    def __init__(self, storage: S3Storage, *, agent: str) -> None:
        self._storage = storage
        self._agent = quote(agent, safe="")
        self._dir = f"journal/{self._agent}/"

    def _name(self, invocation_id: str) -> str:
        return f"{self._dir}{quote(invocation_id, safe='')}.json"

    async def record(self, invocation_id: str, *, kind: str, event: Event,
                     routed: RoutedEvent | None) -> None:
        body = {
            "version": _VERSION,
            "invocationId": invocation_id,
            "acceptedAt": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "event": {"eventId": event.event_id, "channel": event.channel, "type": event.type,
                      "source": event.source, "payload": event.payload,
                      "context": event.context, "actor": event.actor},
            "workspaceKey": routed.workspace_key if routed else "",
            "sessionKey": routed.session_key if routed else "",
            "rateKeys": list(routed.rate_keys) if routed else [],
        }
        data = json.dumps(body, separators=(",", ":")).encode()
        await self._storage.put_record(self._name(invocation_id), data)

    async def forget(self, invocation_id: str) -> None:
        await self._storage.delete_record(self._name(invocation_id))

    async def pending(self) -> list[JournalEntry]:
        names = await self._storage.list_records(self._dir)
        entries: list[JournalEntry] = []
        for name, _ in names:
            raw = await self._storage.get_record(name)
            if raw is None:
                continue
            try:
                entries.append(_parse(json.loads(raw)))
            except (ValueError, KeyError, TypeError):
                log.warning("journal record %s is unreadable; deleted", name)
                alarm("journal_unavailable")
                await self._storage.delete_record(name)
        return sorted(entries, key=lambda e: e.accepted_at)

    def _tick_name(self, channel: str) -> str:
        return f"cron/{self._agent}/{quote(channel, safe='')}.json"

    async def last_tick(self, channel: str) -> datetime | None:
        raw = await self._storage.get_record(self._tick_name(channel))
        if raw is None:
            return None
        return datetime.fromisoformat(json.loads(raw)["tick"])

    async def record_tick(self, channel: str, tick: datetime) -> None:
        data = json.dumps({"tick": tick.astimezone(timezone.utc).isoformat()}).encode()
        await self._storage.put_record(self._tick_name(channel), data)


def _parse(body: dict) -> JournalEntry:
    if body.get("version") != _VERSION or body["kind"] not in ("agent", "script"):
        raise ValueError("unsupported journal record")
    ev = body["event"]
    return JournalEntry(
        invocation_id=body["invocationId"],
        accepted_at=datetime.fromisoformat(body["acceptedAt"]),
        kind=body["kind"],
        event=Event(event_id=ev["eventId"], channel=ev["channel"], type=ev["type"],
                    source=ev["source"], payload=ev["payload"], context=ev["context"],
                    actor=ev.get("actor", "")),
        workspace_key=body["workspaceKey"], session_key=body["sessionKey"],
        rate_keys=tuple(body["rateKeys"]),
    )
