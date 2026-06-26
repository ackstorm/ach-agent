# SPDX-License-Identifier: Apache-2.0
"""SanitizedEnv and structlog redaction processors.

SEC-01: ek_ presented as bearer, never logged or forwarded to tool backends.
SEC-03: GITLAB_TOKEN value never logged (token-in-logs safety net).
The SanitizedEnv class wraps the subprocess env dict; the redact_ek_processor
and redact_gitlab_token_processor strip secrets before structlog renders log lines.

Constraint:
  - No router or Hermes imports (D-08, RTR-06).
  - The ACH_API_KEY value is NEVER read into a Python variable here (Pitfall 6 /
    T-00-TRACE). It flows only through the env dict into the subprocess via
    {env:ACH_API_KEY} in opencode.json. Python tracebacks cannot leak what is
    never in a frame local.
  - GITLAB_TOKEN is read inside the processor only when present — the token value
    is never stored as a module-level constant (it may rotate between invocations).
"""

from __future__ import annotations

import logging
import os
import re
import sys

import structlog
from structlog.typing import EventDict, WrappedLogger

# Pattern matches the ek bearer token prefix (SEC-01).
# Real ACH keys use the `ek-` (dash) prefix; earlier code assumed `ek_` (underscore).
# Match BOTH separators so the live bearer is redacted (confirmed vs real ACH).
# Matches: ek, then `-` or `_`, then one or more alphanumeric / underscore / dash.
# No leading \b — must be caught even when embedded mid-word (CR-03).
_EK_PATTERN = re.compile(r"ek[-_][A-Za-z0-9_\-]+")


def redact_ek_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: replace any ek_ token value with [REDACTED].

    Must be inserted early in the structlog processor chain, before the
    render stage. Recurses one level into dict values (env dict logging).

    SEC-01 / T-00-EK: prevents ACH_API_KEY leaking into structured log output.
    """
    for key, value in list(event_dict.items()):
        if isinstance(value, str) and _EK_PATTERN.search(value):
            event_dict[key] = _EK_PATTERN.sub("[REDACTED]", value)
        elif isinstance(value, dict):
            # Recurse one level — catches env dict logging ({"ACH_API_KEY": "ek_..."})
            for k2, v2 in list(value.items()):
                if isinstance(v2, str) and _EK_PATTERN.search(v2):
                    value[k2] = _EK_PATTERN.sub("[REDACTED]", v2)
    return event_dict


def redact_gitlab_token_processor(
    logger: WrappedLogger, method_name: str, event_dict: EventDict
) -> EventDict:
    """structlog processor: replace the live GITLAB_TOKEN value with [REDACTED].

    Reads GITLAB_TOKEN from os.environ at processor-call time (never cached
    as a module-level constant — allows rotation). If GITLAB_TOKEN is unset
    or empty the processor is a no-op.

    Must be inserted early in the structlog processor chain, before the
    render stage. Recurses one level into dict values (env dict logging).

    SEC-03 / T-02-08: prevents GITLAB_TOKEN value leaking into log output
    via upstream error strings or accidental log.info(..., token=token) calls.
    """
    gitlab_token = os.environ.get("GITLAB_TOKEN", "")
    if not gitlab_token:
        return event_dict

    for key, value in list(event_dict.items()):
        if isinstance(value, str) and gitlab_token in value:
            event_dict[key] = value.replace(gitlab_token, "[REDACTED]")
        elif isinstance(value, dict):
            for k2, v2 in list(value.items()):
                if isinstance(v2, str) and gitlab_token in v2:
                    value[k2] = v2.replace(gitlab_token, "[REDACTED]")
    return event_dict


class SanitizedEnv:
    """Env dict wrapper that never exposes ek_ values in repr/logging.

    Wraps the subprocess environment so Python tracebacks and log statements
    that print local variables cannot leak the ACH_API_KEY value (SEC-01 /
    T-00-EK, Pitfall 6).

    Usage::

        env = SanitizedEnv(os.environ.copy())
        proc = await asyncio.create_subprocess_exec(..., env=env.as_dict())
        log.info("launching", env=repr(env))  # ek_ values are [REDACTED] in log
    """

    def __init__(self, env: dict[str, str]) -> None:
        self._env = env

    def as_dict(self) -> dict[str, str]:
        """Return the raw env dict for passing to subprocess.

        The subprocess needs the real values (opencode dereferences
        {env:ACH_API_KEY} at runtime). Only repr/logging is masked.
        """
        return self._env

    def __repr__(self) -> str:
        """Safe repr for logging — masks ek_ values with [REDACTED]."""
        masked = {
            k: _EK_PATTERN.sub("[REDACTED]", v) if isinstance(v, str) else v
            for k, v in self._env.items()
        }
        return f"SanitizedEnv({masked!r})"

    def __str__(self) -> str:
        """Safe str — same as repr."""
        return self.__repr__()


class _StderrProxy:
    """Write proxy that forwards to the *current* ``sys.stderr`` at each call.

    Logs must go to STDERR so STDOUT carries only the agent reply (the live token
    stream from the tui/one-shot sink). Passing ``sys.stderr`` directly to
    PrintLoggerFactory would freeze the handle at configure time (import); under
    pytest the captured stderr is closed/swapped between tests, raising
    "I/O operation on closed file". Resolving ``sys.stderr`` per write follows
    whatever stream is live (prod: real stderr; pytest: the per-test capture).
    """

    def write(self, s: str) -> int:
        return sys.stderr.write(s)

    def flush(self) -> None:
        sys.stderr.flush()


_LOG_LEVELS = {
    "debug": logging.DEBUG,
    "info": logging.INFO,
    "warning": logging.WARNING,
    "warn": logging.WARNING,
    "error": logging.ERROR,
}


def _resolve_log_level() -> int:
    """Resolve the filtering level from ACH_LOG_LEVEL (default INFO).

    Default INFO keeps the console readable: at debug, the opencode stdout/stderr drain
    re-logs every line opencode prints (~135/178 lines in a calendar run) and buries the
    agent reply on the shared TTY. ACH_LOG_LEVEL=debug restores the full firehose,
    including the raw per-event SSE trace.
    """
    return _LOG_LEVELS.get(os.environ.get("ACH_LOG_LEVEL", "").strip().lower(), logging.INFO)


def configure_logging() -> None:
    """Configure structlog with the redact_ek_processor in the processor chain.

    Must be called at harness startup before any log output is emitted.
    The redact_ek_processor is inserted before the final renderer so all
    log entries are scrubbed of ek_ tokens regardless of call site.
    """
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            redact_ek_processor,  # SEC-01: must come before renderer
            redact_gitlab_token_processor,  # SEC-03: must come before renderer
            structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(_resolve_log_level()),
        context_class=dict,
        # Logs → stderr so STDOUT carries ONLY the agent reply (live token stream from
        # the tui/one-shot sink). Without this the streamed reply is buried in the log
        # torrent on a shared stdout; `--prompt ... 2>/dev/null` now yields a clean reply.
        # PrintLogger only ever calls .write/.flush — the proxy satisfies that at runtime;
        # the stub demands full TextIO, hence the narrow ignore.
        logger_factory=structlog.PrintLoggerFactory(file=_StderrProxy()),  # type: ignore[arg-type]
    )
