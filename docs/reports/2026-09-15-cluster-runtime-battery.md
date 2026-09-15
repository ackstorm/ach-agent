# Cluster runtime battery — 2026-09-15

Environment: ACH operator v0.8.11 on EKS. Temporary agents are declared in GitOps
under `workloads/agents/achagents/` and referenced by its kustomization. They use
the default profile, a controlled prompt and an internal-only webhook Service.
Only synthetic named environment values are printed; full environment listings
are not requested.

## Baseline: ACH Agent v0.16.3, OpenCode

| Check | Standalone | Distributed |
| --- | --- | --- |
| Startup/hydration readiness | 1/1 Ready | 3/3 Ready |
| Selected `SPLIT_ENV_VISIBLE` reaches native tool subprocess | Pass | Pass |
| Unselected `SPLIT_ENV_PRIVATE` absent from native subprocess | Pass | Pass |
| Two distinct events reuse native session | Pass | Pass |
| Duplicate event ID | HTTP 200 `duplicate`; no extra turn | Pending |
| Engine loss removes readiness | Pass: `/readyz` 503, `/healthz` 200 | Pass: all roles not ready |
| Automatic recovery after engine loss | Failed: parent remains alive/not ready | Failed: E restarts without a new init from H |

Standalone native session: `ses_f5c3e9335ffevq5wgUg0bWP1I1`.
Distributed native session: `ses_f5c3d7f5affewY52XJ3X2rVWPh`.
Both used logical lane `tests/split-env:1` and returned
`{"visible":"synthetic-forwarded-20260915","private_present":false}`.

The distributed environment was restored successfully by restarting its
Deployment. The recovery failure is caused by H retaining a permanently lost
controller client; its readiness watcher never exits to permit fresh startup.
The first v0.16.4 publication workflow was cancelled before release/tag creation
so the minimal recovery correction can be verified before deployment.

## Updated release

Runtime verification of the final image, Pi parity, recovery and cleanup are
pending. This report does not claim those checks passed.
