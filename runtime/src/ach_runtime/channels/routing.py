# SPDX-License-Identifier: MIT
"""route(event, channel) -> RoutedEvent — adapter defaults + optional overrides (contract §2).

Defaults (contract §2 table):
  gitlab MR & MR-notes   -> workspace "gitlab:<channel>:<projectId>", session "mr:<iid>"
  gitlab issue & notes   -> workspace "gitlab:<channel>:<projectId>", session "issue:<iid>"
  generic webhook        -> workspace "webhook:<channel>", session: routing.sessionKey (required)
  GitHub pull request    -> workspace "github:<channel>:<repo>", session "pr:<number>"
  Redis queue            -> workspace "queue:<channel>", session: routing.sessionKey (required)
  A2A                    -> workspace "a2a:<channel>", session the contextId (else taskId)
  cron                   -> workspace "cron:<channel>", session the original scheduled UTC tick

`routing.workspaceKey: session` is a reserved keyword: one workspace per session, keyed
JSON `[<default workspace>, <session key>]` (a session key is only unique inside its
workspace, e.g. `mr:7` exists in every project).

"<channel>" is the channel's configured name, used as the stable "configured source
namespace" the contract calls for — there is no separate server-identity field, and one is
deliberately not added for this (task-3 dispatch: "never extract server identity from
payload URL and don't add a new authoring field just for this").

`channels[].routing.workspaceKey`/`sessionKey` are independent, optional template
overrides rendered against `event.*`/`payload.*` (contract §2) via
`templating.render_routing_template` — a missing path or empty render REJECTS (raises),
it never falls back to the default.
"""

from __future__ import annotations

import json
from typing import Any

from ach_runtime.channels.event import Event, RoutedEvent
from ach_runtime.config.schema import ChannelConfig
from ach_runtime.templating.render import UnresolvedTemplateError, render_routing_template

SESSION_WORKSPACE = "session"  # routing.workspaceKey keyword: one workspace per session

_DOMAIN = {"webhook": "webhook", "queue": "queue", "a2a": "a2a", "cron": "cron"}


class RoutingError(ValueError):
    """A default could not be derived, or a configured routing override failed to render."""


def _routing_context(event: Event) -> dict[str, Any]:
    return {
        "event": {"id": event.event_id, "channel": event.channel, "type": event.type, **event.context},
        "payload": event.payload,
    }


def _default_workspace_key(event: Event, channel: ChannelConfig) -> str:
    if event.source == "gitlab":
        project_id = event.context.get("gitlab", {}).get("projectId")
        if project_id is None:
            raise RoutingError("gitlab event missing context.gitlab.projectId")
        return f"gitlab:{channel.name}:{project_id}"
    if event.source == "github":
        repo = event.context.get("github", {}).get("repo")
        if not isinstance(repo, str) or not repo.strip():
            raise RoutingError("github event missing context.github.repo")
        return f"github:{channel.name}:{repo}"
    return f"{_DOMAIN[channel.type]}:{channel.name}"


def _default_session_key(event: Event, channel: ChannelConfig) -> str:
    if event.source == "gitlab":
        gitlab_ctx = event.context.get("gitlab", {})
        if "mergeRequestIid" in gitlab_ctx:
            return f"mr:{gitlab_ctx['mergeRequestIid']}"
        if "issueIid" in gitlab_ctx:
            return f"issue:{gitlab_ctx['issueIid']}"
        raise RoutingError("gitlab event missing mergeRequestIid/issueIid")
    if event.source == "github":
        pr_number = event.context.get("github", {}).get("prNumber")
        if isinstance(pr_number, bool) or not isinstance(pr_number, int) or pr_number <= 0:
            raise RoutingError("github event missing valid context.github.prNumber")
        return f"pr:{pr_number}"
    if channel.type == "cron":
        tick = event.context.get("cron", {}).get("scheduledTick")
        if not tick:
            raise RoutingError("cron event missing context.cron.scheduledTick")
        return tick
    if channel.type == "a2a":
        task_id = event.context.get("a2a", {}).get("taskId")
        context_id = event.context.get("a2a", {}).get("contextId")
        return context_id or task_id or ""
    raise RoutingError(f"channel {channel.name!r}: routing.sessionKey is required")


def _render_override(template: str, context: dict[str, Any], field: str) -> str:
    try:
        rendered = render_routing_template(template, context)
    except UnresolvedTemplateError as exc:
        raise RoutingError(f"routing.{field}: {exc}") from exc
    if not rendered:
        raise RoutingError(f"routing.{field} rendered to an empty string")
    return rendered


def route(event: Event, channel: ChannelConfig) -> RoutedEvent:
    routing = channel.routing
    override_workspace = routing.workspace_key if routing is not None else None
    override_session = routing.session_key if routing is not None else None

    session_key = (
        _render_override(override_session, _routing_context(event), "sessionKey")
        if override_session is not None
        else _default_session_key(event, channel)
    )
    if override_workspace == SESSION_WORKSPACE:
        workspace_key = json.dumps(
            [_default_workspace_key(event, channel), session_key], separators=(",", ":")
        )
    elif override_workspace is not None:
        workspace_key = _render_override(override_workspace, _routing_context(event), "workspaceKey")
    else:
        workspace_key = _default_workspace_key(event, channel)

    return RoutedEvent(event=event, workspace_key=workspace_key, session_key=session_key)
