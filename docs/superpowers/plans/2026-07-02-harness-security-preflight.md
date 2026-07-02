# Harness Security Preflight Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the harness process at boot so a same-container `opencode` peer cannot steal the harness's in-memory/`/proc` secrets, and refuse to start on an unsafe host unless explicitly overridden.

**Architecture:** One new boot-time module `security/preflight.py` with two layers. **Class A (ENFORCE)** applies our own syscalls and verifies they took — `PR_SET_DUMPABLE=0` (reowns `/proc/self/{environ,mem,maps}` to root and blocks same-UID ptrace) and `PR_SET_NO_NEW_PRIVS=1` (inherited by opencode; blocks privilege regain via setuid/file-cap binaries). Class A hard-fails with no override. **Class B (GATE)** detects host properties it cannot change (running as root, `CAP_SYS_PTRACE`/`CAP_SYS_ADMIN` held) and fails closed, downgradable to warnings via `ACH_INSECURE_ALLOW_DEGRADED=1` for dev. `run_preflight()` is called as the first statement in `main()`, before any secret enters process memory.

**Tech Stack:** Python 3.12, `ctypes` (prctl) + `/proc/self/status` (stdlib only, no new deps), structlog, pytest (`asyncio_mode=auto`).

## Global Constraints

- Python 3.12; always a venv/uv, never system-wide `pip install`.
- **No new dependencies** — `ctypes` + `/proc` are stdlib. (ponytail: nothing on the ladder above stdlib is needed here.)
- Linux-only mechanism (`prctl` + `/proc`). On non-Linux (mac dev/CI) the enforce + gate steps **no-op with a warning** — production is always a Linux container.
- Fail-closed pattern matches existing boot code: `log.error(...)` then `sys.exit(1)` (see `main.py:_open_dedup_store`, `lifecycle.py:poll_ready`).
- **Never log `ek_`/tokens** — the preflight only ever logs gate names + static detail strings; it reads no secret values.
- Lint/type: `ruff` clean, `mypy --strict` clean.
- Override env var name is **exactly** `ACH_INSECURE_ALLOW_DEGRADED` (value `"1"` enables); `ACH_` prefix per contract env convention.

---

### Task 1: Pure gate logic + module scaffold

**Files:**
- Create: `src/ach_agent/security/__init__.py`
- Create: `src/ach_agent/security/preflight.py` (constants + pure helpers only; syscalls added in Task 2)
- Test: `tests/security/__init__.py`
- Test: `tests/security/test_gates.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - Constants: `PR_GET_DUMPABLE=3`, `PR_SET_DUMPABLE=4`, `PR_SET_NO_NEW_PRIVS=38`, `PR_GET_NO_NEW_PRIVS=39`, `CAP_SYS_PTRACE=19`, `CAP_SYS_ADMIN=21`, `DEGRADED_ENV="ACH_INSECURE_ALLOW_DEGRADED"`
  - `parse_status(text: str) -> dict[str, str]`
  - `evaluate_gates(uid: int, cap_eff: int, seccomp: int) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]` — returns `(hard_failures, warnings)`, each item `(gate_name, detail)`.

- [ ] **Step 1: Create the test package init**

Create `tests/security/__init__.py` (empty file).

- [ ] **Step 2: Write the failing tests**

Create `tests/security/test_gates.py`:

```python
# SPDX-License-Identifier: Apache-2.0
from ach_agent.security.preflight import (
    CAP_SYS_ADMIN,
    CAP_SYS_PTRACE,
    evaluate_gates,
    parse_status,
)


def test_parse_status_extracts_fields():
    text = "Name:\tpython\nCapEff:\t0000000000000000\nSeccomp:\t2\n"
    status = parse_status(text)
    assert status["Name"] == "python"
    assert status["CapEff"] == "0000000000000000"
    assert status["Seccomp"] == "2"


def test_evaluate_gates_clean_host_passes():
    hard, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=2)
    assert hard == []
    assert warn == []


def test_evaluate_gates_root_is_hard_failure():
    hard, _ = evaluate_gates(uid=0, cap_eff=0, seccomp=2)
    assert [name for name, _ in hard] == ["not_root"]


