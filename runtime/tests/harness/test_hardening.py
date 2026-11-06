"""set_non_dumpable makes the process non-dumpable so /proc is not readable by same-uid peers."""

from __future__ import annotations

import subprocess
import sys
import textwrap


def test_set_non_dumpable_clears_the_dumpable_flag() -> None:
    # Run in a subprocess so the test runner itself stays dumpable.
    code = textwrap.dedent(
        """
        import ctypes
        from ach_runtime.hardening import set_non_dumpable
        ok = set_non_dumpable()
        # Read the flag back independently (PR_GET_DUMPABLE == 3); 0 means non-dumpable.
        dumpable = ctypes.CDLL("libc.so.6", use_errno=True).prctl(3, 0, 0, 0, 0)
        print(f"{ok} {dumpable}")
        """
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "True 0", out.stdout + out.stderr
