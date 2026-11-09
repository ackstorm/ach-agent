# SPDX-License-Identifier: Apache-2.0
from importlib.metadata import version

import ach_runtime


def test_version_comes_from_package_metadata() -> None:
    assert ach_runtime.__version__ == version("ach-agent-runtime")
