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

The recovery correction makes the Harness exit through its normal shutdown path
when its controller is permanently lost. Its supervisor starts a fresh Harness,
which hydrates and initializes a new controller. A transient failed health check
only removes readiness. This adds no reconnect or invocation replay protocol.

Validation after the correction: 1,211 tests passed, 3 skipped; Ruff, formatting
and strict mypy passed. The public schema is unchanged.

## Baseline: Pi in Kubernetes

Both standalone and distributed completed two real Gemini-backed invocations.
The selected synthetic environment value reached the native bash subprocess;
the unselected value was absent. Both modes reused their native session, and
resubmitting the second event returned HTTP 200 `duplicate`.

- Standalone session: `2026-09-15T08-05-10-440Z_01a0a419-34a8-7-321bee93`.
- Distributed session: `2026-09-15T08-05-10-095Z_01a0a419-334f-7-a07fac0c`.

The distributed Engine also lacked `ACH_TOKEN`, the complete configuration and
Harness state, while its home and workspace were visible. These checks inspected
only existence/booleans and the named synthetic values.

## Compose restart limitation

The corrected `scripts/test-split.sh` completed with exit 0 on the release
runtime, with real OpenCode and Pi binaries and a controlled model upstream:

| Check | OpenCode | Pi |
| --- | --- | --- |
| Startup through completed hydration | 6 s | 7 s |
| HTTP health/readiness on all three roles | Pass | Pass |
| Internal APIs absent from public health listeners | Pass | Pass |
| Skill copy, temporary transfer removal, private mounts | Pass | Pass |
| Selected environment values in native process | Pass | Pass |
| Two completed turns, same native session | Pass | Pass |
| Prepare twice on retained workspace; separate hook HOME | Pass | Pass |
| Proxy uses authorized upstream requests | Pass | Pass |
| Engine killed during held inference: Harness exits 0 | 2 s | 2 s |
| Channels becomes not ready after controller loss | HTTP 503 | HTTP 503 |

The script removed its Compose containers and volumes afterward. Local evidence:
`/tmp/ach-v0.16.4-split-acceptance.log`.

A separate experiment added `restart: always` to Harness and Engine. After an
Engine failure, both restarted and readiness returned, but a new model invocation
timed out. Inspecting `/proc/1/ns/net` showed that Harness had a new network
namespace while Channels and Engine retained the old one; Engine could not reach
the Harness HTTP listener on localhost. This is a limitation of restarting the
namespace-owning container independently under Docker Compose's
`network_mode: service:harness` arrangement. Recover that setup by recreating the
stack together. Kubernetes recovery is tested separately because the pod owns
the shared network namespace.

## Published v0.16.4

Release commit: `153097a2b77eb3de00bc540c61b7ecd1ae556cf5`.
Both `v0.16.4` and `latest` resolve to index digest
`sha256:9573ea2d553cbe9f553adfd0cc396f50ef560aea3cbd990f319286e4fe7b7e7b`.
GitHub release workflow `34945130899` completed successfully, including docs.

The temporary agents pulled that digest in every container. Pi passed the
following checks on the published image in **both** placements:

- Two distinct events completed with the same native session; duplicate second
  submission returned HTTP 200 `duplicate`.
- Native bash returned the selected synthetic value and `private_present: false`.
- Distributed Engine emitted prompt, tool and model-generation logs. Harness
  emitted final response/summary without those detailed engine logs.
- Killing Engine PID 1 in distributed mode caused Engine and Harness to restart
  once; Channels remained running. Killing the native mini-harness child in
  standalone caused its parent container to restart once.
- Both returned to Ready automatically and completed `pi-recovered-1` afterward.
- Engine could not see the managed token, private configuration or Harness state;
  its home/workspace were present and the transfer directory had been cleaned.

Pi telemetry still reports zero tools/duration in its summary despite a completed
bash action. That was also observed on the v0.16.3 baseline; this battery validates
execution and log placement, not the accuracy of those existing summary metrics.

OpenCode passed the same two-turn/session-reuse, duplicate, environment and log
placement checks in both modes. Its summary retained `tools=1` after the detailed
tool log moved to Engine. Sessions were `ses_f5bd08aa5ffeAqZWySK52btTVs`
(distributed) and `ses_f5bd08fbeffeNoTMX43UWu250R` (standalone).

Both OpenCode placements also recovered automatically after mini-harness death
and completed `oc-recovered-1` with the expected synthetic environment output.
The distributed agent then recovered from separate Harness and Channels
terminations. `roles-recovered-1` completed afterward, reusing the OpenCode
session from before those two restarts.

`gitlab-reviewer` was restarted onto the published digest. Pod
`achagent-gitlab-reviewer-7fd5d48fdf-mlc5n` became 3/3 Ready with zero restarts;
its public gateway `/readyz` returned HTTP 200 and `{"status":"ok"}`. No new
GitLab comment/hook was sent by this battery; the earlier user-triggered real
review is recorded in `2026-09-15-distributed-gitlab-cluster-validation.md`.
Direct HTTP checks also returned 200 for both `/healthz` and `/readyz` on all
three role ports (8080, 8090 and 8081).

## Cleanup

GitOps commit `fcc01a86131135e7126b0a4974fb4816950d7f7b` removed both temporary
agent manifests and their kustomization entries. Flux `workloads-agents` applied
that revision; both ACHAgents and their Deployments, Pods and Services were
absent afterward. Task-owned port-forwards and local Compose containers/volumes
were removed. The distributed `gitlab-reviewer` remains deployed and Ready.
