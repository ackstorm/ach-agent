# Channel cleanup hook design

**Date:** 2026-09-02  
**Status:** Approved  
**Compatibility:** Additive change to the `v1alpha1` CRD and agent config schema

## Goal

Let a channel release the session-owned resources created by
`channels[].prepare` when that session's engine is finally stopped. The first
use case is deleting a Git checkout under `ACH_WORKSPACE` so a long-lived Pod
does not accumulate one clone for every issue or merge request it has seen.

## User API

`cleanup` is one optional block beside `prepare`, not a list:

```yaml
spec:
  channels:
    - name: gitlab-review
      type: webhook
      prepare:
        script: |
          set -eu
          AUTH=$(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 -w0)
          export GIT_CONFIG_COUNT=1
          export GIT_CONFIG_KEY_0=http.extraHeader
          export GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"
          if [ ! -d "$ACH_WORKSPACE/repo/.git" ]; then
            git clone "$GITLAB_BASE_URL/$ACH_EVENT_PROJECT_PATH.git" \
              "$ACH_WORKSPACE/repo"
          else
            git -C "$ACH_WORKSPACE/repo" fetch --prune origin
          fi
        forwardEnv: [GITLAB_TOKEN, GITLAB_BASE_URL]
        timeoutSeconds: 120
      cleanup:
        script: |
          set -eu
          test -n "$ACH_WORKSPACE"
          rm -rf -- "$ACH_WORKSPACE"
        timeoutSeconds: 30
```

The operator-facing `cleanup` shape is identical to `prepare`:

- `script`: required, static shell text; `{{ }}` is not rendered.
- `forwardEnv`: optional set of names selected from the merged
  `AgentProfile.spec.env` and `ACHAgent.spec.env`.
- `timeoutSeconds`: optional integer from 1 through 3600; default 120.

`cleanup` requires `prepare`. This keeps `ACH_WORKSPACE` ownership explicit and
avoids silently changing channels without a workspace hook from the engine's
global work directory to a per-session directory.

The rendered ach-agent config resolves `forwardEnv` to the existing hook shape:

```json
{
  "cleanup": {
    "script": "set -eu\nrm -rf -- \"$ACH_WORKSPACE\"\n",
    "env": {"GITLAB_BASE_URL": "https://git.example.com"},
    "secretEnv": {"GITLAB_TOKEN": {"env": "ACH_SECRET_GITLAB_REVIEW_CLEANUP_GITLAB_TOKEN"}},
    "timeoutSeconds": 30
  }
}
```

An unknown `forwardEnv` name is ignored and remains unset. Prepare and cleanup
use independent allowlists and independent generated secret aliases.

## Lifecycle

For a session with `prepare` and `cleanup`, the harness performs:

1. Reserve the `session_key` in the engine pool and cancel any pending idle-TTL
   expiry before starting `prepare`.
2. Run `prepare` on the lane with the session workspace as its current working
   directory.
3. Acquire or reuse the session engine, with that workspace as the engine work
   directory.
4. Release the engine after the invocation.
5. If another event for the same session arrives before the idle TTL expires,
   cancel expiry and retain the engine and workspace.
6. When the final idle TTL expires, stop the engine and then run `cleanup`.

`idleTtlSeconds: 0` stops the engine and runs cleanup immediately after the
invocation. A prepare or engine-launch failure after session reservation runs
cleanup immediately. Graceful process shutdown stops every tracked engine and
runs every registered cleanup.

The pool's per-session lock serializes expiry/cleanup with new session
activity. If expiry has already started, a new event waits until cleanup ends
and then prepares a fresh workspace. If the new event reserves the session
first, it cancels expiry before prepare begins. Cleanup therefore cannot delete
a workspace while a new prepare script is using it.

## Cleanup execution contract

Cleanup receives the same isolated environment as prepare:

- `ACH_WORKSPACE`
- `ACH_SESSION_KEY`
- `ACH_EVENT_ID`
- `ACH_CHANNEL`
- validated scalar `ACH_EVENT_*` variables from the most recent event
- only the literal and secret variables selected by `cleanup.forwardEnv`
- the small runtime base environment already used by prepare

Cleanup runs through `/bin/sh -eu -s`; the script is sent on stdin and is never
written into the agent-readable workspace. Its current working directory is
the parent of `ACH_WORKSPACE`, allowing it to remove the complete workspace.
It uses the same process-group kill and stderr-tail redaction rules as prepare.

Prepare remains fail-closed. Cleanup is best-effort: spawn, timeout, or nonzero
exit increments `ach_agent_cleanup_failures_total{reason=...}` and emits a
warning, but never changes an already produced reply or raises into pool
release. Cancellation still kills and reaps the cleanup process group before
propagating `CancelledError`.

Cleanup scripts should be idempotent. The harness aims to invoke one registered
cleanup once, but cannot guarantee execution after `SIGKILL`, node loss, or
container-runtime failure. A failed cleanup is not retried automatically; a
later event for the session registers a new cleanup attempt.

## Application-owned resources

The harness has no Git-specific behavior. It does not choose between clone,
fetch, bare mirrors, or worktrees, and it never deletes a workspace unless the
configured cleanup script says to do so. `prepare` and `cleanup` are generic
shell lifecycle hooks; the agent configuration owns every resource operation.

The documented GitLab example uses one idempotent clone per active session:
prepare reuses and fetches the checkout on subsequent events for that session,
and cleanup removes it after the session goes idle. This bounds clone count by
active/warm sessions instead of all sessions observed during the Pod's
lifetime.

A deployment may instead implement a shared bare mirror plus per-session Git
worktrees entirely inside its prepare and cleanup scripts. That script is also
responsible for cross-session locking and stale-lock recovery around mirror
updates. No repository cache, Git command, lock manager, or automatic workspace
garbage collector belongs in ach-agent or the operator.

## Cross-repository surfaces

### ach-agent

- Add `channels[].cleanup` to the Pydantic contract and frozen JSON Schema.
- Share the safe shell execution machinery with prepare.
- Register cleanup ownership in `EnginePool` before prepare starts.
- Run cleanup on TTL expiry, immediate release, failed preparation/launch, and
  graceful shutdown.
- Add cleanup failure metrics, lifecycle/race tests, contract docs, and a Git
  checkout example.

### ach operator

- Add `ChannelSpec.Cleanup` using the existing `PrepareSpec` shape.
- Resolve cleanup `forwardEnv` from the merged profile/agent environment.
- Generate `ACH_SECRET_<CHANNEL>_CLEANUP_<NAME>` aliases for forwarded secrets.
- Render cleanup into the agent ConfigMap without secret plaintext.
- Regenerate deepcopy code, CRDs, Helm CRD copies, API reference, the field
  shape golden, and the vendored ach-agent JSON Schema.

## Verification requirements

Tests must prove:

1. Cleanup without prepare is rejected by both schemas.
2. Cleanup uses the same validated environment and secret isolation as prepare.
3. Exit, spawn, and timeout failures are observable and best-effort.
4. Cancellation kills and reaps cleanup children.
5. TTL zero, TTL expiry, preparation failure, launch failure, and graceful
   shutdown all invoke cleanup.
6. A new event before expiry cancels cleanup.
7. Cleanup already in progress is serialized before a new prepare starts.
8. Operator literal, secret, and missing-name rendering matches the contract,
   and secret plaintext never enters the ConfigMap.
9. Generated schemas, CRDs, Helm copies, API docs, examples, and field-shape
   golden agree across both repositories.
