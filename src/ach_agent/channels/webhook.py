# SPDX-License-Identifier: Apache-2.0
"""Webhook channel adapter — GitLab MR inbound (CHN-01, IDM-01, D-05, SEC-02, D-07).

Locked decisions:
  - Auth: GitLab plain-token compare via hmac.compare_digest (NOT HMAC-SHA256 body sig).
    See RESEARCH.md Pitfall 1: GitLab sends X-Gitlab-Token as a plain secret string.
  - Secret: read per-request from auth.secret_path via Path.read_text(); NEVER stored
    as an instance attribute or long-lived variable (SEC-02, Pitfall 2).
  - delivery_context: {project_id, mr_iid} extracted at parse time and threaded through
    MessageEvent.delivery_context (D-07).
  - Idempotency key: derive_webhook_idempotency_key(headers) — X-Gitlab-Event-UUID or
    ms-timestamp fallback (IDM-01, Pitfall 7: always send unique UUID per test POST).
  - Status map: ACCEPTED→202, DUPLICATE→200, FULL_QUEUE→503 (D-05).
  - source_trait: "sync" (webhook caller can handle 503 / retry).
  - reply mode (CR-01 / ACT-01): when deliver.type == "reply", WebhookResult carries
    a reply_future set on the admitted event. engine_runner resolves it; the route
    awaits it to return reply text on the held connection. Engine runs EXACTLY ONCE
    on the bounded lane — no separate sync_invoke call.

RTR-06: NEVER import from hermes_agent.* here.

Boot-order: imported after configure_logging().
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from ach_agent.channels.message_event import MessageEvent
from ach_agent.router.dedup import derive_webhook_idempotency_key
from ach_agent.router.router import RouterAdmitResult

if TYPE_CHECKING:
    from ach_agent.channels.seam import MessageHandler
    from ach_agent.config.schema import ChannelConfig, WebhookAuthBlock

log = structlog.get_logger(__name__)


@dataclass
class WebhookResult:
    """Outcome of a webhook request — carries HTTP status + JSON body.

    reply_future: populated only when deliver.type == "reply" and the event was
    ACCEPTED (202). The route awaits this future to obtain the engine reply text.
    None in all non-reply or non-202 outcomes (CR-01 / ACT-01).
    """

    status_code: int
    body: dict[str, Any]
    reply_future: asyncio.Future[str] | None = field(default=None)


def _verify_gitlab_token(header_token: str, secret_path: str) -> bool:
    """Constant-time compare of X-Gitlab-Token against mounted secret.

    GitLab uses plain-token compare, NOT HMAC-SHA256 body signature (Pitfall 1).
    Secret read per-call from secret_path for rotation support (SEC-02, Pitfall 2).
    The secret is read, compared, and discarded — never stored in an attribute.
    """
    if not header_token:
        return False
    # SEC-02: read per-request, discard after use — NEVER assigned to long-lived attr
    secret = Path(secret_path).read_text(encoding="utf-8").strip()
    return hmac.compare_digest(header_token, secret)


def _verify_hmac(signature: str, secret_path: str, raw_body: bytes) -> bool:
    """Verify GitHub-style HMAC-SHA256 body signature (X-Hub-Signature-256).

    The header value is `sha256=<hexdigest>`; we strip the prefix and compare in
    constant time against HMAC-SHA256(secret, raw_body). Secret read per-request
    from secret_path (SEC-02), never cached.
    """
    if not signature:
        return False
    sig = signature[len("sha256=") :] if signature.startswith("sha256=") else signature
    # SEC-02: read per-request, discard after use — NEVER assigned to long-lived attr
    secret_bytes = Path(secret_path).read_text(encoding="utf-8").strip().encode("utf-8")
    expected = hmac.new(secret_bytes, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, expected)


def _verify_header_token(header_token: str, secret_path: str) -> bool:
    """Constant-time compare a static shared secret carried in a configurable header.

    Secret read per-call from secret_path for rotation support (SEC-02). Empty header or
    empty/unreadable secret → reject.
    """
    if not header_token or not secret_path:
        return False
    secret = Path(secret_path).read_text(encoding="utf-8").strip()
    return bool(secret) and hmac.compare_digest(header_token, secret)


def _verify_auth(auth: WebhookAuthBlock, lower_headers: dict[str, str], raw_body: bytes) -> bool:
    """Dispatch auth verification by auth.type (gitlab_token | hmac | header_token | none)."""
    match auth.type:
        case "gitlab_token":
            return _verify_gitlab_token(lower_headers.get("x-gitlab-token", ""), auth.secret_path)
        case "hmac":
            return _verify_hmac(
                lower_headers.get("x-hub-signature-256", ""), auth.secret_path, raw_body
            )
        case "header_token":
            return _verify_header_token(
                lower_headers.get(auth.header.lower(), ""), auth.secret_path
            )
        case "none":
            return True


def _parse_gitlab(body: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Parse a GitLab Merge Request Hook payload (D-07).

      body.project.id              → project_id (numeric, always int in GitLab)
      body.object_attributes.iid   → mr_iid (MR internal ID, project-scoped)

    Returns (delivery_context, session_key). Raises KeyError/TypeError/ValueError
    on missing/non-castable fields (mapped to 422 by the caller).
    """
    project_id = int(body["project"]["id"])
    mr_iid = int(body["object_attributes"]["iid"])
    return {"project_id": project_id, "mr_iid": mr_iid}, f"{project_id}:{mr_iid}"


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


