# Unix split behavior matrix

This is the Task 1 characterization record for the simplification work. The reference
implementation is original v0.16.1 at `462912f`. The matrix's “Current split before
this plan” column is the historical pre-plan snapshot at `2ff1dd8`; later verification
attempts are listed separately below.

| Behavior | Original reference | Current split before this plan | Regression test / evidence | Planned task |
| --- | --- | --- | --- | --- |
| Stable workspace mapping | `workspace_dir(work_dir, session_key)` is stable and distinct per key (`boot.prepare.workspace_dir`) | Same mapping | `test_workspace_mapping_is_stable_and_keyed_by_session` with exact paths `/work/repo-42-a45d87f4` and `/work/repo-43-082624e0` | 3 |
| Repeated same-key prepare | Runs again in the populated checkout (`boot.prepare.run_prepare`) | Credentialed hooks use a fresh private path | `test_credentialed_prepare_keeps_original_workspace` | 3 |
| Prepare ordering | Prepare precedes acquire and turn | Existing runner delegates workspace preparation before acquire | Existing runner and `tests/boot/test_engine_runner_http.py` | 3 |
| Cleanup ordering | Cleanup follows stop; warm reuse defers cleanup until expiry/close | Cleanup coordination is credentialed-only in the split | `tests/boot/test_engine_runner_http.py`, `tests/test_private_prepare.py` | 3 |
| Prepare cwd and HOME | cwd is workspace; HOME is workspace (`boot.prepare.build_prepare_env`, `boot.prepare.run_prepare`) | Public hook path matches; private path diverges for secrets | `test_credentialed_prepare_keeps_original_workspace` | 3 |
| Cleanup cwd and HOME | cwd is workspace parent; HOME is workspace (`boot.prepare.run_cleanup`) | Existing shared path behavior is characterized | `test_cleanup_runs_from_workspace_parent_with_original_environment` | 3 |
| Event and selected environment | Base, configured, secret, and validated `ACH_EVENT_*` values are passed to hooks | Same for public hooks; credentialed hooks cross the private boundary | `tests/test_prepare.py` | 3 |
| Prepare failure and timeout | Fail closed; bounded timeout; no native turn | Existing failure path retained | `tests/test_prepare.py`, `tests/router/test_lane.py` | 3 |
| Best-effort cleanup failure | Logs/records failure and does not replace the original result | Existing cleanup failure handling retained | `tests/test_prepare.py`, `tests/boot/test_engine_runner_http.py` | 3 |
| Script-only payload lifecycle | Temporary workspace under configured `work_dir` receives newline-terminated JSON on stdin and is removed (`boot.prepare.run_webhook_script`) | Current path uses `/tmp/ach-private`, ignoring `work_dir` | `test_script_only_payload_uses_configured_workdir_and_is_removed` | 3 |
| Session modes | none/custom/default reuse, prompt rendering, repair and terminal behavior | Existing runner/session tests cover these paths | `tests/channels/test_tui.py`, `tests/engine`, `tests/boot` | 3/4 |
| Source ACK policy | Queue ACK follows original completion policy, including failure and full-queue cases | Existing queue consumer retains policy | `tests/channels/test_queue.py`, `tests/channels/test_internal_http.py` | 4 |
| Selected engine environment | E receives only selected H values, not managed credentials | Current split forwards names and relies on E ambient values | Task 2 red test specified in plan | 2 |
| Native process lifecycle | Acquire/turn/stream/cancel/stop confirmation remain observable | Existing execution API retains lifecycle over TCP | `tests/execution`, `tests/engine` | 4 |
| Internal transport | Not an original public behavior; target replaces TCP/bootstrap with two UDS endpoints | TCP/bootstrap currently present | Task 4 UDS tests and Task 5 mount checks | 4/5 |

## Reproducible evidence

The characterization module was copied into a task-owned checkout created on the project
filesystem at `.worktrees/compat-baseline-462912f`, with only the test/report commit
applied. This avoids `/tmp`'s small tmpfs and avoids concurrent source edits. The current
checkout remained at `2ff1dd8` while these counts were captured. The original source
functions named above were read directly with `git show 462912f:src/ach_agent/boot/prepare.py`;
the current implementations were read from `src/ach_agent/boot/prepare.py`.

Exact commands and results:

```text
baseline (.worktrees/compat-baseline-462912f, source 462912f):
rtk ./scripts/dev.sh uv run pytest tests/compat/test_original_split_behavior.py tests/test_prepare.py tests/router -q
81 passed in 3.96s

current (feat/phase1-split, source 2ff1dd8):
rtk ./scripts/dev.sh uv run pytest tests/compat/test_original_split_behavior.py tests/test_prepare.py tests/router -q
85 collected: 84 passed, 1 failed in 5.76s
```

The one current failure is intentional evidence for Task 3: the new script-only test
observes `/tmp/ach-private/webhook-script-*` rather than a child of the supplied
`work_dir`. It is not marked xfail, so a future runtime change must make it pass.

After the runtime correction changed script-only execution to the supplied `work_dir`,
the same focused command was rerun: **85 passed, 1 failed in 5.44s**. That remaining
failure is an existing `tests/test_prepare.py::test_webhook_script_survives_an_unpaired_surrogate`
expectation that the parent work directory disappears; the corrected implementation
removes the temporary child and leaves the configured parent directory present. This
late result is separate from the historical pre-plan matrix and was not changed in the
owned characterization files.

## Baseline command

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/router tests/channels tests/engine -q
```

The broader pre-Task-1 runtime command reported **612 passed, 3 skipped, 101 warnings**;
the focused command above is the reproducible parity gate for this matrix.
