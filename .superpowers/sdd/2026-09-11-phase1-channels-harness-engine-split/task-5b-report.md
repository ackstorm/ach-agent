# Task 5B report: actual owned process cleanup

## Result

`ManagedServer.stop()` now creates one retained stop task and all concurrent callers await that
task. The process handle and ownership observations remain available until cleanup has been
confirmed; the HTTP client and reserved port are released from the joined operation.

On Linux, each launched native process is observed through `/proc` ancestry. Ownership records
include the PID and `/proc/<pid>/stat` start time, so a PID observed with a different start time
is skipped. This is an observation-time PID-reuse guard; the ordinary `os.kill` call still has a
kernel scheduling TOCTOU window, and this increment does not claim pidfd-atomic signalling.
Descendants discovered while the leader is alive remain tracked after reparenting. On Linux, the
new per-launch `process_supervisor` sets `PR_SET_CHILD_SUBREAPER` before spawning the native
engine and remains alive after the engine leader exits, making rapid fork/setsid orphans direct
owned descendants instead of relying on an arbitrary parent lifetime. Cleanup uses SIGTERM with a
bounded wait, then descendant-first SIGKILL and a second bounded confirmation; an unresolved live
process raises `OwnedProcessCleanupError`, which flows through the existing strict pool and
execution-service unhealthy/shutdown path.

Native macOS mode remains portable through the existing private-session process-group fallback.
It verifies that the process group ID equals the launched leader PID, then signals that group.
Because native macOS mode has no procfs ancestry table, a detached descendant that is already
reparented cannot be attributed safely; that is an explicit native-mode containment limit. Linux
isolated engine containers rely on the container PID namespace and the mini-harness-owned tree;
the per-launch helper is the only new subreaper call and it is process-local. No kernel hardening
is added. `tini` installation and role entrypoint wiring remain Task 9/Task 8 work.
When the native leader exits, the helper performs its own bounded descendant TERM/KILL/reap
cycle and exits only after no adopted children remain; it therefore cannot hold Pi stdio open or
present a dead native leader as a healthy execution.

Pi immediate startup exit now joins `ManagedServer.stop()` before reporting `NativeLaunchFailed`.

## Acceptance coverage

`tests/engine/test_process_cleanup.py` launches real Python processes. The fast fixture forks,
setsid's, closes standard descriptors, and exits the leader immediately. The leader starts a
detached grandchild that holds a workspace file, writes its PID, and exits first. Tests verify
the orphan receives termination and dies, concurrent stop callers wait through forced process
death, and stopping one server leaves an independent server tree alive. A service-level
controller-release test uses a real native process and verifies a new controller is accepted only
after cleanup. An unrelated `sleep` process is also kept alive and is cleaned by the test teardown.

## Verification

- `rtk proxy ./scripts/dev.sh uv run pytest tests/engine/test_process_cleanup.py tests/engine/test_lifecycle.py tests/engine/test_opencode_driver.py tests/engine/test_pool.py tests/engine/pi/test_driver.py tests/execution/test_service.py tests/execution/test_http.py -q` — **166 passed**.
- `rtk proxy ./scripts/dev.sh uv run ruff check src/ach_agent/engine/lifecycle.py src/ach_agent/engine/process_supervisor.py src/ach_agent/engine/pi/driver.py tests/engine/test_process_cleanup.py tests/engine/pi/test_driver.py tests/execution/test_service.py` — **passed**.
- `rtk proxy ./scripts/dev.sh uv run mypy src/ach_agent/engine/lifecycle.py src/ach_agent/engine/process_supervisor.py src/ach_agent/engine/pi/driver.py` — **passed**.
- The requested repository-wide `ruff check src tests` command remains outside this task's clean
  gate because it reports 43 errors in unrelated test files. The requested repository-wide
  `mypy src` command reports 42 errors, including existing stats test files and two phase-1
  `main.py` completion-port seam errors. No new errors were reported for the changed modules;
  changed-source Ruff and mypy commands above pass.
