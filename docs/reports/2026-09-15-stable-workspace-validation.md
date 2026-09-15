# Stable workspace placement validation — 2026-09-15

The earlier [live transition report](2026-09-15-pvc-placement-transition.md)
records the failure on v0.16.4. This correction preserves the original persistent
workspace directory rather than relocating native sessions.

## Implementation and contract

Persistent standalone and distributed resolve the default workspace to
`<mountPath>/home/workspace`. Standalone defaults are unchanged. The obsolete
workspace-copy helper and its startup call are removed. Native engine adapters,
session mappings and the configuration schema are unchanged.

The operator must mount PVC subPath `home/workspace` at that path in Harness.
Engine mounts subPath `home` and sees the same physical workspace through it.
Harness never receives the whole Engine home. Image and renderer must be rolled
out together; the old distributed `workspace` subPath is not equivalent.

The Compose examples use separate home/workspace volumes, mounting the workspace
at the same nested path in Engine and Harness. The persistent transition fixture
instead uses a single host data tree to reproduce the existing-PVC layout.

## Verification

- Regression proof: the new persistent path-equality test fails for both Pi and
  OpenCode when executed against the baseline resolver from `a18489c`.
- Fixed full suite: **1,215 passed, 3 skipped**.
- Conformance: **18 passed**.
- Ruff, formatting, strict mypy and strict documentation build pass.
- No schema/config-model diff against `a18489c`.
- Real `scripts/test-split.sh`: OpenCode and Pi completed two turns, reused their
  native session, passed environment/hook checks, and failed readiness after
  controller loss while Harness exited for supervisor restart. Exit status 0.

## Native persistent transition

Root independently ran `scripts/test-pvc-transition.sh` for both engines against
the locally built combined image `ach-agent:pvc-stable-review`. Both runs exited
**0**, including cleanup. This uses real native binaries and a deterministic local
model fixture, not a live model provider.

| Check | OpenCode | Pi |
| --- | --- | --- |
| Standalone Harness terminal response and summary observed | Pass | Pass |
| Native session reference unchanged after split | Pass | Pass |
| Prior standalone history present in distributed provider requests | Pass | Pass |
| Native tool reads `distributed-marker` written through Harness | Pass | Pass |
| Distributed invocation has actual completed result | Pass | Pass |
| Harness cannot access Engine session storage | Pass | Pass |

OpenCode retained `ses_f5b7603f1ffeeaRqBbIZd3LqZ0`. Pi retained the same JSONL
session file ending in `01a0a489-fd5a-77c3-b4d7-ff7ce9a254a2.jsonl`.
Both distributed upstream fixtures recorded message counts `[6, 8]`,
`prior_history_seen=2` and a tool result containing `distributed-marker`.
The upstream fixtures restart between placements; this history comes from native
session storage. Neither fixture places marker values in the submitted prompt.

The standalone completion assertion checks real Harness response/summary logs;
upstream traffic alone does not count as completed work. Distributed completion
uses the existing event-result API. Failed fixture iterations exposed test wiring,
log-pipeline and temporary-file cleanup errors; these were corrected before the
independent final runs.

No corrected operator-rendered Kubernetes transition has been run yet. This does
not validate custom layouts or already-populated distributed `base/workspace`
volumes. The live GitLab reviewer has not been changed by this work.
