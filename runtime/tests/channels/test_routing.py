# SPDX-License-Identifier: MIT
"""route(event, channel) -> RoutedEvent — adapter defaults + independent overrides (contract §2)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_runtime.channels.event import Event
from ach_runtime.channels.routing import RoutingError, route
from ach_runtime.config.schema import ChannelConfig

MR_EVENT = Event(
    event_id="evt-1",
    channel="gitlab-mr-review",
    type="merge_request",
    source="gitlab",
    payload={"object_kind": "merge_request"},
    context={"gitlab": {"projectId": "42", "mergeRequestIid": "7", "projectPath": "team/service"}},
)

NOTE_ON_MR_EVENT = Event(
    event_id="evt-2",
    channel="gitlab-mr-review",
    type="note",
    source="gitlab",
    payload={},
    context={"gitlab": {"projectId": "42", "mergeRequestIid": "7"}},
)

ISSUE_EVENT = Event(
    event_id="evt-3",
    channel="gitlab-mr-review",
    type="issue",
    source="gitlab",
    payload={},
    context={"gitlab": {"projectId": "42", "issueIid": "3"}},
)


def _gitlab_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "gitlab-mr-review",
        "type": "webhook",
        "source": "gitlab",
        "webhook": {"auth": {"type": "none"}},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


def _generic_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "generic-hook",
        "type": "webhook",
        "source": "generic",
        "webhook": {"auth": {"type": "none"}},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


def _cron_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "nightly",
        "type": "cron",
        "cron": {"schedule": "0 0 * * *"},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


def _a2a_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "chat",
        "type": "a2a",
        "a2a": {"auth": {"env": "A2A_KEY"}},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


def _queue_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "redis-jobs",
        "type": "queue",
        "queue": {"type": "redis", "key": "ach:jobs"},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


def _github_channel(routing: dict[str, object] | None = None) -> ChannelConfig:
    cfg: dict[str, object] = {
        "name": "github-pr",
        "type": "webhook",
        "source": "github",
        "webhook": {"auth": {"type": "none"}},
    }
    if routing is not None:
        cfg["routing"] = routing
    return ChannelConfig.model_validate(cfg)


# ---------------------------------------------------------------------------
# defaults — MR and its notes share a session; issue and its notes share a session
# ---------------------------------------------------------------------------


def test_mr_default_workspace_and_session() -> None:
    routed = route(MR_EVENT, _gitlab_channel())
    assert routed.workspace_key == "gitlab:gitlab-mr-review:42"
    assert routed.session_key == "mr:7"


def test_note_on_mr_shares_mr_session() -> None:
    routed = route(NOTE_ON_MR_EVENT, _gitlab_channel())
    assert routed.session_key == "mr:7"
    assert routed.workspace_key == route(MR_EVENT, _gitlab_channel()).workspace_key


def test_issue_default_session_namespaced_separately_from_mr() -> None:
    routed = route(ISSUE_EVENT, _gitlab_channel())
    assert routed.session_key == "issue:3"


def test_different_project_does_not_collide() -> None:
    other = Event(
        event_id="evt-9",
        channel="gitlab-mr-review",
        type="merge_request",
        source="gitlab",
        payload={},
        context={"gitlab": {"projectId": "99", "mergeRequestIid": "7"}},
    )
    r1 = route(MR_EVENT, _gitlab_channel())
    r2 = route(other, _gitlab_channel())
    assert r1.workspace_key != r2.workspace_key
    assert r1.session_key == r2.session_key  # same iid, different project


def test_different_channel_does_not_collide() -> None:
    chan_a = _gitlab_channel()
    chan_b = ChannelConfig.model_validate(
        {
            "name": "gitlab-other-server",
            "type": "webhook",
            "source": "gitlab",
            "webhook": {"auth": {"type": "none"}},
        }
    )
    r1 = route(MR_EVENT, chan_a)
    r2 = route(MR_EVENT, chan_b)
    assert r1.workspace_key != r2.workspace_key


def test_generic_webhook_shares_one_workspace_per_channel() -> None:
    event = Event(event_id="d1", channel="generic-hook", type="webhook", source="generic",
                  payload={"ticket": 9})
    routed = route(event, _generic_channel({"sessionKey": "ticket:{{ payload.ticket }}"}))
    assert (routed.workspace_key, routed.session_key) == ("webhook:generic-hook", "ticket:9")


def test_session_workspace_is_default_workspace_plus_session() -> None:
    a = route(MR_EVENT, _gitlab_channel({"workspaceKey": "session"}))
    other_project = Event(event_id="e", channel="gitlab-mr-review", type="merge_request",
                          source="gitlab", context={"gitlab": {"projectId": "43", "mergeRequestIid": "7"}})
    b = route(other_project, _gitlab_channel({"workspaceKey": "session"}))
    assert a.session_key == b.session_key == "mr:7"
    assert a.workspace_key == '["gitlab:gitlab-mr-review:42","mr:7"]'
    assert a.workspace_key != b.workspace_key


@pytest.mark.parametrize("channel", [_generic_channel, _queue_channel])
def test_session_key_required_without_standard_identity(channel) -> None:
    with pytest.raises(ValidationError, match="routing.sessionKey is required"):
        channel()  # no routing block


def test_cron_default_uses_scheduled_tick_as_session() -> None:
    event = Event(
        event_id="nightly:2026-10-01T00:00:00",
        channel="nightly",
        type="cron",
        source="cron",
        context={"cron": {"scheduledTick": "2026-10-01T00:00:00+00:00"}},
    )
    routed = route(event, _cron_channel())
    assert routed.workspace_key == "cron:nightly"
    assert routed.session_key == "2026-10-01T00:00:00+00:00"


def test_cron_default_rejects_missing_scheduled_tick() -> None:
    event = Event(event_id="x", channel="nightly", type="cron", source="cron")
    with pytest.raises(RoutingError):
        route(event, _cron_channel())


def test_queue_and_github_defaults_use_legacy_lanes() -> None:
    queue_event = Event(
        event_id="1700000000000-0", channel="redis-jobs", type="queue", source="queue",
        payload={"task": "run"},
    )
    github_event = Event(
        event_id="delivery-1", channel="github-pr", type="pull_request", source="github",
        payload={"action": "opened"},
        context={"github": {"repo": "ackstorm/ach-runtime", "prNumber": 17}},
    )

    queued = route(queue_event, _queue_channel({"sessionKey": "main"}))
    github = route(github_event, _github_channel())

    assert (queued.workspace_key, queued.session_key) == ("queue:redis-jobs", "main")
    assert (github.workspace_key, github.session_key) == (
        "github:github-pr:ackstorm/ach-runtime", "pr:17"
    )


# ---------------------------------------------------------------------------
# routing overrides — optional, independent, reject on missing/empty, no fallback
# ---------------------------------------------------------------------------


def test_workspace_override_independent_of_session_default() -> None:
    routed = route(MR_EVENT, _gitlab_channel({"workspaceKey": "custom:{{ event.gitlab.projectId }}"}))
    assert routed.workspace_key == "custom:42"
    assert routed.session_key == "mr:7"  # untouched default


def test_session_override_independent_of_workspace_default() -> None:
    routed = route(MR_EVENT, _gitlab_channel({"sessionKey": "fixed-session"}))
    assert routed.workspace_key == "gitlab:gitlab-mr-review:42"
    assert routed.session_key == "fixed-session"


def test_both_overrides_applied() -> None:
    routed = route(
        MR_EVENT,
        _gitlab_channel({"workspaceKey": "ws-{{ event.gitlab.projectId }}", "sessionKey": "s-{{ event.gitlab.mergeRequestIid }}"}),
    )
    assert routed.workspace_key == "ws-42"
    assert routed.session_key == "s-7"


def test_override_missing_path_rejects_even_with_nonempty_prefix() -> None:
    """Old prompt-template fallback (missing -> "") is NOT valid for routing."""
    routed_channel = _gitlab_channel({"workspaceKey": "prefix-{{ event.gitlab.missingField }}"})
    with pytest.raises(RoutingError):
        route(MR_EVENT, routed_channel)


def test_override_using_payload_namespace() -> None:
    event = Event(
        event_id="evt-7",
        channel="generic-hook",
        type="webhook",
        source="webhook",
        payload={"room": "ops"},
    )
    routed = route(event, _generic_channel({"workspaceKey": "room-{{ payload.room }}", "sessionKey": "main"}))
    assert routed.workspace_key == "room-ops"


# ---------------------------------------------------------------------------
# invalid template syntax must reject, never silently become literal text
# (task-3-admission-review.md finding 7)
# ---------------------------------------------------------------------------


def test_override_with_filter_syntax_rejected_not_passed_through_literally() -> None:
    """`{{ payload.foo | missing }}` matches no accepted grammar — it must fail,
    not be left in the rendered output as literal text."""
    channel = _generic_channel({"workspaceKey": "prefix-{{ payload.foo | missing }}", "sessionKey": "main"})
    event = Event(event_id="evt-1", channel="generic-hook", type="webhook", source="webhook")
    with pytest.raises(RoutingError):
        route(event, channel)


def test_override_with_unbalanced_braces_rejected() -> None:
    channel = _generic_channel({"workspaceKey": "prefix-{{ event.id", "sessionKey": "main"})
    event = Event(event_id="evt-1", channel="generic-hook", type="webhook", source="webhook")
    with pytest.raises(RoutingError):
        route(event, channel)


def test_override_with_empty_expression_rejected() -> None:
    channel = _generic_channel({"workspaceKey": "prefix-{{  }}", "sessionKey": "main"})
    event = Event(event_id="evt-1", channel="generic-hook", type="webhook", source="webhook")
    with pytest.raises(RoutingError):
        route(event, channel)


def test_override_with_reversed_stray_delimiters_rejected() -> None:
    """task-3-admission-fix-1-review.md I7: equal `{{`/`}}` counts in the WRONG order
    (close before open) must still reject — a regex over complete matches alone finds
    zero matches here and would otherwise pass the whole string through as literal text."""
    channel = _generic_channel({"workspaceKey": "prefix-}} stray {{ event.id", "sessionKey": "main"})
    event = Event(event_id="evt-1", channel="generic-hook", type="webhook", source="webhook")
    with pytest.raises(RoutingError):
        route(event, channel)


def test_a2a_session_defaults_to_context_then_task() -> None:
    channel = _a2a_channel()
    with_context = Event(event_id="a2a:t1", channel="chat", type="a2a", source="a2a",
                         context={"a2a": {"taskId": "t1", "contextId": "c1"}})
    without_context = Event(event_id="a2a:t2", channel="chat", type="a2a", source="a2a",
                            context={"a2a": {"taskId": "t2", "contextId": ""}})
    assert route(with_context, channel).session_key == "c1"
    assert route(without_context, channel).session_key == "t2"
    assert route(with_context, channel).workspace_key == "a2a:chat"


def test_header_template_routes_and_missing_header_rejects() -> None:
    channel = _a2a_channel({"sessionKey": "{{ event.headers.x-litellm-user-id }}:{{ event.a2a.contextId }}"})
    event = Event(event_id="a2a:t", channel="chat", type="a2a", source="a2a",
                  context={"a2a": {"taskId": "t", "contextId": "c"},
                           "headers": {"x-litellm-user-id": "alice"}})
    assert route(event, channel).session_key == "alice:c"
    bare = Event(event_id="a2a:t", channel="chat", type="a2a", source="a2a",
                 context={"a2a": {"taskId": "t", "contextId": "c"}, "headers": {}})
    with pytest.raises(RoutingError):
        route(bare, channel)
