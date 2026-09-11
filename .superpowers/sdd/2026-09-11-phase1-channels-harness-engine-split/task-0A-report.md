# Task 0A handoff report

Status: complete characterization and test-only handoff prototype. Commit `56054a1`
contains the fixtures and compatibility report; this follow-up tightens the prototype.

Files: `tests/test_private_prepare.py`, `docs/reports/phase1-prepare-compatibility.md`.
No production code or transport code changed.

Required validation:

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/test_private_prepare.py -q
42 passed, 3 xfailed in 3.87s
```

Initial red evidence:

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_private_prepare.py -q --runxfail
3 failed, 3 passed in 1.25s
```

The failures are exact reproduced risks: a planted workspace `.gitconfig` fsmonitor
executes during credentialed Git, a planted checkout `.git/config` fsmonitor executes
during credentialed Git, and a destination symlink allows a script write outside the
workspace. Markers are conditional on the synthetic credential in the first two cases.

The prototype now has two phases. Credentialed preparation uses fresh private HOME and
scratch checkout outside `workDir`, then exits. A second `run_prepare` performs only a
local fetch/checkout from that scratch path with no secret environment and no Git config
override; a planted target hook confirms it cannot observe the token. The target root
inode, `.ach-state`, untracked files, and local commit objects are retained while tracked
files follow the existing force-checkout behavior.

Warm expiry and cleanup lifecycle are already covered by named existing tests:
`tests/engine/test_pool.py::test_ttl_expiry_stops_engine_before_cleanup`,
`test_ttl_zero_runs_cleanup_immediately`, and `test_cleanup_failure_does_not_escape_release`;
prepare failure cleanup wiring is covered by
`tests/test_main_wiring.py::test_prepare_failure_discards_reserved_cleanup`. Task 0A's
new fixture separately verifies hook acquisition failure retains the workspace and that
successful cleanup removes the repo while retaining the workspace root.

Recommendation: Task 0B should publish only approved scratch checkout data after the
credential-bearing process exits, update the existing target Git repository with local
credential-free operations, recreate `.ach-state`, reject source/destination symlink
escapes, and preserve the existing root inode and retained files. This does not claim
same-UID isolation; split mounts and namespaces provide that boundary.
