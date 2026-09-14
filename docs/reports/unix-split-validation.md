# Unix split validation — 2026-09-14

## Startup hydration and generic operator storage

Branch `feat/operator-storage`, based on `1b240ef`. Luna implementers completed
the startup/storage plan; root reviewed the diffs, requested corrections, and ran
the final verification independently. The public agent schema is unchanged.

The harness now downloads into a temporary shared batch. The mini-harness installs
and validates files in Engine home, deletes the batch and only then becomes ready.
The execution API carries selected environment names; the operator or local launcher
supplies their values to Engine. Channel hooks keep shared workspace access and
their separate Harness HOME. Supported legacy workspace/native database data is
preserved by agent code, without tool-specific operator mounts.

Fresh verification:

- Full Docker pytest: **1,230 passed, 4 skipped**, 169 existing dependency warnings.
- Ruff check/format and strict mypy: passed for all **97 source files**.
- Generated schema check passed; schema/model diff against `1b240ef` is empty.
- MkDocs strict build and whitespace checks passed.
- Combined image built successfully. Final runtime image:
  `sha256:c2dd270ef2953a82920cda4e56fa491c74bd24edfee3dda4a1c1f1b33e108555`.
- On that image, real OpenCode and Pi each completed two events with the same
  native session, selected environment values and shared workspace hooks.
  Startup checks verified installed skills and consumed transfer batches before
  the first event. Killing Engine during a model request produced a failed result
  and Harness readiness 503; cancellation took two seconds for Pi.
  The final run used `scripts/test-split.sh` assertions with its build step omitted
  after tagging the already-built image for all roles.
- Harness-process restart passed with Channels/Engine container IDs unchanged,
  the new startup batch consumed, and a second event reusing native session
  `ses_f5ed2b7baffe1GCSbjvHqexkcT`. A test-only shell supervisor preserved Docker's
  shared network namespace while restarting the Harness Python process.
- A real Engine-role container rejected an unavailable native binary before any
  invocation and exited with status **1**. Discovery worked before init while
  public health returned 503.
- The nonpersistent Compose profile completed two events and passed role health
  checks and transfer cleanup. Native Pi and OpenCode TUI each completed two typed
  prompts in a PTY, resized and exited with status **0**; their stored native
  conversations contain two user and two assistant messages each.

Temporary-storage and TUI checks used image
`sha256:9e29ed4bcf343dd72864cb2febe032451466b2f39070deb2a0da8d7761780132`.
The only subsequent runtime change preserved top-level directory symlinks during
legacy workspace migration; this is covered by the final tests and does not run
on those fresh/standalone paths.

Root review also caught and corrected concurrent controller takeover during init,
empty hydration batches on retry, stale env/path assertions, migration markers
outside Engine mounts, and accidental dereferencing of repository symlinks.
One verification attempt ran out of disk; disposable ACH build images/cache were
removed and the full gate was rerun successfully. No data volumes were pruned.

Local logs: `/tmp/ach-startup-full-tests.log`, `/tmp/ach-startup-lint.log`,
`/tmp/ach-startup-compose-final.log`, `/tmp/ach-startup-restart.log`, and
`/tmp/ach-startup-tui-{pi,opencode}.log`.

No Kubernetes cluster rollout, live external model/MCP test, release or remote push
was performed. Native engines used controlled synthetic ACH/model endpoints.
Fresh PVC subpath creation and permissions remain a joint operator e2e check.

## Main integration

On 2026-09-14, local `main` fast-forwarded from `462912f` to `36f0304`, including
all 117 split-branch commits without conflicts. Two untracked older draft files
that occupied incoming paths were preserved under `/tmp/ach-main-premerge.ov8tnk/`;
other untracked files were left untouched. No remote push was performed.

Fresh verification after integration in the main checkout: **1,178 passed,
3 skipped**, Ruff check/format and strict mypy passed. Runtime behavior matches
the previously tested branch tree. The new operator mode document is a handoff
proposal; no ACH renderer or CR implementation was changed here.

