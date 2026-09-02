# Channel-owned repo workspace (replacing the `repoCheckout` archive facade)

**Status:** Shipped in v0.13.0, as a **script hook** rather than the typed `repo:` block
sketched below. Everything above "Config shape" held; the config shape did not. What landed is
`channel.prepare` (CONTRACT §9.1): `{script, env, secretEnv, timeoutSeconds}` — the harness runs
an operator-supplied `/bin/sh` on the lane with the workspace as cwd, and every event field
arrives as an environment variable. One generic seam (~150 lines) instead of a typed clone
block per forge; it also covers pushes, SSH remotes and non-git preparation without a schema
change. ach-agent executes configuration-owned scripts and contains no Git/cache policy. The rest
of this note stands as the reasoning, and the "Deferred" section is unchanged.
**Supersedes:** `mcpServers: {type: repoCheckout}` + `engine/repo_facade.py` +
`engine/repo_archive.py` — still present and supported (live users), now the legacy path.

## Shipped prepare/cleanup example

```yaml
prepare:
  script: |
    set -eu
    AUTH=$(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 -w0)
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0=http.extraHeader
    export GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"
    export GIT_TERMINAL_PROMPT=0
    export GIT_LFS_SKIP_SMUDGE=1
    REPO="$ACH_WORKSPACE/repo"
    URL="$GITLAB_REPO_BASEURL/$ACH_EVENT_PROJECT_PATH.git"
    if [ -d "$REPO/.git" ]; then
      git -C "$REPO" remote set-url origin "$URL"
      git -C "$REPO" fetch --prune origin
    else
      git clone --filter=blob:none --no-recurse-submodules "$URL" "$REPO"
    fi
    if [ -n "${ACH_EVENT_MR_IID:-}" ] && [ -n "${ACH_EVENT_HEAD_SHA:-}" ]; then
      git -C "$REPO" fetch origin "refs/merge-requests/$ACH_EVENT_MR_IID/head"
      git -C "$REPO" checkout --detach "$ACH_EVENT_HEAD_SHA"
    fi
  forwardEnv: [GITLAB_TOKEN, GITLAB_REPO_BASEURL]
  timeoutSeconds: 120
cleanup:
  script: |
    set -eu
    test -n "$ACH_WORKSPACE"
    rm -rf -- "$ACH_WORKSPACE"
  timeoutSeconds: 30
```

A shared bare mirror/worktree optimization, including its cross-session locking, must also be
implemented in these scripts rather than in ach-agent.

## Why

The agent needs a real on-disk repo to review a merge request. Today it gets one through the
`repoCheckout` facade: a harness-hosted MCP tool (`checkout_repo`) that reads gitlab-mcp's
`gitlab://{project}/archive/{ref}` resource harness-side with the `ek_`, base64-decodes a gzip
tar and extracts it under `/tmp/gitlab`. That path has six problems, in order of severity:

1. **No `.git`.** The facade's own docstring says it: "no blame/log/history". Without `.git`
   there is no local `git diff base...head`, so the agent still asks gitlab-mcp for the diff file
   by file — the exact token cost the checkout was meant to remove. It also rules out any tool
   that reads git (see "code-review-graph" below).
2. **A whole repo, base64'd through a JSON-RPC response.** The `subpath` parameter is a patch
   asking the model to keep the blast radius small.
3. **Model-initiated.** `boot/prompt.py:checkout_hint` nudges the model to call the tool. Costs a
   turn, and if the model skips the call there is no repo.
4. **Lifecycle blind.** The facade is shared and "cannot attribute a call to a `session_key`", so
   cleanup is a TTL sweep instead of deletion at session close — while the engine pool is already
   keyed by `session_key`.
5. **No cache.** A fresh `mkdtemp` per call; every event on the same MR re-downloads everything.
6. **Coupled to a non-standard resource.** The `gitlab://` URI scheme is hardcoded in the harness.
   Note: the sibling `mcp/gitlab-mcp` implements no `archive` resource at all (no match for
   `archive` or `gitlab://` in that repo) — verify which deployment actually serves it before
   assuming the feature is live.

## Decisions

**D1 — The harness clones. Credentials live in the harness process.**
Accepted knowingly: harness and opencode share a container and a uid (10001), so a co-resident
agent with a shell can read `/proc/<pid>/environ`. This is already true of the `ek_`; the git
credential does not change the threat model. "We do not hand it over" — not "it cannot be
obtained". The container-isolated alternative is recorded under "Deferred".

**D2 — Credentials, origin and repo policy are channel-scoped, not agent-scoped.**
A channel is 1:1 with a forge instance, so the credential is instance-scoped. An agent with two
webhook channels (gitlab.com + a self-managed instance) cannot express two credentials at agent
level. It also puts the outbound secret next to the inbound one (`webhook.auth.secret`) in the
same block.

