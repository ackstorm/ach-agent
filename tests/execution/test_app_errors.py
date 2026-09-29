# SPDX-License-Identifier: Apache-2.0
from __future__ import annotations

import tarfile

from ach_agent.execution.app import _error_response
from ach_agent.execution.service import SessionHookFailed
from ach_agent.sandbox.archive import ArchiveTooLarge


def test_workspace_rejections_are_4xx() -> None:
    assert _error_response(SessionHookFailed("sessionStart failed")).status_code == 422
    assert _error_response(tarfile.TarError("absolute path")).status_code == 422
    assert _error_response(ArchiveTooLarge("big")).status_code == 413
