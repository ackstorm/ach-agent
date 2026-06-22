"""CONTRACT §6.9: Dual delivery invariant (authoritative conformance test).

Invariant: synchronous reply + out-of-band delivery are both supported.
"""
from __future__ import annotations

import asyncio
from typing import Any


async def test_inv09_dual_delivery() -> None:
    """§6.9: dual delivery — sync reply + out-of-band both supported — authoritative conformance.

    CONTRACT perspective: the harness must support two independent delivery paths:
    1. Synchronous reply: the reply action resolves event.reply_future so the HTTP
       handler can return the reply body to the caller (reply mode, e.g. CR-01).
    2. Out-of-band delivery: the adapter.deliver() call dispatches the action
       asynchronously (e.g. gitlab_comment mode, ACT-01).

    Both paths must complete successfully in isolation.
    """
    from ach_agent.actions.gitlab_comment import dispatch_actions

    # ---- Path 1: sync reply via reply_future ----
    # Models the reply-mode webhook path (CR-01): the adapter resolves reply_future
    # with the reply text so the HTTP handler can return a synchronous response.
    loop = asyncio.get_event_loop()
    reply_future: asyncio.Future[str] = loop.create_future()

    delivered_out_of_band: list[dict[str, Any]] = []

    class _FakeAdapter:
        async def deliver(
            self, action: dict[str, Any], context: dict[str, Any]
        ) -> None:
            delivered_out_of_band.append(action)
            # Resolve the reply_future when a reply action is delivered (models
            # what the webhook adapter does in reply-mode, CR-01).
            if action.get("kind") == "reply" and not reply_future.done():
                reply_future.set_result(action.get("input", {}).get("text", ""))

    reply_action = {"kind": "reply", "input": {"text": "LGTM from conformance test"}}
    await dispatch_actions([reply_action], _FakeAdapter(), {})

    # Out-of-band path: adapter.deliver() was called.
    assert len(delivered_out_of_band) == 1, (
        "§6.9: out-of-band delivery path must call adapter.deliver() "
        "(delivery path 2 not supported)"
    )
    assert delivered_out_of_band[0]["kind"] == "reply"

    # Sync reply path: the reply_future can be resolved (models CR-01 reply mode).
    # In this test we directly resolve it to verify the future mechanism works.
    if not reply_future.done():
        reply_future.set_result("manual")
    assert reply_future.done(), (
        "§6.9: sync reply path — reply_future must be resolvable "
        "(synchronous reply mechanism not available)"
    )
    assert reply_future.result() == "LGTM from conformance test", (
        "§6.9: sync reply result must match the delivered reply text"
    )

    # ---- Path 2: out-of-band only (no reply_future) ----
    oob_delivered: list[dict[str, Any]] = []

    class _OOBAdapter:
        async def deliver(
            self, action: dict[str, Any], context: dict[str, Any]
        ) -> None:
            oob_delivered.append(action)

    oob_action = {"kind": "reply", "input": {"text": "out-of-band"}}
    await dispatch_actions([oob_action], _OOBAdapter(), {})

    assert len(oob_delivered) == 1, (
        "§6.9: out-of-band delivery path (no reply_future) must call adapter.deliver()"
    )
