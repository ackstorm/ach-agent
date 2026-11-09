from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from fakes import FakeS3Client

from a2a.types.a2a_pb2 import TASK_STATE_COMPLETED, TASK_STATE_WORKING, Task, TaskStatus
from ach_runtime.channels.a2a_store import StorageTaskStore
from ach_runtime.storage.s3 import S3Storage


def _ctx(user: str) -> SimpleNamespace:
    return SimpleNamespace(user=SimpleNamespace(user_name=user))


def _store(client: FakeS3Client | None = None, agent: str = "a") -> StorageTaskStore:
    storage = S3Storage(bucket="b", prefix="p", client=client or FakeS3Client())
    return StorageTaskStore(storage, agent=agent, channel="review")


def _task(task_id: str, state: int = TASK_STATE_WORKING) -> Task:
    return Task(id=task_id, context_id="c", status=TaskStatus(state=state))


async def test_owner_gets_the_task_and_others_do_not() -> None:
    store = _store()
    await store.save(_task("t1"), _ctx("alice"))
    assert await store.get("t1", _ctx("alice")) == _task("t1")
    assert await store.get("t1", _ctx("bob")) is None
    assert [t.id for t in (await store.list(SimpleNamespace(context_id="c"), _ctx("alice"))).tasks] == ["t1"]
    assert len((await store.list(SimpleNamespace(context_id=""), _ctx("bob"))).tasks) == 0


async def test_pending_lists_only_non_terminal_tasks() -> None:
    store = _store()
    await store.save(_task("t1"), _ctx("alice"))
    await store.save(_task("t2", TASK_STATE_COMPLETED), _ctx("alice"))
    assert [t.id for t in await store.pending()] == ["t1"]


async def test_finish_and_annotate_keep_the_owner() -> None:
    store = _store()
    await store.save(_task("t1"), _ctx("alice"))
    task = (await store.pending())[0]
    await store.annotate(task, {"invocationId": "i1"})
    await store.finish(task, "completed", "done")
    got = await store.get("t1", _ctx("alice"))
    assert got.status.state == TASK_STATE_COMPLETED
    assert got.status.message.parts[0].text == "done"
    assert got.metadata["invocationId"] == "i1"
    assert await store.get("t1", _ctx("bob")) is None


async def test_prune_drops_old_records() -> None:
    store = _store()
    await store.save(_task("t1"), _ctx("alice"))
    await store.prune(datetime.now(UTC) - timedelta(days=1))
    assert await store.get("t1", _ctx("alice")) is not None
    await store.prune(datetime.now(UTC) + timedelta(days=1))
    assert await store.get("t1", _ctx("alice")) is None


async def test_pending_sees_only_its_own_agents_tasks() -> None:
    shared = FakeS3Client()  # one storage prefix serves every Agent in the namespace
    await _store(shared, agent="finops").save(_task("t1"), _ctx("alice"))
    await _store(shared, agent="doctor").save(_task("t2"), _ctx("alice"))
    assert [t.id for t in await _store(shared, agent="finops").pending()] == ["t1"]
