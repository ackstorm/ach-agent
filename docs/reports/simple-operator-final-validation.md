> Historical evidence for the revisions named below. The [current split contract](../references/2026-09-14-three-role-split.md) is authoritative; latest acceptance evidence is in [unix-split-validation](unix-split-validation.md).

# Simplified operator contract — final validation

Runtime and packaging snapshot: `929397a`, following forwarding `98393ad` and
bootstrap `a58c43c`. GPT-5.6-Luna implemented; root reviewed and independently
ran the repository gate and native terminal checks. Subsequent changes in this
increment are documentation only. All upstream services and credentials used
for these checks were synthetic. No image was published or external cluster
changed.

## Repository gate

Root ran the unchanged `scripts/pre-push-check.sh` in a clean local clone at
`929397a`, with `PRE_PUSH_BASE_REF=462912f`. It exited 0:

- 1,224 tests passed, 3 skipped, in 88.97 seconds.
- 18 conformance tests passed in 1.73 seconds.
- Ruff, formatting and strict mypy passed (97 source files).
- Gitleaks scanned 65 commits and found no leaks.

The complete log is in the ignored task scratch directory:
`.superpowers/sdd/2026-09-12-simple-operator-bootstrap/root-gate-929397a-disk.log`.
The first attempt stopped while downloading dev dependencies because the host's
2 GiB `/tmp` tmpfs was nearly full. Moving this task's clone onto the project
filesystem resolved that infrastructure issue; no gate or source was changed.
The only origin warning reflects the local clone source rather than GitHub.

Luna additionally ran the focused packaging/bootstrap/role suites: 47 passed.
Root previously ran 27 forwarding/native-env tests and 39 bootstrap/role tests
independently while reviewing Tasks 1 and 2.

## Packaged three-container execution

`scripts/test-split.sh` passed for real OpenCode and Pi using the ordinary
Compose definition plus its synthetic upstream fixture. Only H receives the
full config; no manually supplied C/E projections or internal HMAC settings
are required. Both engines completed two events with the same native session,
received E-side `DEBUG` and `CUSTOM_TOOL_TOKEN` values, and failed a held
invocation when E was killed. H readiness then returned 503.

The acceptance log is `/tmp/simple-bootstrap-compose-929397a.log`. Root read
the results and reviewed the script's held-inference barrier, native-session
lookup and native-process environment checks. HMAC is read inside H by the
test client, without exposing it in host shell arguments.

| Image | SHA256 |
| --- | --- |
| Harness | `16710e2644c20ba7b4e2b6d8e219fd14f8b7493a4d5710801388967039e253cb` |
| Channels | `1a996eca06998064f6d4278f6439a09a089c5554f8550459167698253b4eb8a5` |
| OpenCode engine | `7f1a52ed8e3601480bc518f52822ea0cbe52ebfc12159255602d19c956b4008f` |
| Pi engine | `4924e157cb5e42dc55a262d441399e5ddfc5fa02ab3b0446329355387e5292c8` |

Independent real OpenCode bootstrap and harness-process restart evidence is
recorded in [the bootstrap report](simple-bootstrap-root-validation.md): the
key and native session survived H restart while C/E stayed alive. That check
used a source-overlay diagnostic image, not the final packaging images above.

## Native local terminal regression

Root built the combined target from the same source:
`a2fc42b48b9c9d40afcabe5f54d965491a39cf1c43f81e99a766af65f9ad996d`.
Both real Pi and OpenCode ran through the image entrypoint with `--tui`,
using `docker run -it` and an actual PTY, not piped prompt input.

For each engine root typed two turns, resized the terminal to 32x100 through
`TIOCSWINSZ`, and observed `PHASE1_NATIVE_TUI_REPLY` for both turns. Pi's JSONL
contained one session with two user and two assistant messages. OpenCode's
SQLite database likewise contained one session with two messages of each role.
`/proc/1/cmdline` confirmed tini remained PID 1. Pi exited 0 with Ctrl-D;
OpenCode exited 0 with Ctrl-C. The fixture recorded two hydrations and five
model requests, all five carrying the expected synthetic authorization.

## Review and deployment limits

Root's packaging review corrected the E startup probe to exec against
loopback; a kubelet HTTP probe to the pod IP cannot reach a loopback listener.
The reviewed manifest uses two separate bootstrap `emptyDir` directories,
read-only C/E mounts, UID/GID/fsGroup 10001, role arguments that preserve tini,
and no service-account token. Manifest tests cover those boundaries.

The final Kubernetes manifest was not reapplied to kind in this increment;
earlier phase-one Kubernetes evidence remains separately identified. Production
Deployment rendering and image publication remain integration work outside
this local branch. `/tmp/to-ach.md` is the self-contained operator handoff,
also tracked as the simplified operator specification.

All task-owned acceptance/terminal containers and networks were removed.
The existing unrelated memory services were left running. Phase-two proxy
masking and later queue/autoscaling/S3 work remain deferred.
