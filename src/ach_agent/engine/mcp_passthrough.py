# SPDX-License-Identifier: Apache-2.0
"""Translate passthrough MCP config (local/remote) into a canonical engine mcp entry.

The ENGINE is the MCP client for these — it connects DIRECTLY, not through the ACH
localhost proxy — so they carry no ACH credential and get no trace correlation from us.
Every engine consumes this one shape: opencode writes it verbatim into its `mcp.<name>`
block, Pi's adapter reshapes it in ``engine.pi.mcp_json``. Mirrors
ackbot-process._normalize_mcp_server: stdio → type "local" (command array), http → type
"remote" (url + headers). Env NAMES / ${env:NAME} refs are resolved harness-side at write
time — passthrough auth necessarily lands in the engine's config file, which is what needs
it.
"""

from __future__ import annotations

import os
import re

from ach_agent.config.schema import LocalMcpServer, RemoteMcpServer

# ponytail: only our contract's ${env:NAME} form; opencode's own interpolation is not relied on.
_ENV_REF = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env_refs(value: str) -> str:
    """Expand ${env:NAME} → os.environ[NAME] (empty string if unset)."""
    return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)


def to_engine_entry(spec: LocalMcpServer | RemoteMcpServer) -> dict[str, object]:
    """The canonical engine mcp entry for one passthrough server."""
    if isinstance(spec, LocalMcpServer):
        entry: dict[str, object] = {
            "type": "local",
            "command": [spec.command, *spec.args],
            "enabled": True,
        }
        env = {name: os.environ[name] for name in spec.env if name in os.environ}
        if env:
            entry["environment"] = env
        return entry
    # RemoteMcpServer
    remote: dict[str, object] = {"type": "remote", "url": spec.url, "enabled": True}
    if spec.headers:
        remote["headers"] = {k: _expand_env_refs(v) for k, v in spec.headers.items()}
    return remote
