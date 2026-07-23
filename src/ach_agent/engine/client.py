# SPDX-License-Identifier: Apache-2.0
"""Back-compat shim: OpenCodeClient + port helpers moved to engine/opencode/client.py (SP1).

Re-exported here so existing `from ach_agent.engine.client import …` sites keep resolving.
NOTE: module-level function/submodule `patch()` targets must use the opencode.client path.
"""

from ach_agent.engine.opencode.client import OpenCodeClient as OpenCodeClient
from ach_agent.engine.opencode.client import _reserved_ports as _reserved_ports
from ach_agent.engine.opencode.client import find_free_port as find_free_port
from ach_agent.engine.opencode.client import release_port as release_port