## Bounded cleanup follow-up

Runtime revision: `d87894f`, following the previously validated `5b2e9e5` tree.
The [current split contract](../references/2026-09-14-three-role-split.md) replaces
the historical proposals linked below.

Changes: hooks use a lazy process-owned temporary HOME (mode 0700), distinct
from engine home and workspace. Prepare/cleanup working directories and lifecycle
remain unchanged. Retention is an internal 300-second constant; the frozen public
schema now matches original v0.16.1 exactly. The cleanup registry was renamed and
acceptance-only fixtures moved under `tests/integration/fixtures/split/`.
Session migration, session-ready acknowledgement, controller ownership and the
existing execution API remain intact.

Fresh verification on this runtime tree:

- Full Docker pytest suite: **1,178 passed, 3 skipped**, 161 dependency deprecation
  warnings. Command: `scripts/dev.sh uv run pytest tests/ -q --ignore=tests/e2e`.
- Ruff check/format and strict mypy: passed for all 96 source files.
- `scripts/gen_schema.py --check`: passed; diff of the frozen schema against
  `462912f` is empty.
- `scripts/test-split.sh`: passed with real OpenCode and Pi in three containers,
  controlled upstreams, two turns and native-session reuse, selected environment
  values, shared workspace hooks using the new HOME, active cancellation and
  engine failure. Cancellation took two seconds for each engine. Private mount
  assertions passed. Detailed local log: `/tmp/ach-split-cleanup-acceptance.log`.
- Persistent/ephemeral/acceptance Compose configuration checks, shell syntax,
  MkDocs strict build and whitespace checks passed.
- Gitleaks scan of the staged tracked-file snapshot: no findings.
  An initial whole-directory scan also included ignored dependency caches and
  archived local test artifacts, reporting 40 findings there (including an old
  local kind kubeconfig). Those files are not part of the tracked release tree.

Native TUI and harness restart were not rerun for this bounded cleanup; their
earlier revision-specific evidence remains below. No image publication, merge,
push or Kubernetes rollout was performed.

## Earlier split implementation evidence

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


## HTTP probes correction — 2026-09-14

The v0.16.3 correction replaces command probes with HTTP on C8080/H8090/E8081.
H/E TCP apps expose health routes only; internal APIs stay on Unix sockets.
Both placements require startup hydration installation and track engine loss.

Root verification, after Luna implementation and root review:

- Full suite: 1,225 passed, 4 skipped. Strict Ruff/format/mypy: 96 source files passed.
- Manifest contract: 11 passed. Schema unchanged; strict MkDocs build passed.
- Real Pi and OpenCode, three containers: two events reuse one native session;
  hydration installed and transfer batch removed before work; selected environment
  and shared-workspace hooks preserved. Health HTTP was queried from the mock
  upstream's separate network namespace. Private API paths on H/E health ports
  returned 404. Killing E made H and C readiness return 503 (1–2s cancellation).
- Engine before init: `/healthz` and `/readyz` return 503; private API over TCP
  returns 404. SIGTERM closes the role with exit 0.
- Final combined test image: `sha256:3482565d380123cf22c7573274d51cb4183600e3cf5624c7671b39747f578b2b`.
  Two standalone launchers shared a network with distinct public ports, both
  hydrated successfully, and their child health ports did not collide. Killing
  the engine child changed standalone readiness to 503 while liveness stayed 200.
- Real native Pi and OpenCode TUIs each received a model response and exited with
  code 0; the new listener did not hang shutdown.

Evidence logs: `/tmp/ach-http-health-tests-final.log`,
`/tmp/ach-http-health-lint-final.log`, `/tmp/ach-http-health-compose.log`,
`/tmp/ach-http-standalone-final.log`, `/tmp/ach-http-tui-pi.log`, and
`/tmp/ach-http-tui-opencode.log`. These tests use controlled synthetic ACH/model
upstreams. They do not establish Kubernetes rollout success. The operator must
validate its rendered probes and storage in its cluster.