def _status_map(result: RouterAdmitResult) -> WebhookResult:
    """Map RouterAdmitResult to D-05 HTTP status + JSON body."""
    match result:
        case RouterAdmitResult.ACCEPTED:
            return WebhookResult(status_code=202, body={"status": "accepted"})
        case RouterAdmitResult.DUPLICATE:
            return WebhookResult(status_code=200, body={"status": "duplicate"})
        case RouterAdmitResult.FULL_QUEUE:
            return WebhookResult(status_code=503, body={"status": "full", "retry": True})


async def handle_webhook_request(
    raw_body: bytes,
    headers: dict[str, str],
    channel_cfg: ChannelConfig,
    handler: MessageHandler,
    deliver_type: str | None = None,
) -> WebhookResult:
    """Handle a GitLab MR webhook request end-to-end.

    Stages (ORDER IS NORMATIVE):
      1. Auth: verify X-Gitlab-Token via constant-time compare (SEC-02)
         — invalid/absent → 401 WebhookResult, handler.handle() NOT called
      2. Parse JSON body, extract delivery_context (D-07)
      3. Derive idempotency key from headers (IDM-01, D-06)
      4. Build MessageEvent (source_trait="sync")
         For reply mode: attach event.reply_future BEFORE handler.handle() so the
         lane consumer sees it when engine_runner runs (CR-01 / ACT-01).
      5. Dispatch to handler.handle(event) → map to D-05 status
         For reply ACCEPTED: carry reply_future on WebhookResult for the route.

    Args:
        raw_body:     Raw request body bytes (read by caller before JSON parse).
        headers:      Request headers dict (case-sensitive; caller normalises keys).
        channel_cfg:  Channel configuration carrying webhook.auth.secret_path.
        handler:      MessageHandler (Router) to dispatch to.
        deliver_type: deliver.type from channel config ("reply" / "gitlab_comment" /
                      None). When "reply" and ACCEPTED, creates event.reply_future
                      and returns it on WebhookResult so the route can await it.

    Returns:
        WebhookResult with status_code, body dict, and (reply mode only) reply_future.
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
        else:
            delivery_context, session_key = _parse_gitlab(body)
    except (KeyError, TypeError, ValueError) as exc:
        log.warning(
            "webhook: payload missing required fields",
            channel=channel_cfg.name,
            source=source,
            error=str(exc),
        )
        return WebhookResult(status_code=422, body={"detail": "Missing required fields"})

    # 5. BUILD MessageEvent
    # For reply mode: create reply_future BEFORE handler.handle() so the lane
    # consumer can resolve it when engine_runner runs (CR-01: single execution).
    reply_future: asyncio.Future[str] | None = None
    if deliver_type == "reply":
        reply_future = asyncio.get_running_loop().create_future()

    event = MessageEvent(
        idempotency_key=idempotency_key,
        session_key=session_key,
        channel_name=channel_cfg.name,
        payload=body,
        delivery_context=delivery_context,
        source_trait="sync",
        reply_future=reply_future,
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
    webhook_result = _status_map(result)

    # 7. Carry reply_future on result ONLY for ACCEPTED reply-mode events.
    # Non-202 outcomes (401/200-dup/503): no future — reply_future stays None.
    if deliver_type == "reply" and webhook_result.status_code == 202:
        webhook_result.reply_future = reply_future

    return webhook_result
