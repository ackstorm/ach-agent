# SPDX-License-Identifier: Apache-2.0
"""Webhook channel adapter — GitLab MR inbound (CHN-01, IDM-01, D-05, SEC-02, D-07).

Locked decisions:
  - Auth: GitLab plain-token compare via hmac.compare_digest (NOT HMAC-SHA256 body sig).
    See RESEARCH.md Pitfall 1: GitLab sends X-Gitlab-Token as a plain secret string.
  - Secret: read per-request from auth.secret (SecretSource: env) via resolve_secret();
    NEVER stored as an instance attribute or long-lived variable (SEC-02, Pitfall 2).
  - delivery_context: {project_id, mr_iid} extracted at parse time and threaded through
    MessageEvent.delivery_context (D-07).
  - Idempotency key: derive_webhook_idempotency_key(headers) — X-Gitlab-Event-UUID or
    ms-timestamp fallback (IDM-01, Pitfall 7: always send unique UUID per test POST).
  - Status map: ACCEPTED→202, DUPLICATE→200, FULL_QUEUE→503 (D-05).
  - source_trait: "sync" (webhook caller can handle 503 / retry).
  - Webhook events are always async — 202 accept-and-process; no reply-hold path.

RTR-06: NEVER import from hermes_agent.* here.

Boot-order: imported after configure_logging().
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import SecretSource, resolve_secret
from ach_agent.router.dedup import derive_gitlab_composite_key, derive_webhook_idempotency_key
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig, WebhookAuthBlock

log = structlog.get_logger(__name__)

# GitLab event kinds routed when a channel does not set webhook.gitlabEvents.
_DEFAULT_GITLAB_EVENTS = {"merge_request", "issue", "note"}


@dataclass
class WebhookResult:
    """Outcome of a webhook request — carries HTTP status + JSON body."""

    status_code: int
    body: dict[str, Any]
    # Correlation id (uuid4 hex) echoed on 202 accept — log/trace correlation ONLY, not
    # persisted/queryable. Empty for every non-202 outcome.
    task_id: str = ""


def _verify_hmac(signature: str, secret: SecretSource, raw_body: bytes) -> bool:
    """Verify GitHub-style HMAC-SHA256 body signature (X-Hub-Signature-256).

    The header value is `sha256=<hexdigest>`; we strip the prefix and compare in
    constant time against HMAC-SHA256(secret, raw_body). Secret resolved per-request
    via resolve_secret (SEC-02), never cached.
    """
    if not signature:
        return False
    sig = signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    # SEC-02: resolve per-request, discard after use — NEVER assigned to long-lived attr
    resolved = resolve_secret(secret)
    if not resolved:
        return False
    secret_bytes = resolved.encode("utf-8")
    expected = hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _verify_header_token(header_token: str, secret: SecretSource) -> bool:
    """Constant-time compare a static shared secret carried in a configurable header.

    Secret resolved per-call via resolve_secret for rotation support (SEC-02). Empty
    header or empty/unresolvable secret → reject.
    """
    resolved = resolve_secret(secret)
    if not header_token or not resolved:
        return False
    return hmac.compare_digest(header_token, resolved)


def _verify_auth(auth: WebhookAuthBlock, lower_headers: dict[str, str], raw_body: bytes) -> bool:
    """Dispatch auth verification by auth.type (gitlab_token | hmac | header_token | none)."""
    if auth.type == "none":
        return True
    # Schema requires auth.secret for every non-"none" type (WebhookAuthBlock validator).
    assert auth.secret is not None, "webhook.auth.secret must be set for non-none auth types"
    match auth.type:
        case "gitlab_token":
            return _verify_header_token(lower_headers.get("x-gitlab-token", ""), auth.secret)
        case "hmac":
            return _verify_hmac(lower_headers.get("x-hub-signature-256", ""), auth.secret, raw_body)
        case "header_token":
            return _verify_header_token(lower_headers.get(auth.header.lower(), ""), auth.secret)


def _gitlab_actor(body: dict[str, Any]) -> str:
    """The GitLab username that authored the hook (empty string if absent).

    Same extraction as derive_gitlab_composite_key (dedup.py): user.username with a
    user_username fallback. Used by the loop-guard + actor-allowlist gates.
    """
    return str((body.get("user") or {}).get("username", "") or body.get("user_username", ""))


def _parse_gitlab(body: dict[str, Any], allowed: set[str]) -> tuple[dict[str, Any], str] | None:
    """Route a GitLab hook, accept-ignore (None), or raise on a routable-but-malformed payload.

    Returns (delivery_context, session_key) when the event's kind is in `allowed` and a
    per-conversation session_key can be derived; None to accept-and-ignore (HTTP 200); raises
    KeyError/TypeError/ValueError only for a routable kind with a malformed payload (→ 422).

    session_key scheme (collision-safe, MR back-compat):
      MR hook & MR-comment    → f"{project_id}:{mr_iid}"        (UNCHANGED — shared lane)
      Issue hook & issue-comment → f"{project_id}:issue:{issue_iid}"  (namespaced)
    """
    kind = body.get("object_kind", "")

    if kind == "merge_request" and "merge_request" in allowed:
        project_id = int(body["project"]["id"])
        mr_iid = int(body["object_attributes"]["iid"])
        return (
            {
                "project_id": project_id,
                "kind": "merge_request",
                "target_type": "mr",
                "mr_iid": mr_iid,
            },
            f"{project_id}:{mr_iid}",
        )

    if kind == "issue" and "issue" in allowed:
        project_id = int(body["project"]["id"])
        issue_iid = int(body["object_attributes"]["iid"])
        return (
            {
                "project_id": project_id,
                "kind": "issue",
                "target_type": "issue",
                "issue_iid": issue_iid,
            },
            f"{project_id}:issue:{issue_iid}",
        )

    if kind == "note" and "note" in allowed:
        attrs = body.get("object_attributes", {}) or {}
        if attrs.get("system"):
            return None  # gitlab-generated system note (label/assignee change) → ignore
        noteable = str(attrs.get("noteable_type", "")).lower()
        # project_id is read INSIDE the routable branches only — a non-routable note
        # (commit/snippet, or a base kind not allowed) must accept-and-ignore (200), so it
        # must not raise on a malformed project block before reaching the `return None`.
        if noteable == "mergerequest" and "merge_request" in allowed:
            project_id = int(body["project"]["id"])
            mr_iid = int(body["merge_request"]["iid"])
            return (
                {"project_id": project_id, "kind": "note", "target_type": "mr", "mr_iid": mr_iid},
                f"{project_id}:{mr_iid}",
            )
        if noteable == "issue" and "issue" in allowed:
            project_id = int(body["project"]["id"])
            issue_iid = int(body["issue"]["iid"])
            return (
                {
                    "project_id": project_id,
                    "kind": "note",
                    "target_type": "issue",
                    "issue_iid": issue_iid,
                },
                f"{project_id}:issue:{issue_iid}",
            )
        return None  # comment on commit/snippet, or noteable kind not allowed → ignore

    return None  # kind not allowed / not routable (push, pipeline, emoji, …) → ignore


def _parse_github(body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Parse a GitHub pull_request webhook payload.

      body.repository.full_name                          → repo
      body.number (fallback body.pull_request.number)    → pr_number

    Returns (delivery_context, session_key). Raises KeyError/TypeError/ValueError
    on missing/non-castable fields (mapped to 422 by the caller).
    """
    repo = body["repository"]["full_name"]
    pr_number = int(body.get("number") or body["pull_request"]["number"])
    return {"repo": repo, "pr_number": pr_number}, f"{repo}:{pr_number}"


