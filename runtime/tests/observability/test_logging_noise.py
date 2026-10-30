# SPDX-License-Identifier: MIT
from __future__ import annotations

import logging

from ach_runtime.observability.runs import configure_logging


def test_configure_logging_silences_per_request_http_client_logs() -> None:
    root, httpx = logging.getLogger(), logging.getLogger("httpx")
    saved = root.level, httpx.level
    try:
        root.setLevel(logging.INFO)  # the control process runs with an INFO root
        httpx.setLevel(logging.NOTSET)
        configure_logging()
        assert not httpx.isEnabledFor(logging.INFO)
        assert httpx.isEnabledFor(logging.WARNING)
    finally:
        root.setLevel(saved[0])
        httpx.setLevel(saved[1])
