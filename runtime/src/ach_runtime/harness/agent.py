# SPDX-License-Identifier: MIT
"""The `agent` role: control composition with an in-process LocalSource (spec 4.3).

Single-unit topology for a laptop, test, pre-production or CI machine. Not for production (D5).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from ach_runtime.config.public import ExecutionBootstrap
from ach_runtime.config.schema import InfrastructureBlock, RuntimeConfig
from ach_runtime.harness.boot import run_control
from ach_runtime.harness.control import Control, assemble
from ach_runtime.harness.local import LocalRoutingTransport, LocalSource

log = logging.getLogger(__name__)

AGENT_FACADE_PORT = 8081  # fixed in v2.0 (spec Q5)
AGENT_FACADE_ENDPOINT = f"http://127.0.0.1:{AGENT_FACADE_PORT}/facades"
SINGLE_UNIT_WARNING = (
    "single-unit topology: agent tools share this process's credentials; not for production (contract v2 D5)"
)


def make_agent_assembly(
    data_dir: Path, transport: LocalRoutingTransport, created: list[LocalSource], **assemble_kwargs: Any
):
    """`run_control`'s `assembly` hook: inject the loopback facade default, then assemble
    with a LocalSource that is also appended to `created` so the runner can stop its units."""

    def source_factory(bootstrap: ExecutionBootstrap, verify_key: str) -> LocalSource:
        source = LocalSource(bootstrap, verify_key=verify_key, data_dir=data_dir, transport=transport)
        created.append(source)
        return source

    async def assembly(config: RuntimeConfig) -> Control:
        if config.infrastructure.facade_endpoint is None:
            config = config.model_copy(
                update={"infrastructure": InfrastructureBlock(facadeEndpoint=AGENT_FACADE_ENDPOINT)}
            )
        return await assemble(config, mini_transport=transport, source_factory=source_factory, **assemble_kwargs)

    return assembly


async def run_agent(config_path: Path, *, data_dir: Path, host: str, port: int) -> None:
    log.warning(SINGLE_UNIT_WARNING)
    transport = LocalRoutingTransport()
    created: list[LocalSource] = []
    try:
        await run_control(
            config_path, host=host, ingress_port=port,
            facade_host="127.0.0.1", facade_port=AGENT_FACADE_PORT, host_api=False,
            assembly=make_agent_assembly(data_dir, transport, created),
        )
    finally:
        for source in created:  # control.close() has drained; shutdown saves no Workspace, so unsaved work is lost
            await source.aclose()