def _parse_generic(idempotency_key: str) -> tuple[dict[str, Any], str]:
    """Parse a generic webhook payload — no required fields, never 422s.

    delivery_context is empty; session_key is the idempotency key.
    """
    return {}, idempotency_key


def _status_map(result: RouterAdmitResult, task_id: str) -> WebhookResult:
    """Map RouterAdmitResult to D-05 HTTP status + JSON body.

    task_id is echoed in the body (and threaded onto WebhookResult.task_id for the
    X-ACH-Task-Id header) ONLY on the ACCEPTED (202) branch — DUPLICATE/FULL_QUEUE
    bodies are unchanged.
    """
    match result:
        case RouterAdmitResult.ACCEPTED:
            return WebhookResult(
                status_code=202,
                body={"status": "accepted", "task_id": task_id},
                task_id=task_id,
            )
        case RouterAdmitResult.DUPLICATE:
            return WebhookResult(status_code=200, body={"status": "duplicate"})
        case RouterAdmitResult.FULL_QUEUE:
            return WebhookResult(status_code=503, body={"status": "full", "retry": True})


async def handle_webhook_request(
    raw_body: bytes,
    headers: dict[str, str],
    channel_cfg: ChannelConfig,
    handler: MessageHandler,
) -> WebhookResult:
    """Handle a webhook request end-to-end — always async (202 accept-and-process).

    Stages (ORDER IS NORMATIVE):
      1. Auth: verify token/HMAC via constant-time compare (SEC-02)
         — invalid/absent → 401 WebhookResult, handler.handle() NOT called
      2. Parse JSON body, extract delivery_context (D-07)
      3. Derive idempotency key from headers (IDM-01, D-06)
      4. Build MessageEvent (source_trait="sync")
      5. Dispatch to handler.handle(event) → map to D-05 status

    Args:
        raw_body:    Raw request body bytes (read by caller before JSON parse).
        headers:     Request headers dict (case-sensitive; caller normalises keys).
        channel_cfg: Channel configuration carrying webhook.auth.secret.
        handler:     MessageHandler (Router) to dispatch to.

    Returns:
        WebhookResult with status_code and body dict.
    """
    assert channel_cfg.webhook is not None, "webhook block required on channel config"
    auth = channel_cfg.webhook.auth
    # None source defaults to gitlab for back-compat (schema requires source for webhooks).
    source = channel_cfg.source or "gitlab"

    # Normalise headers to lowercase — ASGI spec (PEP 3333) mandates lowercase header
    # names; FastAPI/Starlette preserve this. The unit tests pass mixed-case dicts, so
    # we build a lowercase lookup once and use it throughout this function.
    lower_headers: dict[str, str] = {k.lower(): v for k, v in headers.items()}

    # 1. AUTH — 401 before any router call (T-02-02; CHN-01; SEC-02)
    if not _verify_auth(auth, lower_headers, raw_body):
        log.warning(
            "webhook: auth verification failed — rejected",
            channel=channel_cfg.name,
            auth_type=auth.type,
        )
        return WebhookResult(status_code=401, body={"detail": "Invalid signature"})

    # 2. PARSE JSON
    try:
        body: dict[str, Any] = json.loads(raw_body)
    except (json.JSONDecodeError, ValueError) as exc:
        log.warning("webhook: invalid JSON body", channel=channel_cfg.name, error=str(exc))
        return WebhookResult(status_code=400, body={"detail": "Invalid JSON"})

    # 3. IDEMPOTENCY KEY (IDM-01, D-06)
    # derive_webhook_idempotency_key uses canonical-cased header names; re-case the
    # lowercased ASGI headers back to canonical form for the derivation function.
    canonical_headers: dict[str, str] = {
        "X-GitHub-Delivery": lower_headers.get("x-github-delivery", ""),
        "X-Gitlab-Event-UUID": lower_headers.get("x-gitlab-event-uuid", ""),
        "svix-id": lower_headers.get("svix-id", ""),
        "X-Request-ID": lower_headers.get("x-request-id", ""),
        "Idempotency-Key": lower_headers.get("idempotency-key", ""),
    }
    idempotency_key = derive_webhook_idempotency_key(canonical_headers)

    # 4. DISPATCH PARSE by source → (delivery_context, session_key).
    # gitlab/github raise on missing payload fields → 422; generic never 422s.
    try:
        if source == "github":
            delivery_context, session_key = _parse_github(body)
        elif source == "generic":
            delivery_context, session_key = _parse_generic(idempotency_key)
        else:  # source == "gitlab"
            allowed = set(channel_cfg.webhook.gitlab_events or _DEFAULT_GITLAB_EVENTS)
            parsed = _parse_gitlab(body, allowed)
            if parsed is None:
                log.info(
                    "webhook: gitlab event ignored (not a routed kind)",
                    channel=channel_cfg.name,
                    object_kind=body.get("object_kind"),
                    noteable_type=body.get("object_attributes", {}).get("noteable_type"),
                )
                return WebhookResult(status_code=200, body={"status": "ignored"})
            delivery_context, session_key = parsed
            # ACTOR GATES (gitlab only, pre-enqueue). Loop-guard first, then allowlist.
            actor = _gitlab_actor(body)
            wh = channel_cfg.webhook
            if wh.bot_username and actor == wh.bot_username:
                log.info(
                    "webhook: gitlab event ignored (self-authored — loop guard)",
                    channel=channel_cfg.name,
                    actor=actor,
                )
                return WebhookResult(status_code=200, body={"status": "ignored"})
            if wh.trigger_users is not None and actor not in wh.trigger_users:
                log.info(
                    "webhook: gitlab event ignored (actor not in triggerUsers)",
                    channel=channel_cfg.name,
                    actor=actor,
                )
                return WebhookResult(status_code=200, body={"status": "ignored"})
    except (KeyError, TypeError, ValueError) as exc:
        log.warning(
            "webhook: payload missing required fields",
            channel=channel_cfg.name,
            source=source,
            error=str(exc),
        )
        return WebhookResult(status_code=422, body={"detail": "Missing required fields"})

    # 4b. SECONDARY dedup key — GitLab logical content composite (gitlab source only).
    # Legacy ran dedup AFTER trigger classification so an ignored `open` couldn't shadow a
    # later `update`; ach-agent has no harness trigger step (every event is forwarded to the
    # agent) and the composite is content-sensitive on a short window, so no shadowing is
    # possible — the secondary key is safe to compute here at parse time.
    secondary_key = derive_gitlab_composite_key(body) if source == "gitlab" else None

    # Correlation id — uuid4 hex, echoed on 202 and logged by engine_runner for log/trace
    # correlation ONLY (not persisted, not queryable).
    task_id = uuid.uuid4().hex

    # 5. BUILD MessageEvent
    event = MessageEvent(
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel_name=channel_cfg.name,
        payload=body,
        delivery_context=delivery_context,
        source_trait="sync",
        secondary_idempotency_key=secondary_key,
        task_id=task_id,
    )

    log.info(
        "webhook: dispatching event",
        channel=channel_cfg.name,
        source=source,
        idempotency_key=idempotency_key,
        session_key=session_key,
    )

    # 6. DISPATCH → router → D-05 status map
    result = await handler.handle(event)
    return _status_map(result, task_id)
