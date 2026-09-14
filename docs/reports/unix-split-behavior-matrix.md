# Unix split behavior matrix

Reference: original v0.16.1, `462912f`. This matrix records the implemented
simplification against that reference, rather than treating intermediate split
decisions as original behavior. Final commands, source revisions and runtime
evidence are recorded in [Unix split validation](unix-split-validation.md).

| Behavior | Original contract retained | Evidence |
| --- | --- | --- |
| Workspace identity | Existing `workspace_dir(work_dir, session_key)` formula | Characterization pins `/work/repo-42-a45d87f4` and `/work/repo-43-082624e0` |
| Repeated preparation | The populated same-key checkout survives; prepare runs again there | Baseline/current characterization; Compose retains `retained` and records two prepare runs in one directory |
| Prepare environment | cwd and HOME are workspace; selected env, secretEnv and validated event values retain their original meaning | `tests/compat`, `tests/test_prepare.py`; real H hook assertions with synthetic secret |
| Cleanup environment | cwd is workspace parent, HOME remains workspace; failures are best-effort | Characterization writes a sentinel before deliberately failing cleanup |
| Hook ownership and ordering | H executes hooks, after reservation and before native acquire; stop precedes cleanup; warm reuse defers cleanup | Wiring tests observe H `run_prepare`; real HTTP registry/expiry/cleanup-ACK tests |
| Preparation failure | No native acquire after failed H prepare; reservation is canceled | `test_h_prepare_failure_discards_reserved_cleanup`, original hook timeout tests |
| Script-only execution | H temporary directory under configured workDir; newline-terminated stdin payload; removed afterward; no native acquire | Baseline/current characterization and main wiring tests |
| Explicit engine environment | Original name selection/sanitization; selected H values reach native children | Role/wire/env tests; real Pi/OpenCode process inspection with values absent from E environment |
| Native session reuse | Separate lane/workspace and conversation keys; none/auto/custom policy retained; native map retained/imported | Runner/session/state suites; real two-turn Pi/OpenCode session continuity |
| Multi-turn behavior | Turn budgets, terminal repair, usage, text/tool events and session maintenance remain | Existing `tests/boot`, `tests/engine`, `tests/execution` suites |
| Source ACK policy | Existing Redis consumer behavior unchanged | Relative to `462912f`, `channels/queue.py` changes only its source-config type annotation; queue tests retained |
| Admission | Existing dedup, backpressure, FIFO and finite bounds; no replacement queue | Existing router and conformance suites; no router changes in this simplification |
| Native TUI | Inherited terminal, two native turns, resize, normal exit | Real PTY tests of Pi and OpenCode, native session files and exit status |

## Placement and transport changes

These are deliberate consequences of the split, not features of original v0.16.1:

- C uses the existing admission/result API on `channel.sock`; H uses the existing
  execution API on `agent.sock`. HTTP JSON/NDJSON is retained; independent streams
  and cancellation connections are retained.
- H reads the private full YAML. C fetches typed source inputs; E receives typed
  public controller/acquisition configuration. There are no generated bootstrap
  files or internal HMAC keys.
- Explicit stop acknowledgement carries the pool's former in-process cleanup
  callback across IPC. Useful result correlation, bounded retention, ownership
  and native-process supervision are retained.
- Native home and H state are separate mounts; H/E share workspace and public
  context. `.ach-state` links directly to the shared public context, so H does not
  need access to E's private home.
- A broken IPC connection can fail accepted work. The in-memory result registry
  does not make acceptance durable or external effects exactly-once.

The original runner allowed two lane keys to render the same custom conversation
key; it did not provide a cross-lane conversation lock. The earlier split added
`ConversationLocks`, covered by `tests/test_conversation_ownership.py`. That useful
serialization remains: the mapping policy is unchanged, but overlapping access to
one native conversation is serialized. It was not added or removed by this
simplification. This is distinct from proving that two separate OpenCode servers
can operate concurrently on one conversation; the native reuse acceptance uses
one stable lane and conversation key.

No new repository reset, private clone, bundle handoff, engine-side hook execution,
Git policy, queue, transport framework, masking or autoscaler remains in this phase.
Original credential-bearing hooks still consume a shared, agent-writable checkout;
container placement does not make its Git configuration trustworthy.

## Baseline characterization record

The same five characterization tests were run in a task-owned original checkout
at `462912f` and the current split. The original focused command was:

```sh
rtk proxy ./scripts/dev.sh uv run pytest \
  tests/compat/test_original_split_behavior.py tests/test_prepare.py tests/router -q
```

The baseline focused run passed 81 tests. The intermediate split run at `2ff1dd8`
passed 84 and failed one: script-only work used `/tmp/ach-private` instead of a
temporary child of configured workDir. The implementation and the stale assertion
that deleted the parent workDir were corrected. These historical failures are
not xfails and are not treated as final validation evidence.

The characterization tests prove their named observable behaviors, not equivalence
for every possible repository, operator script or native-engine version. See the
validation report for the broader suite and actual container checks.
