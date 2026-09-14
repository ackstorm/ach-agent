# Unix split validation — 2026-09-14

Branch: `feat/phase1-split`, worktree `.worktrees/phase1-split`.
Behavioral reference: v0.16.1 (`462912f`). Scope: complete the approved
[preserve-behavior simplification plan](../superpowers/plans/2026-09-14-preserve-behavior-simplify-split.md).
Luna agents implemented the changes; the root agent reviewed the diffs, requested
corrections and ran the final gate and native terminal acceptance independently.

## Automated gates

Source `4869d98`, clean local clone on the project filesystem:

```sh
rtk proxy bash -c 'PRE_PUSH_BASE_REF=462912f bash scripts/pre-push-check.sh'
```

The script was not modified. Exit status 0:

- Ruff check and format: passed, 96 source files.
- Strict mypy: passed, 96 source files.
- Main suite: **1176 passed, 3 skipped**, 59.31 seconds.
- Separate conformance run: **18 passed**, 1.71 seconds.
- Gitleaks: no leaks in the branch's commit range.
- Tracked file size, sensitive filename and SPDX checks passed.

The gate intentionally excludes `tests/e2e`, as before. Native/container evidence
below was run separately with a controlled upstream. Existing deprecation warnings
remain; the local clone's non-GitHub origin produces the script's expected warning.

The original-behavior focused command passed **86 tests** on `4869d98`:

```sh
rtk proxy ./scripts/dev.sh uv run pytest \
  tests/compat/test_original_split_behavior.py tests/test_prepare.py tests/router -q
```

The same characterization module was first checked against `462912f`; its original
focused suite passed 81 tests. The [behavior matrix](unix-split-behavior-matrix.md)
separates original invariants from the new IPC failure modes.

## Three-container acceptance

The complete `scripts/test-split.sh` acceptance passed on pinned source `4869d98`
with **real OpenCode 1.17.11 and Pi 0.82.0**, using the synthetic ACH/model fixture
in `tests/integration/fixtures/upstream.py`. The fixture does not contact a real
provider or use production credentials.

```sh
rtk proxy bash scripts/test-split.sh
```

For each engine, the run verified:

- C/H/E startup and readiness using the actual mounted Unix sockets.
- C can connect through its read-only IPC mount but cannot unlink H's socket;
  C cannot see `agent.sock`, and E cannot see `channel.sock`.
- Two distinct events with the same logical workspace key and configured custom
  conversation key complete with `PHASE1_SPLIT_REPLY`.
- The native session reference is identical across those events; upstream message
  history grows on the second turn.
- H's prepare runs twice with the expected cwd, HOME and synthetic credential;
  H and E observe the same retained workspace file and `xx` counter.
- Native child processes receive H-selected `forwardEnv` values that are absent
  from E's ambient environment.
- Model calls are authorized by H's proxy; the fixture records no unauthorized call.
- Killing E during a deliberately held model request yields a failed invocation
  and H readiness 503. Measured cancellation took 2 seconds for OpenCode and
  1 second for Pi.

Measured startup was 6 seconds for each engine on this host.
These measurements are observations, not performance guarantees.

| Image from source `4869d98` | Local content digest |
| --- | --- |
| H | `sha256:37304a5995732b4fcde4f7c0da1dfb88400c5dce2af93149b54b2f80588b839b` |
| C | `sha256:2262d11cf31351d5ea17c2a2e8474475bbef67c32db6d965907dbd57bf24a524` |
| E OpenCode | `sha256:b5178233664ed15795a4af6aa0b7cf051bc1596673a0c624f9455c71de89526c` |
| E Pi | `sha256:333d1329c3e375d2dc27ad5333920b02aec84dc8d748947d259a532ccdbb1d57` |

Test-only commit `ad1eabe` adds actual private-mount assertions to the same
acceptance script. Its full Pi/OpenCode rerun also passed: H's private-state
sentinel is absent in E, E's native-home sentinel is absent in H, E has no full
config or ambient `ACH_TOKEN`, and E cannot write the shared public context.
The script still proves the workspace is genuinely shared using the H-created
retained file and prepare counter. No runtime code or manifest changed in this
test-only follow-up; its evidence is `acceptance-ad1eabe.log`.

The ephemeral profile was also built from `4869d98` with an explicit OpenCode
target before startup. C/H/E started with the `/tmp` workspace/public-context
mounts and one real model event (`ephemeral-one`) completed. No service was
replaced during that check. The project and its volumes were then removed.
Its log records the actual image digests and completed state in
`ephemeral-4869d98.log`.

H process restart also passed with C and E kept alive. A test-only shell supervisor
restarted `python -m ach_agent.main --role harness` inside the same H container;
the test sent SIGTERM to that Python process, not to Docker's network owner.
The definitive OpenCode run (`acceptance-h-restart-pid.log`) asserts:

- H's PID file changed from **7 to 64**, and upstream hydration count reached 2
  before polling the new H readiness endpoint.
- C and E container IDs were unchanged before/after restart.
- A second event completed using native session
  `ses_f60ce02d1ffegmG1lcJeh27TGG`, the same reference as the first event.
