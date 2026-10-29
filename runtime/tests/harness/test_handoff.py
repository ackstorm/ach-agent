# SPDX-License-Identifier: MIT
from __future__ import annotations

import asyncio
import os
import pytest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.channels.gitlab import normalize_gitlab
from ach_runtime.harness.admission import AdmissionTicket
from ach_runtime.harness.handoff import _event_env, run_handoff


def test_handoff_uses_pinned_workspace_and_event_environment() -> None:
    async def scenario(root: Path) -> None:
        root.mkdir(exist_ok=True)
        cfg = SimpleNamespace(script="printf '%s' \"$ACH_EVENT_ID:$ACH_SESSION_KEY:$ACH_WORKSPACE:$ACH_WORKSPACE_KEY\" > result",
                              env={"PATH": "/bin:/usr/bin", "ACH_WORKSPACE_KEY": "configured"},
                              secret_env={"ACH_WORKSPACE_KEY": SimpleNamespace(env="SECRET_WORKSPACE_KEY")}, timeout_seconds=5,
                              destination="result-dir", scope="event")
        routed = RoutedEvent(Event("evt", "ch", "push", "webhook", {},
                                   {"project": "team/repo", "workspace_key": "event-value"}),
                             "team/repo", "opaque-session")
        loop = asyncio.get_running_loop()
        ticket = AdmissionTicket("inv", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "w"*64, "s"*64,
                                 routed, datetime.now(UTC)+timedelta(seconds=20), loop.create_future(), loop.create_future())
        await run_handoff(cfg, routed.event, ticket, root,
                          environ={"ACH_WORKSPACE_KEY": "inherited", "SECRET_WORKSPACE_KEY": "secret-value"})
        value = (root / "result").read_text()
        assert value.startswith("evt:opaque-session:")
        assert value.endswith(":team/repo")
        assert ticket.workspace_ref not in value
    with __import__("tempfile").TemporaryDirectory() as temporary:
        asyncio.run(scenario(Path(temporary)))


def test_handoff_timeout_joins_descendant_processes(tmp_path: Path) -> None:
    async def scenario() -> int:
        cfg = SimpleNamespace(script="sleep 30 & echo $! > child.pid; wait", env={}, secret_env={},
                              timeout_seconds=0.2, destination="handoff", scope="event")
        routed = RoutedEvent(Event("evt", "ch", "push", "webhook"), "key", "sess")
        ticket = AdmissionTicket("inv", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "w"*64, "s"*64,
                                 routed, datetime.now(UTC)+timedelta(seconds=10),
                                 asyncio.get_running_loop().create_future(), asyncio.get_running_loop().create_future())
        with pytest.raises(TimeoutError):
            await run_handoff(cfg, routed.event, ticket, tmp_path, environ={"PATH": "/bin:/usr/bin"})
        return int((tmp_path / "child.pid").read_text())
    child = asyncio.run(scenario())
    for _ in range(50):
        stat_path = Path(f"/proc/{child}/stat")
        try:
            state = stat_path.read_text().split()[2]
        except FileNotFoundError:
            break
        if state == "Z":
            break
        import time
        time.sleep(0.01)
    else:
        raise AssertionError("handoff descendant remained alive after timeout cleanup")


def test_handoff_projects_gitlab_clone_environment_from_normalized_mr(tmp_path: Path) -> None:
    payload = {
        "object_kind": "merge_request",
        "project": {"id": 42, "path_with_namespace": "team/service"},
        "object_attributes": {"iid": 7, "last_commit": {"id": "abc123"}},
        "user": {"username": "alice"},
    }
    channel = SimpleNamespace(
        name="gitlab-pr-review",
        webhook=SimpleNamespace(
            gitlab_events=["merge_request"], bot_username=None,
            trigger_users=None, merge_requests_only=False,
        ),
    )
    event = normalize_gitlab(
        payload, {"X-Gitlab-Event-UUID": "delivery-7"}, channel,
    )
    assert event is not None
    result = tmp_path / "clone-env"
    workspace = tmp_path / "workspace"
    cfg = SimpleNamespace(
        script='printf "%s:%s:%s:%s:%s" "$ACH_EVENT_PROJECT_PATH" "$ACH_EVENT_MR_IID" "$ACH_EVENT_HEAD_SHA" "$ACH_SESSION_KEY" "$ACH_WORKSPACE_KEY" > "$RESULT"',
        env={"RESULT": str(result)}, secret_env={}, timeout_seconds=5,
        destination="handoff", scope="event",
    )
    routed = RoutedEvent(event, "team/service", "42:7")
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        ticket = AdmissionTicket(
            "inv", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "w" * 64, "s" * 64,
            routed, datetime.now(UTC) + timedelta(seconds=20), loop.create_future(), loop.create_future(),
        )
        loop.run_until_complete(run_handoff(
            cfg, event, ticket, workspace, environ={"PATH": "/bin:/usr/bin"},
        ))
    finally:
        loop.close()
    assert result.read_text() == "team/service:7:abc123:42:7:team/service"
    assert ticket.routed_event.session_key == "42:7"
    assert ticket.routed_event.workspace_key == "team/service"


def test_handoff_projects_legacy_github_environment_names(tmp_path: Path) -> None:
    event = Event(
        "github-delivery", "github-review", "pull_request", "github",
        context={"github": {"repo": "team/service", "prNumber": 11}},
    )
    result = tmp_path / "github-env"
    workspace = tmp_path / "workspace"
    cfg = SimpleNamespace(
        script='printf "%s:%s" "$ACH_EVENT_REPO" "$ACH_EVENT_PR_NUMBER" > "$RESULT"',
        env={"RESULT": str(result)}, secret_env={}, timeout_seconds=5,
        destination="handoff", scope="event",
    )
    routed = RoutedEvent(event, "team/service", "pr:11")
    loop = asyncio.get_event_loop_policy().new_event_loop()
    try:
        ticket = AdmissionTicket(
            "inv-github", "ach", "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77", "w" * 64, "s" * 64,
            routed, datetime.now(UTC) + timedelta(seconds=20), loop.create_future(), loop.create_future(),
        )
        loop.run_until_complete(run_handoff(
            cfg, event, ticket, workspace, environ={"PATH": "/bin:/usr/bin"},
        ))
    finally:
        loop.close()
    assert result.read_text() == "team/service:11"


def test_a2a_headers_never_become_event_env() -> None:
    event = Event(event_id="a2a:t", channel="chat", type="a2a", source="a2a",
                  context={"a2a": {"taskId": "t", "contextId": "c"},
                           "headers": {"x-litellm-user-id": "alice"}})
    assert not [k for k in _event_env(event) if "HEADERS" in k or "ALICE" in k.upper()]
    assert "alice" not in _event_env(event).values()
