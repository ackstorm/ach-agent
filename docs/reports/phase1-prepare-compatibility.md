# Phase 1 preparation compatibility (Task 0A / Task 10A closeout)

Status: the private producer handoff is implemented and the compatibility contract is
updated. Credential-bearing hooks must return a fully materialized checkout; the producer
sets `GIT_NO_LAZY_FETCH=1` while creating its bundle, so an incomplete promisor checkout
fails closed with guidance to fetch missing objects before the hook exits. Complete
checkouts preserve history, `HEAD`, the existing workspace root and session state. The
split transport remains outside this report.

## Files and validation

Added `tests/test_private_prepare.py`. It uses a local Git repository and synthetic
credential, and checks first checkout, warm reuse, root inode, `.ach-state` resolution,
HEAD, tracked and untracked contents, local commit objects, cleanup, failed acquisition,
and a destination symlink fixture. Two hostile-input tests plant both workspace
`.gitconfig` and checkout `.git/config` settings for `core.fsmonitor` and `core.hooksPath`.

The Task 0A characterization command passed:

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/test_private_prepare.py -q
42 passed, 3 xfailed in 3.87s
```

The xfails are strict and represent the current reproduced vulnerability. Running the
same tests with `--runxfail` gave 3 failures: both planted fsmonitor commands touched
their outside-workspace marker during `git status`. The global case also emitted Git's
`warning: Empty last update token`, confirming that the planted configuration was read;
the third failure demonstrated destination symlink traversal.
This is a regression harness, not a claim of protection from a malicious same-UID
process.

## Observed behavior

The existing reference-style hook cloned a fresh checkout, then on the second event ran
`git fetch` followed by `git checkout --detach origin/main`. The workspace directory's
inode and `.ach-state` link remained stable. The force checkout replaced a dirty tracked
file with the remote version. An untracked file remained, and the local commit object
remained addressable even though it was no longer `HEAD`. A successful cleanup hook
removed `$ACH_WORKSPACE/repo` while retaining the workspace directory and its inode.
An acquisition exit raised `PrepareFailed` (fail closed) and retained the workspace;
cleanup is best effort, so a failing cleanup does not remove the checkout by itself.

These are the compatibility boundaries: preserve the session workspace root, hydration
link, untracked files, and local Git objects; preserve the current reference hook's
intentional replacement of tracked files when the event selects a new revision. A
handoff that replaces the whole workspace or `.git` directory would silently lose
retained state and is rejected.

## Private producer handoff contract

Use a harness-private scratch checkout for every credential-bearing clone/fetch. Give it
a fresh private `HOME`, cwd, Git config, and checkout path. Set the authorization header
with `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0=http.extraHeader`/`GIT_CONFIG_VALUE_0=...`; never
put the token in a remote URL. Do not import `.git/config`, `.gitconfig`, hooks, or helper
state from the agent workspace.

The operator hook must fully materialize the checkout before returning. Filtered or
promisor clones may be used only if the hook fetches all reachable objects while its
credential is available; otherwise the producer rejects the handoff offline. This adds
download and scratch-storage cost for historical blobs, including blobs unrelated to the
selected `HEAD`, in exchange for preventing a post-hook unauthenticated fetch. The active
reference script in `docs/schemas/operator-contract.md` therefore uses complete clone and
fetch commands.

After the prior engine writer has stopped, hand off only approved repository data. The
executable test `test_private_scratch_prototype_preserves_target_retention` runs this
algorithm against the same warm-reuse fixtures:

1. For a new session checkout, create `$ACH_WORKSPACE/repo` from the private scratch
   checkout using a local, credential-free Git operation.
2. For a warm session, update the existing target repository from scratch using a local
   bundle or local fetch, then perform the same detached checkout operation as the
   current script. Leave the target's existing untracked files and local refs/objects in
   place; do not delete or replace the target `.git` directory.
3. Recreate `.ach-state` from the harness-owned hydration location. Never copy it from
   scratch or input. Before each destination write, reject escaping paths, refuse source
   symlinks, and refuse destination symlink traversal.
4. Run cleanup with its own private HOME/cwd/config. A failed cleanup remains best effort
   and must not trigger a whole-workspace deletion policy.

The test runs credentialed scratch preparation to completion first, then a separate
credential-free transfer hook from a scratch path outside `workDir`. The transfer hook
receives no token and the planted target fsmonitor hook confirms that fact. This algorithm
keeps the existing workspace path and inode, retains unrelated files and local objects,
and makes the credential-bearing Git process independent of agent-planted configuration.
A generic recursive copy is not sufficient because it can follow symlinks and discard
`.git` retention semantics.

Task 10A also covers the authenticated filtered-clone regression: a complete clone and
handoff succeeds with synthetic Basic auth, while an incomplete filtered clone fails during
offline bundle production without an unauthenticated request or credential in the public
workspace/artifact. The root diagnostic fixture is intentionally ignored and is not a
production service.

## Concerns for implementation

The current single-container same-UID deployment still cannot prevent a concurrent agent
from observing or modifying private scratch files. The correction removes contaminated
Git input and credential-bearing use of the agent checkout; process and mount isolation
belongs to the split deployment. A private scratch implementation must also define the
writer-stop boundary and lock it with the existing session lifecycle.