**D3 — The channel decides; the router decides when.**
The channel knows which repo, at which ref, with which credential. It does **not** execute the
clone in the HTTP handler, because that scope runs before the router:

- GitLab drops a webhook at ~10s and redelivers. A cold clone blows that budget — failed
  delivery *and* a duplicate, for work already done.
- The pinned order is `dedup → backpressure → lane`. Cloning before admit turns a redelivery
  flood into a clone flood, on events dedup was about to discard.
- Before admit there is no `maxConcurrentInvocations`, so nothing bounds parallel clones.

Executing on the lane, pre-turn, buys three properties from machinery that already exists:
duplicates never clone, same-MR events serialize against one worktree, and parallel clones
inherit the concurrency cap.

**D4 — Workspace as cwd, not as a tool.**
The worktree is the opencode cwd. No MCP tool, no `checkout_hint`, one less tool schema in
context, and the repo is present before the first token.

**D5 — Fetch the MR head from the *target* repo's ref namespace.**
Verified against live remotes (`git ls-remote`, 2026-08-31):

```
gitlab.com/gitlab-org/cli.git      → refs/merge-requests/{iid}/head
github.com/anthropics/claude-code-action → refs/pull/{n}/head
```

Both forges publish the MR/PR head on the target repo. Fork MRs need no second remote, no second
credential and no fork URL from the payload. (`/merge` also exists if the merged result is wanted.)

## Config shape (sketch)

```yaml
channels:
  - name: gitlab-mr-review
    type: webhook
    source: gitlab
    webhook:
      auth: {type: gitlab_token, secret: {env: ACH_SECRET_GITLAB_HOOK}}   # inbound (exists)
      repo:                                                              # NEW — outbound
        baseUrl: https://gitlab.example.com   # origin allowlist: exactly this host
        auth: {env: ACH_SECRET_GITLAB_CLONE}  # SecretSource — env-only, {file} rejected at load
        ref: mergeRequest                     # mergeRequest | head | none
```

Reuse `SecretSource` + `resolve_secret` (`config/schema.py`): already env-only, already resolved
per use rather than cached, already covered by the redaction processors. No new secret plumbing.

The channel additionally stamps `project_path` (`project.path_with_namespace`, present on both the
MR hook and the note hook) into `delivery_context`; `project_id`, `mr_iid` and `head_sha` are
stamped today. The workspace layer then needs only normalized fields:

```
origin ← channel config      path ← delivery_context      ref ← delivery_context.head_sha
```

Do not put the built URL (or anything credential-adjacent) into `MessageEvent` — it is logged and
feeds the `{{ }}` template context.

**Not templating.** `channel.prompt`-style `{{ payload.… }}` resolution was considered and
rejected as the primary mechanism: one gitlab channel routes both MR hooks (`object_attributes`)
and note hooks (`merge_request`), the shapes differ, and the template engine has
`default("literal")` but no fallback-across-paths — one string cannot cover both. The normalized
`delivery_context` already does. Templating stays available as an escape hatch.

## Layout and lifecycle

```
<home>/repos/<project_id>.git       bare mirror, --filter=blob:none, cache (survives events)
<work_dir>/<session_key>/           worktree at head_sha — the opencode cwd
```

- Key the mirror by `project_id`, never by path: `path_with_namespace` changes on rename and would
  silently orphan the cache.
- Per-project `asyncio.Lock` around fetch. The router serializes per `session_key`
  (`project:mr_iid`), so two *different* MRs on the same repo do run concurrently against one mirror.
- Worktree removed at session close (the pool already has per-key lifecycle + `idleTtlSeconds`).
  The mirror stays — it is the cache.
- **Byte cap + LRU eviction by mtime from day one.** N repos × M open MRs on one PVC grows without
  bound; this is the thing that actually pages someone.
- Base SHA: GitLab's MR hook carries `target_branch`, not a merge base — compute
  `git merge-base origin/<target> FETCH_HEAD` locally. (GitHub does supply `pull_request.base.sha`.)

## Security rules

1. **Origin from config, path from payload.** Never clone a URL taken from
   `payload.project.git_http_url`. Anyone able to forge a webhook body would point it at their own
   host and harvest the token; the webhook secret is typically shared across an entire instance's
   hooks, so "authenticated" is weak here. Build `{baseUrl}/{path}.git` and reject everything else.
2. **The token never lands in `.git/config`.** Qodo PR-Agent embeds it in the clone URL
   (`clone_url += f"{token}@{host}{repo}"`) — correct for them, wrong here, because our agent has a
   shell and the working tree. Pass the credential to the clone subprocess only, and leave no
   authenticated remote behind.
