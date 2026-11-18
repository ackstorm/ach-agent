# SPDX-License-Identifier: Apache-2.0
from datetime import datetime, timezone
import asyncio

from fakes import FakeS3Client
from ach_runtime.storage.s3 import S3Storage
from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.harness.journal import EventJournal


def _journal(client=None):
    return EventJournal(S3Storage(bucket="b", prefix="p", client=client or FakeS3Client()), agent="rev/iew")


EVENT = Event(event_id="e1", channel="hook", type="merge_request", source="gitlab",
              payload={"a": 1}, context={"gitlab": {"x": 2}}, actor="bob")
ROUTED = RoutedEvent(event=EVENT, workspace_key="w", session_key="s", rate_keys=("k",))


def test_record_then_pending_round_trips_the_routed_event():
    async def scenario():
        j = _journal()
        await j.record("inv1", kind="agent", event=EVENT, routed=ROUTED)
        [entry] = await j.pending()
        assert entry.invocation_id == "inv1" and entry.kind == "agent"
        assert entry.routed_event == ROUTED
        assert entry.accepted_at.tzinfo is not None
    asyncio.run(scenario())


def test_forget_removes_the_entry_and_is_idempotent():
    async def scenario():
        j = _journal()
        await j.record("inv1", kind="agent", event=EVENT, routed=ROUTED)
        await j.forget("inv1")
        await j.forget("inv1")
        assert await j.pending() == []
    asyncio.run(scenario())


def test_script_entries_keep_the_event_without_routing():
    async def scenario():
        j = _journal()
        await j.record("inv2", kind="script", event=EVENT, routed=None)
        [entry] = await j.pending()
        assert entry.kind == "script" and entry.event == EVENT
    asyncio.run(scenario())


def test_unparseable_record_is_deleted_not_returned():
    async def scenario():
        storage = S3Storage(bucket="b", prefix="p", client=FakeS3Client())
        j = EventJournal(storage, agent="rev/iew")
        await storage.put_record("journal/rev%2Fiew/bad.json", b"{not json")
        assert await j.pending() == []
        assert await storage.get_record("journal/rev%2Fiew/bad.json") is None
    asyncio.run(scenario())


def test_entries_are_scoped_per_agent():
    async def scenario():
        client = FakeS3Client()
        a = EventJournal(S3Storage(bucket="b", prefix="p", client=client), agent="a")
        b = EventJournal(S3Storage(bucket="b", prefix="p", client=client), agent="b")
        await a.record("inv1", kind="agent", event=EVENT, routed=ROUTED)
        assert await b.pending() == []
    asyncio.run(scenario())


def test_last_tick_round_trip():
    async def scenario():
        j = _journal()
        assert await j.last_tick("tick") is None
        t = datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc)
        await j.record_tick("tick", t)
        assert await j.last_tick("tick") == t
    asyncio.run(scenario())
