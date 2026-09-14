# Unix split behavior matrix

This is the Task 1 characterization record for the simplification work. The reference
implementation is original v0.16.1 at `462912f`; current results are recorded against
the `feat/phase1-split` runtime before refactoring.

| Behavior | Original reference | Current split before this plan | Regression test / evidence | Planned task |
| --- | --- | --- | --- | --- |
| Stable workspace mapping | `workspace_dir(work_dir, session_key)` is stable and distinct per key | Same mapping | `tests/compat/test_original_split_behavior.py` | 3 |
| Repeated same-key prepare | Runs again in the populated checkout | Credentialed hooks use a fresh private path | `test_credentialed_prepare_keeps_original_workspace` (expected current failure) | 3 |
| Prepare ordering | Prepare precedes acquire and turn | Existing runner delegates workspace preparation before acquire | Existing runner and `tests/boot/test_engine_runner_http.py` | 3 |
| Cleanup ordering | Cleanup follows stop; warm reuse defers cleanup until expiry/close | Cleanup coordination is credentialed-only in the split | `tests/boot/test_engine_runner_http.py`, `tests/test_private_prepare.py` | 3 |
| Prepare cwd and HOME | cwd is workspace; HOME is workspace | Public hook path matches; private path diverges for secrets | New compatibility test plus `tests/test_prepare.py` | 3 |
| Cleanup cwd and HOME | cwd is workspace parent; HOME is workspace | Existing shared path behavior is characterized | `test_cleanup_runs_from_workspace_parent_with_original_environment` | 3 |
| Event and selected environment | Base, configured, secret, and validated `ACH_EVENT_*` values are passed to hooks | Same for public hooks; credentialed hooks cross the private boundary | `tests/test_prepare.py` | 3 |
| Prepare failure and timeout | Fail closed; bounded timeout; no native turn | Existing failure path retained | `tests/test_prepare.py`, `tests/router/test_lane.py` | 3 |
| Best-effort cleanup failure | Logs/records failure and does not replace the original result | Existing cleanup failure handling retained | `tests/test_prepare.py`, `tests/boot/test_engine_runner_http.py` | 3 |
| Script-only payload lifecycle | Temporary workspace receives newline-terminated JSON on stdin and is removed | Existing webhook-script path retained | `tests/test_prepare.py`, `tests/channels/test_webhook.py` | 3 |
| Session modes | none/custom/default reuse, prompt rendering, repair and terminal behavior | Existing runner/session tests cover these paths | `tests/channels/test_tui.py`, `tests/engine`, `tests/boot` | 3/4 |
| Source ACK policy | Queue ACK follows original completion policy, including failure and full-queue cases | Existing queue consumer retains policy | `tests/channels/test_queue.py`, `tests/channels/test_internal_http.py` | 4 |
| Selected engine environment | E receives only selected H values, not managed credentials | Current split forwards names and relies on E ambient values | Task 2 red test specified in plan | 2 |
| Native process lifecycle | Acquire/turn/stream/cancel/stop confirmation remain observable | Existing execution API retains lifecycle over TCP | `tests/execution`, `tests/engine` | 4 |
| Internal transport | Not an original public behavior; target replaces TCP/bootstrap with two UDS endpoints | TCP/bootstrap currently present | Task 4 UDS tests and Task 5 mount checks | 4/5 |

## Baseline command

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/router tests/channels tests/engine -q
```

Result before Task 1 changes: **612 passed, 3 skipped, 101 warnings**.

The expected failure in the new credentialed-workspace test is intentionally not marked
as an xfail: the matrix records it as an implementation difference, and Task 3 must make
the test pass.
