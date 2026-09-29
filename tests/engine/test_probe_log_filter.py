from __future__ import annotations

import logging

from ach_agent.engine.sanitized_env import _ProbeLogFilter


def _record(url: str) -> logging.LogRecord:
    return logging.LogRecord(
        "httpx", logging.INFO, __file__, 1, 'HTTP Request: %s %s "%s %d %s"',
        ("GET", url, "HTTP/1.1", 200, "OK"), None,
    )


def test_probe_lines_dropped_below_debug(monkeypatch) -> None:
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    f = _ProbeLogFilter()
    assert not f.filter(_record("http://ach-internal/execution/v1/health"))
    assert not f.filter(_record("http://ach-internal/readyz"))
    assert f.filter(_record("https://ach.example/mcp/ach-memory"))


def test_probe_lines_kept_at_debug(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    assert _ProbeLogFilter().filter(_record("http://ach-internal/readyz"))


def test_facade_token_urls_always_dropped(monkeypatch) -> None:
    monkeypatch.setenv("LOG_LEVEL", "debug")
    f = _ProbeLogFilter()
    assert not f.filter(_record("http://bot.ach.svc:8095/s/tok123/session/archive"))
