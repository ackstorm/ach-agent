> Historical evidence for the revisions named below. The [current split contract](../references/2026-09-14-three-role-split.md) is authoritative; latest acceptance evidence is in [unix-split-validation](unix-split-validation.md).

# Phase 1 independent final validation

Root reviewed the branch while GPT-5.6-Luna implemented it. The independent
Compose, deadline, and Kubernetes snapshots in this report exercised commit
`076bcac`; final-source Compose and gate evidence at `beac73f` is recorded in
[the main evidence report](phase1-split-evidence.md). All upstreams in these checks
were synthetic. No external deployment was made.

## Final repository gate

Root independently ran the unchanged `scripts/pre-push-check.sh` in a clean local
clone at `beac73f`, with `PRE_PUSH_BASE_REF=462912f`. It exited 0:
1,215 tests passed, 3 skipped (82.89 seconds), 18 conformance tests passed,
Ruff/format/mypy passed, and gitleaks found no leaks across 58 commits.
The complete local log is `root-final-gate-beac73f.log` in the task scratch directory.

Root also ran the focused execution HTTP/client suites: 37 passed in 3.85 seconds.
Loading the pre-fix `076bcac` client into the new deterministic EOF regression
produced the expected `CancelledError`; the production tree was not changed for
that negative control. The final fix preserves only the bounded stop response
across controller loss and requires an explicit stopped confirmation.

## Final Compose repeat

Root independently ran `rtk proxy timeout -k 10 300 bash scripts/test-split.sh`.
It exited 0 for both real OpenCode and Pi, including two completed events with
the same native session reference, a held-inference barrier before engine
termination, a failed invocation, and harness readiness 503. The project and its
volumes were removed by the script.

Images from this independent run:

| Role | Image SHA256 |
| --- | --- |
| Harness | `eabbb1cdd67b8efacd1836f026bd1388808602a3d870657d0aab40651cf81992` |
| Channels | `4b45bf3516965be971b0bdff430e00ef151082fa5d8d10964438abb1dffcc206` |
| OpenCode | `669e9104861d65eb7bddadb7bdeb0fb142efb9570e28bdcdfe32bfd0a0f2c856` |
| Pi | `e53d6dcc1a2f446cc49a8b1f6c7a1fbd3e22145fa55409b53a045067349a0f03` |

## Pi invocation deadline and recovery

A separate disposable Compose project used the same harness/channels/Pi images,
an 8-second invocation deadline, and a synthetic model response held for 60 seconds.
The probe submitted a normal event, the held event, then another normal event.

- First event completed at 04:35:37 UTC on 2026-09-12.
- Held event started at 04:35:38.169 and exceeded the invocation deadline at
  04:35:46.260; the fixture confirmed one held model request actually started.
- The following event completed at 04:35:47.618 with the original native session
  reference `2026-09-12T04-35-37-334Z_01a093e6-46f6-7-9750363f`.
- Harness and engine readiness returned 200; all three application containers had
  zero restarts. This exercises timeout recovery, separately from killing E.

All four test containers, six volumes, and the project network were removed.

## Kubernetes snapshot provenance

The disposable kind validation described in [the main evidence report](phase1-split-evidence.md)
used the following earlier Task 10B runtime images, not the final Compose images:

| Role | Image SHA256 |
| --- | --- |
| Harness | `3b9e741c46a6ecac38e9ec8d5ac81d0c289e5222d4943e101b54e7e26a6e7273` |
| Channels | `db994778fd233ed73bbcd94b295d3e2b2475dcdcc215421e46957948637a2f56` |
| OpenCode | `06797aa9bc8d48eb7f6bab373510c2245fe8362f95af61c41895ce06fb85bc85` |

kind 0.31.0 / Kubernetes 1.35.0 reached 3/3 ready, completed two events across
different lanes using one custom conversation, then completed a third after a
harness restart using native session `ses_f6c31bf42ffeCdNlc45LAIJMv8`.
Private state sentinels and credential/mount exclusions were checked. The cluster
was deleted. The external ach-runtime renderer and PVC provisioning remain
integration responsibilities outside this repository.
