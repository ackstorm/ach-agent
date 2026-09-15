# Distributed GitLab reviewer: cluster validation

Validated on 2026-09-15 with ACH operator v0.8.11 and ACH Agent v0.16.3.
This records the deployed release before the subsequent log-ownership correction.

## Deployment

GitOps commit `e392b265cec81abceaef7698c321f155bcb1552b` adds only
`spec.placement: distributed` to `ach/gitlab-reviewer`. Flux applied the change;
the shared profile and other agents were unchanged.

Pod `achagent-gitlab-reviewer-7fd5d48fdf-wqn4q` reached 3/3 Ready with zero
restarts. All three containers used image digest
`sha256:9426a036cea6d6f505bb45c4141053bf047f933a7c64c37d74cd9a3519f59c69`.
Startup/readiness probes used HTTP `/readyz` on channels 8080, harness 8090,
engine 8081. The public gateway readiness URL returned HTTP 200.

## User-triggered GitLab event

- Channels received event `abd2318c-a4ee-427a-8dd5-930df5cc9b74` at 06:11:55 UTC,
  session key `1158:issue:14`.
- Harness prepare cloned the repository into the shared session workspace:
  exit 0, 3,467 ms.
- Engine launched OpenCode and became ready in approximately 2.1 seconds.
- Native session: `ses_f5c4e647bffeiZbtCkX9AGuJMj`.
- Trace: `487ccea9f76b05f3de9ef8a446115dec`.
- Model and MCP requests passed through Harness proxies. Logs recorded HTTP 200
  and successful tool results.
- The GitLab comment tool returned comment ID `20090` on issue 14 at 06:13:40 UTC.
  Harness recorded the final response at 06:13:49 and a summary of 25 tools.
- Cleanup completed at 06:14:20 UTC: exit 0, 40 ms. Pod remained 3/3 Ready without
  restarts.

## Limits and follow-ups

This proves one real channel invocation through the operator-rendered split,
including preparation, shared workspace access, model/MCP proxying, final result
and cleanup. It does not prove native session reuse across two separate events.

The partial clone's historical `git show` attempted a lazy fetch and failed
because Engine had no forge credential. The agent continued and completed the
task. This is a prepare/available-content limitation, not a reason to expose the
Harness credential to Engine.

Hydration reported that `ach-memory` was absent from the manifest, so this run
did not validate memory integration.
