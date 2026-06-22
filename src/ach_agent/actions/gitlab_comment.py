# SPDX-License-Identifier: Apache-2.0
"""GitlabCommentAdapter — clean-room aiohttp MR-notes client (ACT-02, D-06, D-12).

Delivers reply actions as GitLab MR notes via the REST API.
GITLAB_TOKEN is read from os.environ at deliver() call time — never stored as
an instance attribute (D-12 deviation; SEC-02 spirit).

ACT-02: out-of-band delivery as gitlab_comment.
ACT-03: sideEffect consent gate + dry-run executor + audit (Phase 5).
ACT-04: only accepted, validated actions reach this adapter.
SEC-02: GITLAB_TOKEN read at call time, never stored; cross-host redirect strips token.
RTR-06: NEVER import from hermes_agent.* or engine.* or router.* here.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Literal
from urllib.parse import urljoin, urlparse

import aiohttp
import structlog

from ach_agent.actions.side_effect import DryRunSideEffectExecutor, SideEffectExecutor
from ach_agent.channels.message_event import MessageEvent
from ach_agent.config.schema import ResponseActionBlock

log = structlog.get_logger(__name__)


class UnsupportedActionKind(Exception):
    """Raised when the dispatch loop encounters an unsupported action kind.

    ACT-03: sideEffect actions go through the consent gate; ConsentDenied
    (a subclass) is raised on denial and caught inside the dispatch loop.
    The dispatch loop MUST catch this and continue — never let it propagate out.
    """


class ConsentDenied(UnsupportedActionKind):
    """Raised inside dispatch_actions when a consent-tier sideEffect is denied.

    Subclasses UnsupportedActionKind so callers that already catch
    UnsupportedActionKind automatically catch consent denials too (D-05).

    The dispatch loop catches this and continues — the accompanying reply
    action is still delivered (D-05: ConsentDenied never escapes the loop).
    """


class GitlabCommentAdapter:
    """Deliver reply action as a GitLab MR note (ACT-02, D-06).

    Token from GITLAB_TOKEN env var (D-12 deviation — read at call time).
    Target from delivery context: project_id + mr_iid (D-07).
    Manual redirect loop strips PRIVATE-TOKEN on cross-host hops (SEC-02 / Pitfall 3).
    Bounded retry on 429 with exponential backoff.

    Usage::

        adapter = GitlabCommentAdapter(base_url=os.environ["GITLAB_BASE_URL"])
        await adapter.deliver(action, context)
        await adapter.close()  # on shutdown (Phase 3 drain)
    """

    def __init__(
        self,
        base_url: str = "https://gitlab.com",
        max_retries: int = 3,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._max_retries = max_retries
        self._session: aiohttp.ClientSession | None = None

    async def _ensure_session(self) -> aiohttp.ClientSession:
        """Return the lazily-created, reused aiohttp session (connection pooling)."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """Close the underlying aiohttp session.

        Call during harness shutdown for graceful drain (Phase 3 hook).
        """
        if self._session is not None and not self._session.closed:
            await self._session.close()
            self._session = None

    async def deliver(self, action: dict[str, Any], context: dict[str, Any]) -> None:
        """Post action["input"]["text"] as a note on the MR described by context.

        Reads GITLAB_TOKEN from os.environ at call time — never stored in self.
        Cross-host redirects have PRIVATE-TOKEN stripped (Pitfall 3).

        Args:
            action: Engine-emitted action dict; action["input"]["text"] is the note body.
            context: Delivery context with keys "project_id" (int) and "mr_iid" (int).
        """
        # D-12: read at call time; never store on the instance (SEC-02)
        token = os.environ["GITLAB_TOKEN"]
        project_id: int = context["project_id"]
        mr_iid: int = context["mr_iid"]
        body_text: str = action.get("input", {}).get("text", "")

        url = f"{self._base_url}/api/v4/projects/{project_id}/merge_requests/{mr_iid}/notes"
        # Numeric project_id needs no URL encoding (A3 from RESEARCH.md)

        session = await self._ensure_session()
        base_host = urlparse(self._base_url).hostname

        for attempt in range(self._max_retries + 1):
            try:
                result = await self._post_with_redirect(session, url, body_text, token, base_host)
                if result == "retry_429":
                    if attempt < self._max_retries:
                        await asyncio.sleep(2**attempt)
                        continue
                    log.error(
                        "gitlab_comment: rate limited after retries",
                        project_id=project_id,
                        mr_iid=mr_iid,
                        attempts=attempt + 1,
                    )
                    return
                elif result == "error":
                    return
                else:
                    log.info(
                        "gitlab_comment: note posted",
                        project_id=project_id,
                        mr_iid=mr_iid,
                    )
                    return
            except aiohttp.ClientError as exc:
                if attempt < self._max_retries:
                    await asyncio.sleep(2**attempt)
                    continue
                log.error(
                    "gitlab_comment: request failed",
                    error=str(exc),
                    project_id=project_id,
                    mr_iid=mr_iid,
                )
                return

    async def _post_with_redirect(
        self,
        session: aiohttp.ClientSession,
        url: str,
        body_text: str,
        token: str,
        base_host: str | None,
    ) -> str:
        """POST with manual redirect loop that strips PRIVATE-TOKEN on cross-host hops.

        Redirect semantics (matching browser / RFC 7231):
          - 301/302/303 redirect from POST → follow as GET (body dropped)
          - 307/308 redirect → follow as POST (body preserved)
        PRIVATE-TOKEN is stripped when the redirect hostname differs from base_host.

        Returns "ok", "retry_429", or "error".
        """
        current_url = url
        # Headers for the initial request — include PRIVATE-TOKEN
        headers: dict[str, str] = {
            "PRIVATE-TOKEN": token,
            "Content-Type": "application/json",
        }
        # use_post controls whether the next hop is a POST (True) or GET (False)
        use_post = True
        json_body: dict[str, str] | None = {"body": body_text}

        for _hop in range(10):
            if use_post and json_body is not None:
                async with session.post(
                    current_url,
                    json=json_body,
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        # Resolve relative Location against current URL so a bare
                        # path like /api/v4/... becomes an absolute URL (T-02-07).
                        current_url = urljoin(current_url, location)
                        # Fail-closed: strip PRIVATE-TOKEN if host differs OR is None
                        # (urljoin of a valid absolute base should never yield None,
                        # but strip defensively if it ever does — T-02-07).
                        redirect_host = urlparse(current_url).hostname
                        if redirect_host != base_host or redirect_host is None:
                            headers = {k: v for k, v in headers.items() if k != "PRIVATE-TOKEN"}
                        # 307/308 preserve POST + body; 301/302/303 switch to GET
                        if status in (307, 308):
                            use_post = True
                        else:
                            use_post = False
                            json_body = None
                        continue
                    if status == 429:
                        return "retry_429"
                    if status >= 400:
                        text = await resp.text()
                        log.error(
                            "gitlab_comment: post failed",
                            status=status,
                            response=text[:200],
                        )
                        return "error"
                    return "ok"
            else:
                # GET follow (301/302/303 redirect from original POST, or GET redirect)
                async with session.get(
                    current_url,
                    headers=headers,
                    allow_redirects=False,
                ) as resp:
                    status = resp.status
                    if status in (301, 302, 303, 307, 308):
                        location = resp.headers.get("Location", "")
                        # Resolve relative Location against current URL (T-02-07).
                        current_url = urljoin(current_url, location)
                        # Fail-closed: strip PRIVATE-TOKEN if host differs OR is None
                        redirect_host = urlparse(current_url).hostname
                        if redirect_host != base_host or redirect_host is None:
                            headers = {k: v for k, v in headers.items() if k != "PRIVATE-TOKEN"}
                        continue
                    if status == 429:
                        return "retry_429"
                    if status >= 400:
                        text = await resp.text()
                        log.error("gitlab_comment: get failed after redirect", status=status)
                        return "error"
                    return "ok"

        log.error("gitlab_comment: too many redirects", url=url)
        return "error"


def _emit_audit(
    action: dict[str, Any],
    event: MessageEvent | None,
    decision: Literal["executed-dryrun", "denied"],
    consent_tier: str,
    reason: str,
) -> None:
    """Emit a sideeffect.audit structlog event (D-07, D-06).

    The audit event carries the full decision record and flows through the
    existing redact_ek_processor + redact_gitlab_token_processor pipeline for
    free (D-06). No new durable storage is required.

    D-07 fields: action_name, action_kind, consent_tier, decision, reason,
    session_key, idempotency_key, channel_name, intended_input.

    Pitfall 4 (v1 input safety): v1 action inputs are schema-validated and
    contain no live secrets (no ek_/GITLAB_TOKEN in sideEffect input fields).
    The existing redaction processors strip any that do slip through.
    """
    log.info(
        "sideeffect.audit",
        action_name=action.get("name"),
        action_kind=action.get("kind"),
        consent_tier=consent_tier,
        decision=decision,
        reason=reason,
        session_key=event.session_key if event is not None else None,
        idempotency_key=event.idempotency_key if event is not None else None,
        channel_name=event.channel_name if event is not None else None,
        intended_input=action.get("input", {}),
    )


async def dispatch_actions(
    actions: list[dict[str, Any]],
    adapter: Any,
    context: dict[str, Any],
    *,
    event: MessageEvent | None = None,
    side_effect_executor: SideEffectExecutor | None = None,
    response_action_config: dict[str, ResponseActionBlock] | None = None,
) -> None:
    """Iterate engine result actions and dispatch each to the delivery adapter.

    ACT-03 / D-05: sideEffect actions pass through the consent gate:
      - auto tier → dry-run execute + audit("executed-dryrun"), no consent check.
      - consent tier + user_consented=True → dry-run execute + audit("executed-dryrun").
      - consent tier + user_consented=False → ConsentDenied raised + audit("denied"),
        caught inside the loop; reply still delivers (D-05 / Pitfall 5).
    Absent response_action_config entry → defaults to consent tier (safe).

    Args:
        actions: List of engine-emitted action dicts.
        adapter: DeliveryAdapter implementation (e.g. GitlabCommentAdapter).
        context: Delivery context passed through to adapter.deliver().
        event: The MessageEvent that triggered this invocation (D-07 correlation fields).
        side_effect_executor: Executor for sideEffect actions. Defaults to
            DryRunSideEffectExecutor (v1 dry-run / no-op per D-01).
        response_action_config: Per-channel map of action name → ResponseActionBlock.
            Used to look up consent_tier for each sideEffect. Defaults to {} (all consent).
    """
    executor: SideEffectExecutor = side_effect_executor or DryRunSideEffectExecutor()
    cfg_map: dict[str, ResponseActionBlock] = response_action_config or {}

    for action in actions:
        kind = action.get("kind")
        if kind == "sideEffect":
            # Consent gate (ACT-03 / D-05):
            # 1. Look up the action's ResponseActionBlock by name (absent → consent tier).
            # 2. Resolve consent_tier from the block (default "consent" — safe tier).
            # 3. If auto: execute regardless. If consent: check event.user_consented.
            # 4. On denial: raise ConsentDenied internally, catch it, audit, continue.
            #    Anti-pattern: NEVER re-raise — reply must still deliver (D-05).
            action_name = action.get("name", "")
            cfg: ResponseActionBlock | None = cfg_map.get(action_name)
            consent_tier: str = cfg.consent_tier if cfg is not None else "consent"
            consented: bool = event.user_consented if event is not None else False

            try:
                if consent_tier == "auto" or (consent_tier == "consent" and consented):
                    # Consent passes: dry-run execute + audit
                    await executor.execute(action, context)
                    _emit_audit(
                        action,
                        event,
                        decision="executed-dryrun",
                        consent_tier=consent_tier,
                        reason="consent passed",
                    )
                else:
                    # Consent denied: raise inside try so the except below catches it
                    raise ConsentDenied(
                        f"sideEffect '{action_name}' denied: "
                        f"consentTier={consent_tier!r}, user_consented={consented}"
                    )
            except ConsentDenied as exc:
                # D-05: catch here — never propagate out; reply must still deliver.
                _emit_audit(
                    action,
                    event,
                    decision="denied",
                    consent_tier=consent_tier,
                    reason=str(exc),
                )
                log.warning(
                    "sideeffect.consent_denied",
                    action_name=action_name,
                    consent_tier=consent_tier,
                    user_consented=consented,
                    reason=str(exc),
                )
            continue
        if kind == "reply":
            await adapter.deliver(action, context)
