# Existing PVC: standalone → distributed — 2026-09-15

**Result: data preservation passes; end-to-end conversation continuity fails.**
Do not advertise the default-layout placement switch as a transparent upgrade.

## Environment and procedure

- EKS, ACH operator **v0.8.11**, ACH Agent **v0.16.4**, OpenCode and real Gemini.
  This does not claim to have tested operator v0.8.13.
- Temporary GitOps-managed `pvc-transition-probe` profile and agent, internal
  webhook, synthetic files, no external tool actions.
- A new 1 GiB EBS PVC was populated by standalone before switching placement.
  The test StorageClass and profile use deletion policies for subsequent cleanup.
- Standalone created `pvc-probe.txt` through native bash and remembered the phrase
  `amber lighthouse`. Additional synthetic markers were placed in Harness state
  and Engine home.
- GitOps commit `6091a41779e40d41f8eabacb1c4b96592265db69` changed **only**
  `placement: standalone` to `placement: distributed`. Image, PVC, profile,
  logical conversation key and other agent configuration stayed the same.

## Standalone prerequisite found

The initial fresh-PVC standalone deployment could not start:
`persistence.enabled=true but state storage missing or not writable`.
Its rendered pod security context had no `fsGroup`. The image runs as UID 10001.

The test profile was given `podTemplate.spec.securityContext.fsGroup: 10001`
before populating the volume (GitOps commit `86c73fe`). Standalone then became
Ready with zero restarts. The same profile overlay remained for the placement
switch. ACH should test fresh-PVC standalone permissions separately; its
ephemeral standalone readiness evidence does not cover this case.

## What survived the switch

The PVC UID remained `4f3bc96b-4858-46e0-beff-db09520a77a3`, backed by the same
PV `pvc-4f3bc96b-4858-46e0-beff-db09520a77a3`. Distributed became 3/3 Ready
with zero restarts.

| Evidence | Result |
| --- | --- |
| Harness state marker | Same SHA-256; visible to Harness, absent from Engine |
| Engine home marker | Same SHA-256; visible to Engine, absent from Harness |
| Workspace marker | Copied into the new shared workspace; same SHA-256 and UID/GID 10001 |
| Native session map | Same key and OpenCode session ID |
| Repeated pre-switch event ID | HTTP 200 `duplicate`; not executed again |
| Post-switch invocation | Accepted, then timed out after 120 seconds |

Workspace marker SHA-256 before migration and in both copies immediately after:
`276e634a743fa73210325359a63bfcdfa47cd537e8b2351688b9dd36de5480c5`.

The map retained:

```text
opencode:tests/pvc-transition:1 → ses_f5bada939ffeIca9r9VbKr0CCQ
```

## Why the successful copy is insufficient

Standalone's default workspace is `/var/lib/ach-agent/home/workspace`.
Distributed's default is `/var/lib/ach-agent/workspace`.
`migrate_legacy_workspace()` copies the former to the latter during Engine init
and creates `.ach-workspace-migrated`. It leaves the original directory intact.

OpenCode's native session still reports:

```json
{"id":"ses_f5bada939ffeIca9r9VbKr0CCQ","directory":"/var/lib/ach-agent/home/workspace"}
```

After the switch, native logs explicitly show it booting that old location and
resolving `cat pvc-probe.txt` to the old path. Its native message API contains a
completed answer with the original marker and the remembered phrase. However,
ACH's engine event stream did not report those tool/completion events and Harness
timed out `pvc-after-1` at **09:11:46 UTC**, 120 seconds after admission.

The stale native session directory is directly observed. Event-stream scoping
between the new server location and the old session location is the likely
explanation for the missing completion, and still requires a focused fix/test.
Simply changing event consumption would not fix the stale workspace used by tools.

A subsequent synthetic write from Harness to the new workspace left the old
copy unchanged, confirming that the paths are separate copies, not aliases.
No claim is made that the earlier native read observed this later write.

## Handoff

- The operator's existing mount contract preserved the PVC and isolated state.
  Do not add OpenCode-specific database or migration logic to the renderer.
- ACH Agent must preserve or correctly relocate the native session's workspace
  association, as well as its files and conversation map. Dropping the map would
  hide the failure by losing continuity and is not an acceptable fix by default.
- Repeat this test after the fix, requiring an actual terminal result, access to
  a marker written by Harness in the active workspace, and the same native
  session/history. Also test a second restart to verify migration idempotency.
- Pi, older pre-split images, custom home/workDir layouts, and the reverse
  distributed → standalone transition were not tested here.

No runtime code, operator mount contract, production PVC or reviewer session was
changed by this test. GitOps cleanup commit
`062f007e8c6edb35c986a8d18931d076245857c8` was reconciled by Flux. The test
agent, profile, Deployment, Pod, Service, PVC, PV and StorageClass were removed.
AWS confirmed that its EBS volume `vol-0cb4b37d62d20273b` no longer exists.
