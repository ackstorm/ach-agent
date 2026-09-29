# SPDX-License-Identifier: Apache-2.0
"""mitmproxy addon: credential substitution only (design §1, §6).

A request to a declared services[] origin gets its caller-supplied credential
replaced with the harness-held one. Every other request — including https to an
undeclared host, or plain http — passes through untouched: no default deny, no
destination filtering, no method/path policy. An exception in matching/injection
is the one case that must not silently forward with a stale state, so it becomes
a 502 rather than propagating out of the addon hook.
"""

from __future__ import annotations

from collections.abc import Sequence

import structlog
from mitmproxy import http

from ach_agent.egress.policy import ResolvedService, match_service

log = structlog.get_logger(__name__)


class EgressAddon:
    def __init__(self, services: Sequence[ResolvedService]) -> None:
        self._services = tuple(services)
        # Every managed credential header across all services, plus caller auth forms —
        # stripped before injection so a caller can't smuggle another service's header
        # or override the managed identity.
        self._strip = frozenset(
            {"authorization", "proxy-authorization", *(s.header.lower() for s in services)}
        )

    def request(self, flow: http.HTTPFlow) -> None:
        try:
            self._handle(flow)
        except Exception:
            log.error("egress: credential injection failed — aborting request", exc_info=True)
            flow.response = http.Response.make(502)

    def _handle(self, flow: http.HTTPFlow) -> None:
        req = flow.request
        if req.scheme != "https":
            return
        svc = match_service(self._services, req.host, req.port)
        if svc is None:
            return
        for name in self._strip:
            req.headers.pop(name, None)  # Headers is case-insensitive; pop drops all dupes
        req.headers[svc.header] = svc.prefix + svc.secret
        log.info("egress: credential injected", service=svc.name, method=req.method)
