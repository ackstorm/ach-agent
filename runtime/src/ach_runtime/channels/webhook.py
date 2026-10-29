# SPDX-License-Identifier: MIT
"""Webhook Event normalization, retaining the legacy generic delivery-ID priority."""

from typing import Any

from ach_runtime.channels.event import Event
from ach_runtime.channels.gitlab import normalize_gitlab
from ach_runtime.config.schema import ChannelConfig


class MissingWebhookDeliveryId(ValueError):
    """A generic webhook has no stable delivery ID in its supported headers."""


_GENERIC_ID_HEADERS = (
    "x-github-delivery",
    "x-gitlab-event-uuid",
    "svix-id",
    "x-request-id",
    "idempotency-key",
)


def _parse_github(payload: dict[str, Any]) -> tuple[str, int]:
    """Read the legacy GitHub repository and pull-request identity fields."""
    try:
        repository = payload["repository"]
        repo = repository["full_name"]
        raw_number = payload.get("number") or payload["pull_request"]["number"]
    except (KeyError, TypeError) as exc:
        raise ValueError("GitHub payload is missing repository or pull request identity") from exc
    if not isinstance(repo, str) or not repo.strip():
        raise ValueError("GitHub repository.full_name must be a non-empty string")
    if isinstance(raw_number, bool):
        raise ValueError("GitHub pull request number must be an integer")
    pr_number = int(raw_number)
    if pr_number <= 0:
        raise ValueError("GitHub pull request number must be positive")
    return repo, pr_number


def _header(headers: dict[str, str], name: str) -> str:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return ""


def normalize_webhook(
    payload: dict[str, Any], headers: dict[str, str], channel: ChannelConfig
) -> Event | None:
    """Normalize a supported webhook source without altering its delivery identity."""
    if channel.source == "gitlab":
        return normalize_gitlab(payload, headers, channel)
    if channel.source not in {"generic", "github"}:
        raise ValueError(f"unsupported webhook source: {channel.source!r}")

    event_id = ""
    for name in _GENERIC_ID_HEADERS:
        if event_id := _header(headers, name):
            break
    if not event_id:
        raise MissingWebhookDeliveryId("generic webhook requires a stable delivery ID")
    if channel.source == "github":
        repo, pr_number = _parse_github(payload)
        return Event(
            event_id=event_id,
            channel=channel.name,
            type="pull_request",
            source="github",
            payload=payload,
            context={"github": {"repo": repo, "prNumber": pr_number}},
        )
    return Event(
        event_id=event_id,
        channel=channel.name,
        type="webhook",
        source="generic",
        payload=payload,
        context={},
    )
