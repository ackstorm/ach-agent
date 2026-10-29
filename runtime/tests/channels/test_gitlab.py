# SPDX-License-Identifier: MIT
"""normalize_gitlab — GitLab payload -> Event | None (contract §2, task-3 dispatch)."""

from __future__ import annotations

import pytest

from ach_runtime.channels.gitlab import MalformedGitlabEventError, normalize_gitlab
from ach_runtime.config.schema import ChannelConfig

EVENT_UUID = "550e8400-e29b-41d4-a716-446655440000"


def _channel(**webhook_overrides: object) -> ChannelConfig:
    webhook = {
        "auth": {"type": "none"},
        "gitlabEvents": ["merge_request", "issue", "note"],
        **webhook_overrides,
    }
    return ChannelConfig.model_validate(
        {"name": "gitlab-mr-review", "type": "webhook", "source": "gitlab", "webhook": webhook}
    )


def _script_channel(events: list[str]) -> ChannelConfig:
    return ChannelConfig.model_validate({
        "name": "gitlab-register-hook",
        "type": "webhook-script",
        "source": "gitlab",
        "webhook": {"auth": {"type": "none"}, "gitlabEvents": events},
        "script": {"script": ":"},
    })


def _headers(event_uuid: str | None = EVENT_UUID) -> dict[str, str]:
    h = {"X-Gitlab-Token": "x"}
    if event_uuid is not None:
        h["X-Gitlab-Event-UUID"] = event_uuid
    return h


MR_PAYLOAD = {
    "object_kind": "merge_request",
    "project": {"id": 42, "path_with_namespace": "team/service"},
    "object_attributes": {"iid": 7, "state": "opened"},
    "user": {"username": "alice"},
}

ISSUE_PAYLOAD = {
    "object_kind": "issue",
    "project": {"id": 42, "path_with_namespace": "team/service"},
    "object_attributes": {"iid": 3},
    "user": {"username": "alice"},
}

NOTE_ON_MR_PAYLOAD = {
    "object_kind": "note",
    "project": {"id": 42, "path_with_namespace": "team/service"},
    "object_attributes": {"iid": 99, "noteable_type": "MergeRequest", "note": "lgtm"},
    "merge_request": {"iid": 7},
    "user": {"username": "alice"},
}

NOTE_ON_ISSUE_PAYLOAD = {
    "object_kind": "note",
    "project": {"id": 42, "path_with_namespace": "team/service"},
    "object_attributes": {"iid": 99, "noteable_type": "Issue", "note": "lgtm"},
    "issue": {"iid": 3},
    "user": {"username": "alice"},
}


# ---------------------------------------------------------------------------
# Parent MR / note-on-MR share identity — parent issue / note-on-issue share identity
# ---------------------------------------------------------------------------


def test_merge_request_normalizes() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _channel())
    assert event is not None
    assert event.type == "merge_request"
    assert event.context["gitlab"]["projectId"] == "42"
    assert event.context["gitlab"]["mergeRequestIid"] == "7"
    assert event.context["gitlab"]["projectPath"] == "team/service"
    assert event.event_id == EVENT_UUID
    assert event.source == "gitlab"
    assert event.actor == "alice"


def test_merge_request_includes_legacy_head_sha() -> None:
    payload = {
        **MR_PAYLOAD,
        "object_attributes": {**MR_PAYLOAD["object_attributes"], "last_commit": {"id": "abc123"}},
    }
    event = normalize_gitlab(payload, _headers(), _channel())
    assert event is not None
    assert event.context["gitlab"]["headSha"] == "abc123"


def test_note_on_mr_shares_mr_identity_not_note_id() -> None:
    """Use parent identity (merge_request.iid), never the note's own object_attributes.iid."""
    event = normalize_gitlab(NOTE_ON_MR_PAYLOAD, _headers(), _channel())
    assert event is not None
    assert event.type == "note"
    assert event.context["gitlab"]["mergeRequestIid"] == "7"
    assert "issueIid" not in event.context["gitlab"]


def test_note_on_mr_includes_legacy_head_sha() -> None:
    payload = {**NOTE_ON_MR_PAYLOAD, "merge_request": {"iid": 7, "last_commit": {"id": "def456"}}}
    event = normalize_gitlab(payload, _headers(), _channel())
    assert event is not None
    assert event.context["gitlab"]["headSha"] == "def456"


def test_issue_normalizes() -> None:
    event = normalize_gitlab(ISSUE_PAYLOAD, _headers(), _channel())
    assert event is not None
    assert event.type == "issue"
    assert event.context["gitlab"]["issueIid"] == "3"


