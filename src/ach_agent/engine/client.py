# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: OpenCodeClient + port helpers moved to engine/opencode/client.py (SP1).

Re-exported here so existing `from ach_agent.engine.client import …` sites keep resolving.
NOTE: module-level function/submodule `patch()` targets must use the opencode.client path.
"""
from ach_agent.engine.opencode.client import (  # noqa: F401
    OpenCodeClient,
    _reserved_ports,
    find_free_port,
    release_port,
)
