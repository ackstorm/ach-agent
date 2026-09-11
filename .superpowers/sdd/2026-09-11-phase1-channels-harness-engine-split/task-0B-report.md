# Task 0B — private preparation correction

## Validation

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/test_private_prepare.py -q
48 passed in 3.32s

rtk proxy ./scripts/dev.sh uv run pytest tests/config -q
133 passed in 0.54s

rtk proxy ./scripts/dev.sh uv run ruff check src/ach_agent/boot/private_prepare.py src/ach_agent/boot/prepare.py src/ach_agent/boot/paths.py tests/test_private_prepare.py tests/test_prepare.py
All checks passed! (repository wrapper emits the existing PLW1514 preview warning)
```

## Behavior

Hooks with `secretEnv` run in a fresh mode-0700 harness scratch directory with separate
mode-0700 HOME and checkout directories. Git system/global configuration is disabled for
the hook, and `ACH_WORKSPACE` points at the private checkout. Scratch is removed by the
temporary-directory cleanup path on success and failure.

After the hook exits, `$ACH_WORKSPACE/repo` must be a regular Git checkout. Source symlinks,
special files, destination symlinks and configured secret values in regular working-tree or
reachable Git object content fail closed before publication. The harness creates a short-lived
local Git bundle,
then uses a separate credential-free Git environment to fetch and force-check out the
prepared HEAD in the stable target repository, including source `origin/*` tracking refs used
by `git diff` and `git merge-base`. Existing target `.git` state, untracked files, local
objects and workspace inode remain in place; the bundle is removed in `finally`.

Credential-free prepare and cleanup retain current workspace behavior. Credentialed cleanup
runs only in fresh private state and cannot inspect or delete the engine workspace, so hooks
that depend on the old credential-bearing workspace contract fail closed. `_event_value`
validation remains unchanged.

## Safety limits

Same-UID local execution still permits a concurrently malicious engine to observe or modify
private scratch. Split deployment must provide private mounts and namespaces. The handoff
supports the documented Git checkout contract; arbitrary non-Git output is rejected rather
than copied by a generic recursive sanitizer.
