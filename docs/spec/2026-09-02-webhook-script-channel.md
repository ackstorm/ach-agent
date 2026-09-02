# Webhook Script Channel

## Goal

Add a deterministic inbound channel that validates and admits a webhook, executes a
static shell script, and never starts or calls an agent engine.

## Contract

```yaml
- name: gitlab-register
  type: webhook-script
  source: gitlab
  concurrency: 4
  webhook:
    auth:
      type: gitlab_token
      secret: {env: ACH_SECRET_GITLAB_REGISTER_WEBHOOK_SCRIPT}
    gitlabEvents:
      - project_create
      - project_rename
      - project_transfer
      - project_update
      - repository_update
      - push
      - merge_request
  script:
    script: |
      set -eu
      # normalized webhook JSON is available on stdin
    env: {GITLAB_REPO_BASEURL: https://git.example.com}
    secretEnv:
      GITLAB_TOKEN: {env: ACH_SECRET_GITLAB_REGISTER_SCRIPT_GITLAB_TOKEN}
    timeoutSeconds: 120
```

`webhook-script` requires `source`, `webhook`, and `script`. It forbids `prompt`,
`prepare`, `cleanup`, `cron`, `queue`, and `a2a`. The script block has the same static
script, environment allowlist, secret handling, timeout, bounded debug-output, and
process-group kill guarantees as lifecycle hooks.

The HTTP request returns `202` after admission. Execution remains asynchronous. A script
failure is logged and counted, but cannot change the already-returned HTTP response.

## Execution

The adapter authenticates and parses the webhook before it reaches the router. GitLab
script events use a project-scoped lane (`<project-id>:webhook-script:<channel>`) so two
events cannot concurrently reconcile the same project. After deduplication and
backpressure, the runner:

1. creates a temporary workspace under `engine.workDir`;
2. exposes the validated `ACH_EVENT_*` values and configured environment;
3. runs `/bin/sh -eu -c <script>` with normalized webhook JSON on stdin;
4. captures only the bounded output tail and logs it at debug level;
5. removes the temporary workspace; and
6. returns without probing memory, acquiring the pool, or invoking a model.

GitLab events added to the accepted contract are `push`, `project_create`,
`project_rename`, `project_transfer`, `project_update`, and `repository_update`. Existing
`merge_request`, `issue`, and `note` behavior remains unchanged. `tag_push` and destructive
or user/group lifecycle events are intentionally excluded because they do not help install
a project webhook.

## Operator

`ACHAgent.spec.channels[].type` accepts `webhook-script`, with `script` using the same
`forwardEnv` resolution as `prepare` and `cleanup`. Agent env overrides profile env by name.
Unknown forwarded names remain unset. Secret values are injected through generated Pod env
aliases and never written to rendered config.

