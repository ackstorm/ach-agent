# SPDX-License-Identifier: MIT
"""normalize_gitlab — GitLab webhook payload -> Event | None (contract §2).

Reuses old-ach-agent `channels/webhook.py` `_parse_gitlab`/`_gitlab_actor`/
`_project_path_fields` almost verbatim (session-key construction moves to
`channels.routing`, out of scope here). Two deliberate behavior changes vs. the old
code, both required by the new contract:

  1. `eventId` is REQUIRED from `X-Gitlab-Event-UUID` — the old ms-timestamp fallback
     is gone. Contract §2: "no se genera un identificador nuevo en cada redelivery."
  2. `webhook.mergeRequestsOnly` drops issue/issue-note events before they ever reach
     admission (old code had no such flag).

Returns `None` for an unsupported/ignored event (bot actor, system note, filtered actor,
mergeRequestsOnly-excluded issue, non-routable note/kind). Raises on a routable-but-
malformed payload (missing/bool-typed id) — the HTTP-layer caller (Task 10) maps that to
422; a bot is not asked to guess a default per contract §2 "no missing fallback".
"""

from __future__ import annotations

from typing import Any

from ach_runtime.channels.event import Event
from ach_runtime.config.schema import CONVERSATIONAL_GITLAB_EVENTS, ChannelConfig


class MalformedGitlabEventError(ValueError):
    """A supported GitLab event kind carried a missing/malformed required field."""


def _gitlab_actor(body: dict[str, Any]) -> str:
    """The GitLab username that authored the hook (empty string if absent)."""
    return str((body.get("user") or {}).get("username", "") or body.get("user_username", ""))


def _project_path_fields(body: dict[str, Any]) -> dict[str, str]:
    path = str(
        (body.get("project") or {}).get("path_with_namespace", "")
        or body.get("path_with_namespace", "")
        or ""
    )
    return {"projectPath": path} if path else {}


def _mr_head_sha(container: dict[str, Any]) -> str:
    """Return the MR source head SHA when the legacy payload includes it."""
    last_commit = container.get("last_commit")
    return str(last_commit.get("id", "")) if isinstance(last_commit, dict) else ""


def _get(mapping: Any, key: str, field: str) -> Any:
    """Required-field lookup — raises `MalformedGitlabEventError`, never a bare KeyError."""
    try:
        return mapping[key]
    except (KeyError, TypeError) as exc:
        raise MalformedGitlabEventError(f"{field}: missing or malformed") from exc


def _require_id(value: Any, field: str) -> str:
    """Reject a missing id AND a bool (bool is a subclass of int — `int(True) == 1`)."""
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise MalformedGitlabEventError(f"{field}: expected an integer id, got {value!r}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise MalformedGitlabEventError(f"{field}: not a valid integer id: {value!r}") from exc
    return str(parsed)


def _event_id(headers: dict[str, str]) -> str:
    event_id = headers.get("X-Gitlab-Event-UUID") or headers.get("x-gitlab-event-uuid")
    if not event_id:
        raise MalformedGitlabEventError("missing X-Gitlab-Event-UUID delivery header")
    return event_id


def _build(
    payload: dict[str, Any],
    headers: dict[str, str],
    channel: ChannelConfig,
    *,
    type_: str,
    gitlab_context: dict[str, Any],
) -> Event | None:
    wh = channel.webhook
    assert wh is not None
    actor = _gitlab_actor(payload)
    if wh.bot_username and actor == wh.bot_username:
        return None  # self-authored — loop guard
    if wh.trigger_users is not None and actor not in wh.trigger_users:
        return None  # actor not in the allowlist
    return Event(
        event_id=_event_id(headers),
        channel=channel.name,
        type=type_,
        source="gitlab",
        payload=payload,
        context={"gitlab": gitlab_context},
        actor=actor,
    )


def normalize_gitlab(
    payload: dict[str, Any], headers: dict[str, str], channel: ChannelConfig
) -> Event | None:
    wh = channel.webhook
    assert wh is not None, "normalize_gitlab requires a webhook-configured channel"
    allowed = set(wh.gitlab_events) if wh.gitlab_events else set(CONVERSATIONAL_GITLAB_EVENTS)
    kind = payload.get("object_kind") or payload.get("event_name") or ""

    if kind == "merge_request" and "merge_request" in allowed:
        project_id = _require_id(_get(payload.get("project") or {}, "id", "project.id"), "project.id")
        attrs = payload.get("object_attributes") or {}
        mr_iid = _require_id(_get(attrs, "iid", "object_attributes.iid"), "object_attributes.iid")
        ctx = {"projectId": project_id, "mergeRequestIid": mr_iid, **_project_path_fields(payload)}
        if head_sha := _mr_head_sha(attrs):
            ctx["headSha"] = head_sha
        return _build(payload, headers, channel, type_="merge_request", gitlab_context=ctx)

    if kind == "issue" and "issue" in allowed:
        if wh.merge_requests_only:
            return None
        project_id = _require_id(_get(payload.get("project") or {}, "id", "project.id"), "project.id")
        issue_iid = _require_id(_get(payload.get("object_attributes") or {}, "iid", "object_attributes.iid"), "object_attributes.iid")
        ctx = {"projectId": project_id, "issueIid": issue_iid, **_project_path_fields(payload)}
        return _build(payload, headers, channel, type_="issue", gitlab_context=ctx)

    if kind == "note" and "note" in allowed:
        attrs = payload.get("object_attributes") or {}
        if attrs.get("system"):
            return None  # GitLab-generated system note (label/assignee change)
        noteable = str(attrs.get("noteable_type", "")).lower()
        if noteable == "mergerequest" and "merge_request" in allowed:
            project_id = _require_id(_get(payload.get("project") or {}, "id", "project.id"), "project.id")
            merge_request = payload.get("merge_request") or {}
            mr_iid = _require_id(_get(merge_request, "iid", "merge_request.iid"), "merge_request.iid")
            ctx = {"projectId": project_id, "mergeRequestIid": mr_iid, **_project_path_fields(payload)}
            if head_sha := _mr_head_sha(merge_request):
                ctx["headSha"] = head_sha
            return _build(payload, headers, channel, type_="note", gitlab_context=ctx)
        if noteable == "issue" and "issue" in allowed:
            if wh.merge_requests_only:
                return None
            project_id = _require_id(_get(payload.get("project") or {}, "id", "project.id"), "project.id")
            issue_iid = _require_id(_get(payload.get("issue") or {}, "iid", "issue.iid"), "issue.iid")
            ctx = {"projectId": project_id, "issueIid": issue_iid, **_project_path_fields(payload)}
            return _build(payload, headers, channel, type_="note", gitlab_context=ctx)
        return None  # comment on commit/snippet, or noteable kind not routed

    if (
        channel.type == "webhook-script"
        and wh.gitlab_events is not None
        and kind in allowed
    ):
        # Legacy deterministic handlers also receive configured project/system kinds such
        # as push and project_*; conversational normalizers above retain their exact rules.
        project = payload.get("project") or {}
        raw_project_id = project.get("id") or payload.get("project_id")
        project_id = _require_id(raw_project_id, "project.id or project_id")
        ctx = {"projectId": project_id, **_project_path_fields(payload)}
        return _build(payload, headers, channel, type_=str(kind), gitlab_context=ctx)

    return None  # unsupported kind (pipeline, emoji, tag_push, ...) — explicitly ignored