def test_note_on_issue_shares_issue_identity() -> None:
    event = normalize_gitlab(NOTE_ON_ISSUE_PAYLOAD, _headers(), _channel())
    assert event is not None
    assert event.context["gitlab"]["issueIid"] == "3"
    assert "mergeRequestIid" not in event.context["gitlab"]


# ---------------------------------------------------------------------------
# mergeRequestsOnly / bot / triggerUsers filters — before admission
# ---------------------------------------------------------------------------


def test_merge_requests_only_drops_issue() -> None:
    event = normalize_gitlab(ISSUE_PAYLOAD, _headers(), _channel(mergeRequestsOnly=True))
    assert event is None


def test_merge_requests_only_drops_note_on_issue() -> None:
    event = normalize_gitlab(NOTE_ON_ISSUE_PAYLOAD, _headers(), _channel(mergeRequestsOnly=True))
    assert event is None


def test_merge_requests_only_keeps_mr() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _channel(mergeRequestsOnly=True))
    assert event is not None


def test_bot_actor_ignored() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _channel(botUsername="alice"))
    assert event is None


def test_trigger_users_filters_non_allowlisted_actor() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _channel(triggerUsers=["bob"]))
    assert event is None


def test_trigger_users_allows_listed_actor() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _channel(triggerUsers=["alice"]))
    assert event is not None


def test_system_note_ignored() -> None:
    payload = {
        **NOTE_ON_MR_PAYLOAD,
        "object_attributes": {**NOTE_ON_MR_PAYLOAD["object_attributes"], "system": True},
    }
    assert normalize_gitlab(payload, _headers(), _channel()) is None


def test_note_on_commit_ignored() -> None:
    payload = {
        "object_kind": "note",
        "project": {"id": 42},
        "object_attributes": {"iid": 1, "noteable_type": "Commit"},
    }
    assert normalize_gitlab(payload, _headers(), _channel()) is None


def test_unsupported_kind_ignored() -> None:
    payload = {"object_kind": "pipeline", "project": {"id": 42}}
    assert normalize_gitlab(payload, _headers(), _channel()) is None


def test_kind_not_in_allowed_ignored() -> None:
    event = normalize_gitlab(ISSUE_PAYLOAD, _headers(), _channel(gitlabEvents=["merge_request"]))
    assert event is None


# ---------------------------------------------------------------------------
# malformed/missing fields -> raise (never a silent fallback)
# ---------------------------------------------------------------------------


def test_missing_project_id_rejected() -> None:
    payload = {"object_kind": "merge_request", "project": {}, "object_attributes": {"iid": 7}}
    with pytest.raises(MalformedGitlabEventError):
        normalize_gitlab(payload, _headers(), _channel())


def test_bool_id_rejected_not_treated_as_integer() -> None:
    """bool is a subclass of int — int(True) == 1 must NOT silently pass as a valid id."""
    payload = {
        "object_kind": "merge_request",
        "project": {"id": True},
        "object_attributes": {"iid": 7},
    }
    with pytest.raises(MalformedGitlabEventError):
        normalize_gitlab(payload, _headers(), _channel())


def test_missing_event_uuid_header_rejected_no_timestamp_fallback() -> None:
    """New contract: no ms-timestamp fallback — a missing delivery header is malformed."""
    with pytest.raises(MalformedGitlabEventError):
        normalize_gitlab(MR_PAYLOAD, _headers(event_uuid=None), _channel())


def test_webhook_script_normalizes_legacy_project_system_event() -> None:
    payload = {
        "object_kind": "push",
        "project_id": 42,
        "project": {"id": 42, "path_with_namespace": "team/service"},
        "user_username": "alice",
    }
    event = normalize_gitlab(payload, _headers(), _script_channel(["push"]))
    assert event is not None
    assert event.type == "push"
    assert event.context["gitlab"] == {"projectId": "42", "projectPath": "team/service"}


def test_webhook_script_preserves_conversational_mr_and_actor_gates() -> None:
    event = normalize_gitlab(MR_PAYLOAD, _headers(), _script_channel(["merge_request"]))
    assert event is not None
    assert event.context["gitlab"]["mergeRequestIid"] == "7"
    assert normalize_gitlab(
        MR_PAYLOAD, _headers(), ChannelConfig.model_validate({
            "name": "gitlab-register-hook", "type": "webhook-script", "source": "gitlab",
            "webhook": {"auth": {"type": "none"}, "gitlabEvents": ["merge_request"], "botUsername": "alice"},
            "script": {"script": ":"},
        })
    ) is None
