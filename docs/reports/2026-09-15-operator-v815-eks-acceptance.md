# ACH v0.8.15 / ach-agent v0.16.5 — EKS acceptance

On 2026-09-15, OpenCode and Pi each completed a real standalone invocation,
changed to distributed placement on the same EBS volume, and completed a second
invocation using the same native session. The earlier workspace relocation
failure is resolved for the standard persistent layout.

## Environment and method

- EKS `pro-ack-ai-platform`, namespace `ach`, operator `v0.8.15` verified live.
- Both test agents pinned `ghcr.io/ackstorm/ach-agent:v0.16.5`; every observed
  container used image digest
  `sha256:9ef89f09eed0b33ceb77cb7a35f90dadd3141d10a864460674cf5f5d429f45ec`.
- Real `gemini-flash-latest` calls through ACH; native OpenCode and Pi binaries.
- Dedicated 1 GiB gp3 EBS volumes, `ebs.csi.aws.com`, Delete reclaim policy.
- No pod securityContext override: UID, GID and fsGroup 10001 came from ACH.
- Temporary webhook agents were installed through GitOps and Flux. Ingress was
  exercised internally with synthetic GitHub-shaped payloads, not a public
  GitHub delivery. No production review or forge mutation was triggered.

Both requests used repository `tests/pvc-transition`, number `1`, channel
`persistence-check`, `session: auto`, and distinct event IDs. The first request
asked the engine to remember `amber lighthouse` and write `PVC_MARKER_20260915`
to `pvc-probe.txt`. Actual Harness terminal response and summary logs were
observed before proceeding; HTTP 202 alone was not used as completion evidence.

The only fixture change between invocations was `placement: standalone` to
`placement: distributed`. After the rollout, Harness overwrote that same file
with `DISTRIBUTED_EBS_MARKER_20260915`. The second prompt supplied neither the
remembered phrase nor the new marker. Both native bash tools read the new marker,
and both terminal results contained the remembered phrase and new file contents.

## Results

| Check | OpenCode | Pi |
| --- | --- | --- |
| Fresh persistent standalone Ready and native tool write as UID/GID 10001 | Pass | Pass |
| Same PVC UID and PV across placement change | Pass | Pass |
| Workspace path and file hash unchanged immediately after rollout | Pass | Pass |
| Native session mapping identical before and after second invocation | Pass | Pass |
| Three distributed containers Ready, zero container restarts | Pass | Pass |
| Harness sees only workspace beneath engine home | Pass | Pass |
| Engine cannot see Harness state marker or private config.json | Pass | Pass |
| Harness write visible to native tool at original workspace path | Pass | Pass |
| Second invocation reports completed, without error | Pass | Pass |
| Second response recalls first-turn phrase | Pass | Pass |

The workspace file remained `/var/lib/ach-agent/home/workspace/pvc-probe.txt`.
Harness mounts PVC subPath `home/workspace`; Engine sees it beneath its `home`
subPath mount. There was no session migration, copied workspace or mapping reset.

### Identifiers and terminal evidence

| Field | OpenCode | Pi |
| --- | --- | --- |
| PVC UID | `73f13904-6b1d-479c-aaf8-f613370f21a4` | `1433034b-b072-4b44-b35f-5379767efe4b` |
| EBS volume | `vol-02a8c68e26e93f375` | `vol-033a3ad33b54841d8` |
| First terminal response, UTC | 13:44:10 | 13:44:00 |
| Second invocation ID | `8367588194974db0a53bfe48219fc639` | `e59eac557ea34657942735a6514c3b13` |
| Second result state | `completed` | `completed` |

OpenCode retained `ses_f5ab0abd8ffeTVMgH3tgdGepRP` under mapping
`opencode:tests/pvc-transition:1`.

Pi retained this exact native reference under `pi:tests/pvc-transition:1`:

```text
/var/lib/ach-agent/home/pi/_tests_pvc-transition_1-cc1b234b/sessions/2026-09-15T13-43-55-478Z_01a0a54f-5756-77d5-9698-be778d8de264.jsonl
```

Distributed completion was independently retrieved through the existing channel
completion API. OpenCode returned `amber lighthouse\nDISTRIBUTED_EBS_MARKER_20260915`;
Pi returned `Remembered phrase: amber lighthouse. Current file contents:
DISTRIBUTED_EBS_MARKER_20260915`. Both results had `action: none`, `error: null`.
Engine tool logs also confirmed the live file read, rather than relying only on
model claims.

## GitOps and scope

Test fixture: `2e99ef5`; placement-only change: `cb968d2`; fixture removal:
`7a3c03d`, in `gitops-genai-blueprint`. YAML server dry-run and Kustomize rendering
passed. Flux dependency ordering was retained throughout.

Cleanup completed through Flux: both test ACHAgents and their Deployments, pods,
Services and PVCs were removed, along with the temporary profile and StorageClass.
Both PVs disappeared after CSI detach, and AWS `describe-volumes` filtered to the
two recorded EBS IDs returned an empty list. No pre-existing PVC was removed.
The production GitLab reviewer remained three containers Ready, zero restarts,
on the same v0.16.5 digest. The documentation build passed with `make docs-build`.

This closes the live EBS permission and native-session transition checks left
open by the operator's kind report. It does not test custom paths, older
distributed `base/workspace` data, channel prepare scripts, or a public webhook
round trip. Those are distinct from this placement continuity check. No new
operator checks, engine-specific mounts or application code changes were needed.