- The subsequent held-call/E-kill check still produced failed work and H readiness 503.

An earlier run (`acceptance-h-restart.log`) also completed two-turn restart flows
for OpenCode and Pi with two hydrations and reused native references; the explicit
PID/container-ID assertions above were then added for the final OpenCode check.
The temporary supervisor helper and all of its Compose resources were removed.

## Real native TUI acceptance

The root agent built the combined image from `3cc9be8`, digest
`sha256:32558c55aab5d3a2bc38a0b0dbd1195855cfa10b81b8c57eacf6fe6d793f0a1a`.
Both tests used `docker run -it ... --tui` in an actual PTY, not piped input,
with the same synthetic upstream and separate engine-specific configurations.

| Check | Pi | OpenCode |
| --- | --- | --- |
| Two typed prompts and streamed replies | Both returned `PHASE1_SPLIT_REPLY` | Both returned `PHASE1_SPLIT_REPLY` |
| Resize | `stty -F /dev/pts/0 rows 32 cols 100`, interface reflowed | Same, interface reflowed |
| Native continuity | One JSONL contains two user and two assistant messages | SQLite contains two user and two completed assistant messages under one session ID |
| Native reference | `01a09f20-ecaf-7e63-9398-e078979b9091` | `ses_f60de58e1ffeBYaOZz8xX6kGlI` |
| PID 1 | `/usr/bin/tini -- python -m ach_agent.main --tui` | Same |
| Exit | Ctrl-D, container exit 0 | Ctrl-C, container exit 0 |

The upstream recorded two hydrations, five authorized model calls (including
OpenCode's title request) and zero unauthorized calls. The later changes covered
by the final automated gate remove unused scratch plumbing, narrow configured
driver typing, correct source-config construction and update tests/fixtures;
they do not change the native terminal code exercised here.

## Review corrections and deletion audit

Review and real execution found and corrected stale bootstrap startup, incorrect
probe destinations, root-owned socket-directory chmod, missing C startup retry,
the cross-container `.ach-state` link, an orphan cleanup acknowledgement for
prepare-only channels, deferred configuration typing and terminal failure/exit
handling. One failed native reuse run was a test fixture error: it sent different
workspace keys while claiming same-key reuse. The fixture now supplies stable
repository/issue identity and distinct event IDs; session policy was not changed.

| Removed | Preserved responsibility |
| --- | --- |
| `boot/bootstrap.py` and role artifact readers/writers | Allowlisted public projection, typed controller-open initialization, config bounds and secret tests |
| Internal `channels/signing.py`, nonce/MAC machinery | Source webhook authentication, event/result IDs, bounded retention and retry semantics |
| Ordinary internal TCP selection | Existing HTTP JSON/NDJSON API on two Unix endpoints; separate cancellation/stream/cleanup connections |
| E-side prepare/cleanup payloads and bundle import/export | All original hooks execute in H on the shared workspace |
| Mandatory private Git/clone handoff and unused scratch mount | Useful cleanup registry, stop notifications/ACKs and warm-expiry ordering |

Tests tied only to deleted signing/bootstrap/bundle mechanics were removed or
replaced. Substantive lifecycle tests were retained or migrated to the current
boundary, including real HTTP slow-cleanup, cancellation, waiter and warm-expiry
tests. No production Router or Redis consumer policy changes were made by this
simplification. The original-branch Router additions are result notification across
the new C/H boundary, not a new queue or scheduler.

## Configuration and deployment limits

- No public schema change relative to the pre-simplification split (`929397a`),
  verified by diff of `config/schema.py` and the frozen JSON schema. Relative to
  original v0.16.1, the earlier split already added optional
  `limits.resultRetentionSeconds` (default 300); existing configs remain valid.
- H alone mounts the full config. C resolves its source credential references
  from C's environment. H sends only selected eligible engine env values to E.
- Native home and H state remain private; workspace is shared read/write and
  public hydration context is H-write/E-read. Existing credential-bearing hooks
  still trust the shared checkout; this refactor does not harden Git hooks/config.
- One active pod/replica, open shared egress, in-memory acceptance/results. No
  durable queue, exactly-once effects, per-session security boundary or masking
  was added. MCP integration is covered by the adapter/proxy tests; the real
  native runs above exercise the model proxy, not a live external MCP service.
- The Kubernetes manifest was checked structurally; no actual cluster rollout,
  external ACH operator change or image publication was performed.

Local detailed logs are retained under
`.superpowers/sdd/2026-09-14-preserve-behavior-simplify-split/`, including
`root-gate-4869d98.log`, `acceptance-4869d98.log`, `ephemeral-4869d98.log`,
`acceptance-h-restart-pid.log`, `root-native-pi.log` and `root-native-opencode.log`.
Native session artifacts were retained for review. All task-owned runtime
containers, networks and volumes were removed; unrelated memory services were
left running. The working branch and evidence directories are preserved.

README diagrams, the specification, behavior matrix and the self-contained operator
handoff have been updated. `/tmp/to-ach.md` is a copy of that handoff for the other
host. `mkdocs build --strict` and `git diff --check` pass. Documentation-only closure
commits do not alter the runtime tree tested at `4869d98`.