3. **Read-only credential for a review agent.** Push (if ever enabled) gets a separate secret with
   its own scope.
4. **Untrusted content.** The worktree holds MR-authored code. `--no-recurse-submodules`,
   `GIT_LFS_SKIP_SMUDGE=1`, and be explicit that a prompt saying "run the tests" executes attacker
   code in the agent container. `GIT_TERMINAL_PROMPT=0` is already set (`lifecycle.py`).
5. Bounded clone: hard timeout, output to `/dev/null`, cleanup on failure.

## Prior art

Researched 2026-08-31. Full landscape in the conversation record; what changed the design:

- **Qodo PR-Agent** (OSS) grew a working copy — `GitProvider.ScopedClonedRepo`,
  `git clone --filter=blob:none --depth 1`, `CLONE_TIMEOUT_SEC = 20`, RAII `__del__` → `rmtree`.
  Take the recipe; reject the token-in-URL auth (rule 2) and note that `--depth 1` makes a local
  merge-base impossible, which is why they still read diffs from the API.
- **OpenHands**: `Workspace` abstraction (`LocalWorkspace` subprocess / `RemoteWorkspace`
  container), and reviews are triggered by a **human with write access** (label or reviewer
  request), never by the raw event.
- **Devin**: repos pre-cloned into a VM **snapshot**; every session boots a fresh copy. Confirms
  pre-baked workspaces work only for an enumerated repo set — which a group-wide MR reviewer does
  not have. (Devin *Review* is a separate web product with its own index, not the VM agent.)
- **Greptile / Cursor Bugbot / Qodo**: the index is persistent and warm across PRs; ~3-minute
  reviews assume it. Cursor exposes `dryRun` — full pipeline, nothing posted to the SCM.
- **Copilot coding agent**: ephemeral Actions env with an egress allowlist, because the agent runs
  PR code. Documented gap: the firewall "only applies to processes started by agent via Bash tool",
  not MCP servers.
- **aihero `/code-review`**: no clone; diffs `<fixed-point>...HEAD` three-dot; two isolated
  sub-agents (Standards vs Spec) whose verdicts are deliberately never merged. Prompt discipline,
  not infrastructure.

Nobody runs the cached-mirror + per-event-worktree shape, because CI gets a fresh runner free and
SaaS amortizes an index across a fleet. A long-lived pod serving N repos is neither.

## Consequences

- `mcpServers: {type: repoCheckout}` becomes deprecated the day this lands, and is removed the
  release after. Confirm no live agent depends on it first.
- `boot/prompt.py:checkout_hint` and its call sites disappear.
- Contract change: a new `channel.webhook.repo` block in
  `docs/schemas/operator-contract.md` + regenerated `agent-config-v1.schema.json`
  (`scripts/gen_schema.py`), and a matching render in `ach-runtime`.
- Unblocks **code-review-graph** as a `type: local` MCP server: it needs real git for
  `detect_changes`, and its `.code-review-graph/` index must sit beside the mirror to stay warm
  across events, or the indexing cost eats the token saving.

## Open questions

1. **Failure policy.** Clone fails → fail the invocation (metric, nothing posted) or run degraded
   without a workspace? House style is fail-open (memory, the current facade). Recommendation here
   is the opposite: a silently degraded review posted to an MR is worse than no review.
2. **Worktree per `session_key` or per project?** Per-key isolates; per-project is cheaper on disk
   and is safe because the lane serializes — but only within one project's MRs.
3. **Push.** Deferred entirely (see below), but when it lands: dry-run default, branch-prefix
   allowlist, no force-push, protected branches refused, diff-size cap, and the decision read from
   the terminal object — never from "the turn ended".

## Deferred (recorded so they are not re-litigated)

- **Container/exec lifecycle hooks** (`preStart`/`postStop`). k8s-style hooks fire at pod
  granularity; the unit of work here is the invocation, and nothing outside the harness observes
  invocations. `preStop` for push is dead on arrival — it fires at pod teardown. An exec hook
  (not a container) is the cheap version if a *second*, non-git use case ever appears; the
  precedent already exists in `mcpServers: {type: local}`, which runs an arbitrary command from
  config.
- **Init-container cache warmer** (ach-runtime already injects one, `agent_workload.go`
  `mergeInitContainers`). Only viable for a repo-pinned agent — you cannot warm repos you do not
  know yet.
- **Token-holding sidecar** with a loopback clone/push API, so neither the agent nor the harness
  holds the credential. The only design that actually closes D1's same-uid gap. An `ach-runtime`
  change, gated on deciding that exposure is unacceptable.
