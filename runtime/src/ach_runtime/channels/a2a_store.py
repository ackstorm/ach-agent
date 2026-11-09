# SPDX-License-Identifier: Apache-2.0

"""A2A task store on the Agent's storage: tasks outlive a control restart."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

from a2a.server.owner_resolver import OwnerResolver, resolve_user_scope
from a2a.server.tasks.task_store import TaskStore
from a2a.types.a2a_pb2 import (
    TASK_STATE_CANCELED,
    TASK_STATE_COMPLETED,
    TASK_STATE_FAILED,
    TASK_STATE_REJECTED,
    TASK_STATE_WORKING,
    ListTasksResponse,
    Message,
    Task,
)
from google.protobuf import json_format

_STATES = {
    "canceled": TASK_STATE_CANCELED,
    "completed": TASK_STATE_COMPLETED,
    "failed": TASK_STATE_FAILED,
    "rejected": TASK_STATE_REJECTED,
    "working": TASK_STATE_WORKING,
}
_TERMINAL = {TASK_STATE_CANCELED, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_REJECTED}


class StorageTaskStore(TaskStore):
    """One record per task at `a2a/<agent>/<channel>/<task_id>.json`: `{"owner", "task"}`.
    The storage prefix is shared by every Agent in the namespace, so the agent name keeps
    one Agent's recovery off another's tasks."""

    def __init__(
        self, storage: Any, *, agent: str, channel: str, owner_resolver: OwnerResolver = resolve_user_scope
    ) -> None:
        self._storage = storage
        self._dir = f"a2a/{quote(agent, safe='')}/{quote(channel, safe='')}/"
        self._owner_resolver = owner_resolver

    def _name(self, task_id: str) -> str:
        return f"{self._dir}{quote(task_id, safe='')}.json"

    async def _write(self, owner: str, task: Task) -> None:
        doc = {"owner": owner, "task": json_format.MessageToDict(task)}
        await self._storage.put_record(self._name(task.id), json.dumps(doc).encode())

    async def _read(self, name: str) -> tuple[str, Task] | None:
        raw = await self._storage.get_record(name)
        if raw is None:
            return None
        doc = json.loads(raw)
        return doc["owner"], json_format.ParseDict(doc["task"], Task())

    async def save(self, task: Task, context: Any) -> None:
        await self._write(self._owner_resolver(context), task)

    async def get(self, task_id: str, context: Any) -> Task | None:
        found = await self._read(self._name(task_id))
        if found is None or found[0] != self._owner_resolver(context):
            return None
        return found[1]

    async def list(self, params: Any, context: Any) -> ListTasksResponse:
        # ponytail: no pagination or status filters; add when a caller needs them.
        owner = self._owner_resolver(context)
        tasks = []
        for name, _ in await self._storage.list_records(self._dir):
            found = await self._read(name)
            if found and found[0] == owner and (not params.context_id or found[1].context_id == params.context_id):
                tasks.append(found[1])
        return ListTasksResponse(tasks=tasks, total_size=len(tasks), page_size=len(tasks))

    async def delete(self, task_id: str, context: Any) -> None:
        if await self.get(task_id, context) is not None:
            await self._storage.delete_record(self._name(task_id))

    async def pending(self) -> list[Task]:
        """Every stored task that is not terminal, whatever its owner."""
        tasks = []
        for name, _ in await self._storage.list_records(self._dir):
            found = await self._read(name)
            if found and found[1].status.state not in _TERMINAL:
                tasks.append(found[1])
        return tasks

    async def prune(self, older_than: datetime) -> None:
        for name, modified in await self._storage.list_records(self._dir):
            if modified < older_than:
                await self._storage.delete_record(name)

    async def _owner(self, task: Task) -> str:
        found = await self._read(self._name(task.id))
        return found[0] if found is not None else ""

    async def finish(self, task: Task, state: str, text: str | None) -> None:
        """Set the task's status and save it under its stored owner."""
        owner = await self._owner(task)
        task.status.state = _STATES[state]
        task.status.timestamp.GetCurrentTime()
        if text is not None:
            message = Message()
            message.parts.add().text = text
            task.status.message.CopyFrom(message)
        await self._write(owner, task)

    async def annotate(self, task: Task, metadata: dict[str, str]) -> None:
        """Merge into `task.metadata` and save under the stored owner."""
        owner = await self._owner(task)
        task.metadata.update(metadata)
        await self._write(owner, task)