def test_evaluate_gates_ptrace_cap_is_hard_failure():
    hard, _ = evaluate_gates(uid=1000, cap_eff=1 << CAP_SYS_PTRACE, seccomp=2)
    assert "no_cap_sys_ptrace" in [name for name, _ in hard]


def test_evaluate_gates_admin_cap_is_hard_failure():
    hard, _ = evaluate_gates(uid=1000, cap_eff=1 << CAP_SYS_ADMIN, seccomp=2)
    assert "no_cap_sys_admin" in [name for name, _ in hard]


def test_evaluate_gates_no_seccomp_is_soft_warning_only():
    hard, warn = evaluate_gates(uid=1000, cap_eff=0, seccomp=0)
    assert hard == []
    assert [name for name, _ in warn] == ["seccomp_filter"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/security/test_gates.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ach_agent.security'`

- [ ] **Step 4: Create the security package init**

Create `src/ach_agent/security/__init__.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Boot-time security hardening for the harness process."""
```

- [ ] **Step 5: Write the module scaffold with constants + pure helpers**

Create `src/ach_agent/security/preflight.py`:

```python
# SPDX-License-Identifier: Apache-2.0
"""Boot-time security preflight — harden the harness, refuse unsafe hosts.

Threat: opencode (the agente) runs in the SAME container as the harness. Even with
clean-slate env hygiene (engine.build_opencode_env), a same-UID peer could read the
harness's decrypted secrets via /proc/<harness>/environ, /proc/<harness>/mem, or
PTRACE_ATTACH.

Two layers:
  Class A — ENFORCE (our own syscalls, verified, hard-fail, NO override):
    * PR_SET_DUMPABLE=0     -> reowns /proc/self/{environ,mem,maps} to root:root and
      blocks same-UID ptrace. Closes the /proc env + memory theft vector.
    * PR_SET_NO_NEW_PRIVS=1 -> inherited by opencode; blocks privilege regain via
      setuid / file-capability binaries, so dumpable=0 cannot be undone by a
      re-privileged child.
  Class B — GATE (host properties we can only detect, fail-closed; override via
    ACH_INSECURE_ALLOW_DEGRADED=1 for local/dev only):
    * not running as root (a root peer reads anything)
    * no CAP_SYS_PTRACE in the effective set (ptrace defeats dumpable=0)
    * no CAP_SYS_ADMIN in the effective set
    Soft (warn-only, defense-in-depth): seccomp filter active.

Linux-only (prctl + /proc). On non-Linux (mac dev) it no-ops with a warning —
production is always a Linux container.
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

# prctl option numbers (linux/prctl.h)
PR_GET_DUMPABLE = 3
PR_SET_DUMPABLE = 4
PR_SET_NO_NEW_PRIVS = 38
PR_GET_NO_NEW_PRIVS = 39

# capability bit positions (linux/capability.h)
CAP_SYS_PTRACE = 19
CAP_SYS_ADMIN = 21

DEGRADED_ENV = "ACH_INSECURE_ALLOW_DEGRADED"


def parse_status(text: str) -> dict[str, str]:
    """Parse /proc/<pid>/status into {field: value} (value = text after the colon)."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            key, _, val = line.partition(":")
            out[key.strip()] = val.strip()
    return out


def _cap_has(cap_eff: int, bit: int) -> bool:
    return bool(cap_eff & (1 << bit))


def evaluate_gates(
    uid: int, cap_eff: int, seccomp: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Pure gate evaluator. Returns (hard_failures, warnings) as (name, detail) lists.

    Hard gates are exactly the properties that would defeat Class A hardening for a
    same-UID peer. Everything else (seccomp) is defense-in-depth and warn-only.
    """
    hard: list[tuple[str, str]] = []
    warn: list[tuple[str, str]] = []
    if uid == 0:
        hard.append(("not_root", "running as uid 0 — a root peer can read any secret"))
    if _cap_has(cap_eff, CAP_SYS_PTRACE):
        hard.append(
            ("no_cap_sys_ptrace", "CAP_SYS_PTRACE held — defeats PR_SET_DUMPABLE=0")
        )
    if _cap_has(cap_eff, CAP_SYS_ADMIN):
        hard.append(("no_cap_sys_admin", "CAP_SYS_ADMIN held — broad host access"))
    if seccomp != 2:
        warn.append(("seccomp_filter", f"no seccomp filter active (Seccomp={seccomp})"))
    return hard, warn
```

Note: imports `ctypes`, `Path` are unused in Task 1 — they are used by Task 2. To keep this task's `ruff` clean, add Task 2's functions in the same session; if committing Task 1 alone, temporarily drop the `ctypes`/`Path` imports and re-add them in Task 2 Step 2. (ponytail: prefer landing Tasks 1+2 together to avoid the import churn.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/security/test_gates.py -q`
Expected: PASS (6 passed)

- [ ] **Step 7: Commit**

```bash
git add src/ach_agent/security/__init__.py src/ach_agent/security/preflight.py tests/security/__init__.py tests/security/test_gates.py
git commit -m "feat(security): pure host-gate evaluator for boot preflight"
```

---

### Task 2: Class A enforce + real gate read + fail-closed runner

**Files:**
- Modify: `src/ach_agent/security/preflight.py` (append `_prctl`, `harden_self`, `check_gates`, `run_preflight`)
- Test: `tests/security/test_preflight.py`

**Interfaces:**
- Consumes (from Task 1): `parse_status`, `evaluate_gates`, all constants, `DEGRADED_ENV`.
- Produces:
  - `harden_self() -> None` — Class A enforce + verify; `sys.exit(1)` on any miss (no override); no-op+warn on non-Linux.
  - `check_gates() -> list[tuple[str, str]]` — reads real `/proc/self/status` + `os.getuid()`, logs soft warnings, returns hard failures; `[]` on non-Linux.
  - `run_preflight() -> None` — `harden_self()` then evaluate hard gates; `sys.exit(1)` unless `ACH_INSECURE_ALLOW_DEGRADED=1`.

- [ ] **Step 1: Write the failing tests**

Create `tests/security/test_preflight.py`:

```python
# SPDX-License-Identifier: Apache-2.0
import sys

import pytest

from ach_agent.security import preflight


@pytest.mark.skipif(sys.platform != "linux", reason="prctl is Linux-only")
def test_harden_self_sets_dumpable_and_no_new_privs():
    preflight.harden_self()
    assert preflight._prctl(preflight.PR_GET_DUMPABLE) == 0
    assert preflight._prctl(preflight.PR_GET_NO_NEW_PRIVS) == 1


def test_run_preflight_fail_closed_exits(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [("not_root", "uid 0")])
    monkeypatch.delenv(preflight.DEGRADED_ENV, raising=False)
    with pytest.raises(SystemExit) as exc:
        preflight.run_preflight()
    assert exc.value.code == 1


def test_run_preflight_degraded_override_does_not_exit(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [("not_root", "uid 0")])
    monkeypatch.setenv(preflight.DEGRADED_ENV, "1")
    preflight.run_preflight()  # must return without raising


def test_run_preflight_clean_host_does_not_exit(monkeypatch):
    monkeypatch.setattr(preflight, "harden_self", lambda: None)
    monkeypatch.setattr(preflight, "check_gates", lambda: [])
    monkeypatch.delenv(preflight.DEGRADED_ENV, raising=False)
    preflight.run_preflight()  # must return without raising
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/security/test_preflight.py -q`
Expected: FAIL — `AttributeError: module ... has no attribute '_prctl'` / `harden_self`

- [ ] **Step 3: Append the enforce + runner implementation**

Append to `src/ach_agent/security/preflight.py`:

```python
def _prctl(option: int, arg2: int = 0) -> int:
    """Thin ctypes wrapper over prctl(2). Returns the raw syscall result.

    SET options return 0 on success / -1 on error; GET options return the value.
    """
    libc = ctypes.CDLL(None)
    return int(libc.prctl(option, arg2, 0, 0, 0))


def harden_self() -> None:
    """Class A: set + verify dumpable=0 and no_new_privs=1. Hard-fail on any miss.

    Our own syscalls — a failure is a broken platform, not deploy policy, so there is
    NO override. Must run before any secret enters process memory.
    """
    if sys.platform != "linux":
        log.warning("preflight: non-Linux platform, process hardening skipped")
        return
    if _prctl(PR_SET_DUMPABLE, 0) != 0 or _prctl(PR_GET_DUMPABLE) != 0:
        log.error("preflight: PR_SET_DUMPABLE=0 failed to apply — refusing to start")
        sys.exit(1)
    if _prctl(PR_SET_NO_NEW_PRIVS, 1) != 0 or _prctl(PR_GET_NO_NEW_PRIVS) != 1:
        log.error("preflight: PR_SET_NO_NEW_PRIVS=1 failed to apply — refusing to start")
        sys.exit(1)
    log.info("preflight: process hardened", dumpable=0, no_new_privs=1)


def check_gates() -> list[tuple[str, str]]:
    """Read real host properties, log soft warnings, return the hard-gate failures."""
    if sys.platform != "linux":
        return []
    status = parse_status(Path("/proc/self/status").read_text(encoding="utf-8"))
    cap_eff = int(status.get("CapEff", "0"), 16)
    seccomp = int(status.get("Seccomp", "0") or "0")
    hard, warn = evaluate_gates(os.getuid(), cap_eff, seccomp)
    for name, detail in warn:
        log.warning("preflight: soft check", gate=name, detail=detail)
    return hard


def run_preflight() -> None:
    """Boot entrypoint: enforce Class A, then evaluate Class B gates (fail-closed).

    ACH_INSECURE_ALLOW_DEGRADED=1 downgrades Class B hard failures to warnings
    (local/dev only). It NEVER affects Class A.
    """
    harden_self()
    failures = check_gates()
    if not failures:
        log.info("preflight: host gates passed")
        return
    degraded_ok = os.environ.get(DEGRADED_ENV) == "1"
    for name, detail in failures:
        if degraded_ok:
            log.warning("preflight: DEGRADED — gate bypassed", gate=name, detail=detail)
        else:
            log.error("preflight: host gate failed", gate=name, detail=detail)
    if not degraded_ok:
        log.error(
            "preflight: refusing to start on an unsafe host — fix the securityContext "
            "or set ACH_INSECURE_ALLOW_DEGRADED=1 to override (dev only)"
        )
        sys.exit(1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/security/test_preflight.py -q`
Expected: PASS (4 passed on Linux; 3 passed + 1 skipped on non-Linux)

- [ ] **Step 5: Lint + type check the new module**

Run: `uv run ruff check src/ach_agent/security/ && uv run mypy --strict src/ach_agent/security/preflight.py`
Expected: no errors (all imports now used)

- [ ] **Step 6: Commit**

```bash
git add src/ach_agent/security/preflight.py tests/security/test_preflight.py
git commit -m "feat(security): enforce dumpable=0 + no_new_privs, fail-closed host gates"
```

---

### Task 3: Wire preflight into harness boot

**Files:**
- Modify: `src/ach_agent/main.py` (add import; call `run_preflight()` as first statement in `main()`)
- Modify: `tests/conftest.py` (set `ACH_INSECURE_ALLOW_DEGRADED=1` for the suite so `main()`-invoking tests never exit when CI runs as root) — create if absent
- Test: `tests/test_main_preflight.py`

**Interfaces:**
- Consumes: `run_preflight` from `ach_agent.security.preflight`.
- Produces: no new public symbols — behavioral wiring only.

- [ ] **Step 1: Write the failing wiring test**

Create `tests/test_main_preflight.py`:

```python
# SPDX-License-Identifier: Apache-2.0
import asyncio

import pytest

from ach_agent import main as main_mod


def test_main_runs_preflight_before_config(monkeypatch):
    calls: list[str] = []

    def fake_preflight() -> None:
        calls.append("preflight")

    def fake_load_config(_path):
        calls.append("load_config")
        raise RuntimeError("stop-after-config")

    monkeypatch.setattr(main_mod, "run_preflight", fake_preflight)
    monkeypatch.setattr(main_mod, "load_config", fake_load_config)

    with pytest.raises(RuntimeError, match="stop-after-config"):
        asyncio.run(main_mod.main())

    assert calls == ["preflight", "load_config"]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_main_preflight.py -q`
Expected: FAIL — `AttributeError: module 'ach_agent.main' has no attribute 'run_preflight'`

- [ ] **Step 3: Add the import to main.py**

In `src/ach_agent/main.py`, add to the import block (alongside the other `ach_agent.*` imports, ~line 53 near `from ach_agent.engine.sanitized_env import ...`):

```python
from ach_agent.security.preflight import run_preflight
```

- [ ] **Step 4: Call run_preflight() as the first statement in main()**

In `src/ach_agent/main.py`, `async def main(...)` currently begins (~line 813):

```python
    console_mode = tui_mode or debug_mode or one_shot_prompt is not None
```

Insert immediately above it:

```python
    # SEC: harden this process (dumpable=0 + no_new_privs) and refuse an unsafe host
    # BEFORE any secret (ek_/tokens) is read into memory. Fail-closed unless
    # ACH_INSECURE_ALLOW_DEGRADED=1. See security/preflight.py.
    run_preflight()
    console_mode = tui_mode or debug_mode or one_shot_prompt is not None
```

- [ ] **Step 5: Ensure the suite never hard-exits on a root CI runner**

If `tests/conftest.py` exists, add these lines near the top; otherwise create `tests/conftest.py` with:

```python
# SPDX-License-Identifier: Apache-2.0
import os

# Preflight (security/preflight.py) fails closed on an unsafe host. CI containers
# often run as root, which is a hard gate. Enable degraded mode for the whole suite
# so main()-invoking tests exercise wiring without sys.exit(1). Gate LOGIC is tested
# directly via evaluate_gates() in tests/security/.
os.environ.setdefault("ACH_INSECURE_ALLOW_DEGRADED", "1")
```

- [ ] **Step 6: Run the wiring test to verify it passes**

Run: `uv run pytest tests/test_main_preflight.py -q`
Expected: PASS (1 passed)

- [ ] **Step 7: Run the full suite to confirm no regressions**

Run: `uv run pytest -q`
Expected: PASS (no new failures; existing `main()` tests unaffected thanks to the conftest degraded flag)

- [ ] **Step 8: Commit**

```bash
git add src/ach_agent/main.py tests/conftest.py tests/test_main_preflight.py
git commit -m "feat(security): run boot preflight before secrets load in main()"
```

---

## Deployment note (not code — for the operator / docs)

The preflight *verifies* the deploy; it does not create it. Document the required shape so `ach-runtime` renders it and the gates pass without the override:

- **Kubernetes** `securityContext`: `runAsNonRoot: true`, `capabilities: { drop: ["ALL"] }`, `seccompProfile: { type: RuntimeDefault }`; leave `shareProcessNamespace` unset/`false`; optional `runtimeClassName: gvisor` for kernel-escape defense.
- **Docker**: `--user <uid>`, `--cap-drop ALL`, `--security-opt no-new-privileges`, default seccomp (do not pass `seccomp=unconfined`).

`ACH_INSECURE_ALLOW_DEGRADED=1` is dev-only; every bypassed gate is logged by name at WARN so an unsafe prod deploy is visible in logs.

---

## Self-Review

**1. Spec coverage** (against the conversation's converged design):
- Tier 1 `PR_SET_DUMPABLE=0` → Task 2 `harden_self`. ✅
- `no_new_privs` inherited by opencode → Task 2 `harden_self` (set on harness, inherited across fork+exec; no per-child `preexec_fn` needed). ✅
- Class B detect + fail-closed + override → Task 2 `check_gates`/`run_preflight` + Task 1 `evaluate_gates`. ✅
- Boot ordering (before secrets in RAM) → Task 3 (first statement in `main()`; `ek` is read far later at `main.py:844`). ✅
- Env-hygiene half (clean-slate child env) → **already implemented** in `lifecycle.build_opencode_env`; intentionally not re-done (ponytail: don't rebuild what exists). ✅
- Tier 2 UID-drop / `CAP_SETUID` → **intentionally out of scope** (user chose env-only + Tier 1; setuid needs a capability that hardened pods drop). Documented, not built.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". All code blocks complete. ✅

**3. Type consistency:** `evaluate_gates` returns `(list[tuple[str,str]], list[tuple[str,str]])` in Task 1 and is consumed identically in Task 2 `check_gates`. `_prctl`/constants referenced in Task 2 tests match Task 1/2 definitions. `run_preflight`/`load_config` monkeypatched on `ach_agent.main` in Task 3 match the imported names. ✅
