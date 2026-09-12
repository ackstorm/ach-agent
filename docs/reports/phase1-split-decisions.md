# Phase 1 implementation decisions

This records implementation rulings made while executing the approved phase 1
plan. The specification remains authoritative. Validation results are recorded
separately; this document does not claim release or deployment approval.

| Order | Decision and reason | Cost or limitation |
|---|---|---|
| 1 | Luna implements; the coordinating agent performs task and final reviews, following the user's explicit assignment. | No separate reviewer agent; the coordinator owns review quality. |
| 2 | Isolate work in a local Git worktree and use the repository's local exclude file for the worktree directory. | Local checkout setup is not propagated to other developers. |
| 3 | Allow strict expected-failure characterization tests for the reproduced preparation vulnerability before fixing it in the next task. | Those expected failures must disappear with the fix; they are not release evidence. |
| 4 | Resolve plan details against the specification and existing behavior without interrupting the user for routine choices. | Incorrect assumptions require visible review and rework; destructive workspace changes still need compatibility evidence. |
| 5 | Prepare credentialed checkouts in fresh private scratch, then transfer a credential-free Git bundle. Preserve history, workspace identity and retained untracked files. | The handoff supports the documented Git checkout convention, not an arbitrary filesystem export. |
| 6 | Split envelope/registry implementation from channel caller wiring for bounded implementation and review. | One additional task boundary; no runtime mechanism. |
| 7 | Let each A2A caller wait on the existing deduplicated completion registry, sharing only cancellation notification and waiter counts. | Would need revisiting if the SDK required centralized result fanout. |
| 8 | Execute credential-free workspace hooks and Git imports inside engine; execute all script-only hooks in private harness scratch, including scripts without secretEnv. | Hook paths/environments follow their execution role. Sanitizing env alone would not protect harness files from engine-planted Git configuration. Native same-UID mode remains weaker. |
| 9 | Preserve engine-pool minting of public attribution tokens; return the token so harness can establish trace/cost context before inference. | Tokens remain correlation data. Future per-execution authorization would require a separate review of issuance and validation. |
| 10 | Keep public cleanup under the existing pool release/discard/TTL lifecycle instead of adding another cleanup API. | A future independent cleanup caller would need a narrowly justified extension. |
| 11 | Split private producer/cleanup-barrier work from the central runner migration. | Additional review/dispatch overhead, with unchanged runtime scope. |
| 12 | Use PublicEngineConfig at the harness boundary, retaining unresolved MCP templates for engine-side expansion. | Main wiring and fixtures must use the public type; native EngineConfig remains engine-side. |
| 13 | Split role configuration/bootstrap work from main/local launcher orchestration. | Another bounded integration review; no additional service. |
| 14 | Require credentialed preparation to materialize reachable Git objects before its script returns. Disable post-hook lazy fetching and document removal of incomplete filtered clones from the reference script. | Large repositories need more initial download and scratch storage. Operators using partial clones must materialize them or change the script. History and session continuity are preserved. |
| 15 | Do not add a channels-to-harness heartbeat solely to duplicate readiness of this fixed three-container pod. Channels authenticates startup; signed requests and harness/pod health govern normal operation. | Channels readiness alone can remain true during later key/connectivity mismatch. Rotate the per-pod key with a coordinated rollout. |
| 16 | Require the external deployment/storage handoff to provision the documented PVC subdirectories rather than adding a broad harness mount or application init container. | Operators must prepare those subpaths before the first rollout. Kubernetes validation explicitly provisions them; this repository does not implement the external ach-runtime renderer. |

Phase 2 proxy/content-protection work and phase 3 queue, autoscaling and S3 ideas
remain outside this implementation. No new ownership lease, broker or Unix
transport was introduced.
