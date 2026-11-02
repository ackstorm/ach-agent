"""The execution image installs only the base dependencies, not the `control` extra."""

import subprocess
import sys
import textwrap

# Import names of the `control` extra in pyproject.toml and of what it pulls in.
CONTROL_ONLY = ("boto3", "botocore", "s3transfer", "croniter", "redis", "structlog", "a2a", "google")
# What the execution image runs: the unit (`python -m ach_runtime execution`) and `ach-mcp`.
UNIT_MODULES = ("ach_runtime.__main__", "ach_runtime.execution.boot", "ach_runtime.execution.app",
                "ach_runtime.execution.service", "ach_runtime.engine.opencode", "ach_runtime.mcpcall",
                "ach_runtime.storage.archive", "ach_runtime.facades._mcp_session")


def test_the_execution_unit_imports_without_the_control_extra() -> None:
    # A fresh process imports each module by name: no pkgutil walk, which swallows import
    # errors and leaves half-imported submodules that make later imports look fine.
    script = textwrap.dedent(f"""
        import importlib, sys

        class Block:
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in {CONTROL_ONLY!r}:
                    raise ImportError("control-only package imported: " + name)

        sys.meta_path.insert(0, Block())
        for name in {UNIT_MODULES!r}:
            importlib.import_module(name)
    """)
    done = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
