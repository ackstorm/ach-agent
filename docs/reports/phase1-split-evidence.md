# Phase 1 split acceptance evidence

This report records the Task 10B implementation at the final worktree commit. The
acceptance fixture is deliberately self-contained: its model, hydration, and
authentication upstream is `tests/integration/fixtures/upstream.py`, and it uses no
ACH credential or external memory service.

## Live Compose acceptance

The exact command was:

```text
rtk proxy bash scripts/test-split.sh
```

The script creates a unique Compose project, builds the three role images, allocates
an ephemeral host port, and removes the project, volumes, and orphans in its exit
trap. It ran each engine with the same channels and harness roles.

| target | live result | upstream evidence | failure/cancel evidence |
| --- | --- | --- | --- |
| OpenCode | two HTTP 202 admissions completed with `PHASE1_SPLIT_REPLY` / `action: none`; startup 4s | `authorized=4`, `hydrate=1`, `model=3`, message sizes `[3,2,4]` | an active `CANCEL_ME` event became `failed` after E termination in 1s; H `readyz` returned 503 |
| Pi | two HTTP 202 admissions completed with `PHASE1_SPLIT_REPLY` / `action: none`; startup 4s | `authorized=3`, `hydrate=1`, `model=2`, message sizes `[2,4]` | an active `CANCEL_ME` event became `failed` after E termination in 1s; H `readyz` returned 503 |

The final live run's built image IDs were H
`sha256:6f19bc8779622def941ec1de33f89b6d725b72624cf05bb4968269e7c4b79f1b`, C
`sha256:375af9cca70951527950cd6b48bad8c9073690dab0fe9a34611abb809e804998`, E/OpenCode
`sha256:681a8dced7913f415f4e6f62d4e5edc0b371a0a42c60d2625b13ad3818422a92`, and
E/Pi `sha256:456f287c60ed64cc0561f871726c222f75bb80f6cabb0e6fdc3343c1b3db9385`.

The fixture increments its authorized counter only after the synthetic `x-ach-key`
check, so the model and hydration calls above demonstrate that credentials were
added by the H-side proxy path. The varying message sizes demonstrate that the
engine sent the expected request forms; Pi's provider payload is a list of text
parts while OpenCode sends the OpenAI message array.

The script prints integer-second measurements for startup and cancellation. The
configured native startup deadline is 30 seconds; idle TTL is 30 seconds. The two
positive events use the same custom conversation key, and the independent cancel
channel uses a separate key. The live script does not assert expiry at exactly the
idle TTL, so TTL timing remains a limitation rather than a claimed measurement.

## Role and boundary checks

The acceptance Compose file runs exactly channels, harness, and engine application
roles, plus the synthetic upstream test service. H owns ACH credentials, hydration,
the channel HMAC key, and harness state. E receives the credential-free engine JSON,
public context, engine home/state, and shared workspace. The E image has no
`ACH_TOKEN` or HMAC key. The channels projection is built from an allowlist and
contains no prompt or session fields; the parity test validates this with the real
Pydantic schema.

The live event path is C public ingress → signed C/H transport → H router → H/E
execution HTTP → native engine → H model proxy → synthetic upstream. The active
engine termination leaves H running but unready, and the admitted event resolves
as failed without replaying its prompt.

## Existing coverage mapped to the parity inventory

The live Compose fixture is model-only. The following existing tests provide the
remaining meaningful runtime evidence and are run by the repository gate:

- `tests/e2e/test_opencode_mcp_structured_e2e.py` exercises real local MCP HTTP
  authentication through the proxy, model streaming, and terminal parsing. Its
  direct targeted run passed `3 passed in 0.30s`; it does not launch a native binary.
- `tests/engine/test_mcp_proxy.py` covers route scoping, injected credentials,
  streaming shutdown, and upstream failure behavior.
- `tests/boot/test_engine_runner_http.py` covers controller cancellation, peer
  usability after cancellation, cumulative deadlines, session rotation, and
  cleanup acknowledgements over the execution HTTP seam.
- `tests/engine/test_lifecycle.py`, `tests/engine/pi/test_driver.py`, and the
  conformance tests cover typed launch failure, native process cleanup, terminal
  repair, usage/cost plumbing, environment stripping, and secret hygiene.
- `tests/test_private_prepare.py` and `tests/test_prepare.py` cover authenticated
  Git/forge preparation, filtered-history materialization, workspace retention,
  and script-only cleanup. The Task 10B F1 regression now verifies both secret and
  no-secret scripts use private cwd, HOME, and workspace paths.
- Root's Task 9 actual PTY evidence covers native TUI input, resize, two-turn
  continuity, and clean exit for both Pi and OpenCode; it is recorded in the task
  ledger and was not replaced by a piped smoke test.

Root's packaged Task 9 image was
`sha256:860b5fffc5fab3696ce0df2bc11fe01db7dfaded19709a982c2ec23a0814353b`.
With a real `docker -it` PTY and `tini` entrypoint, both Pi 0.82.0 and OpenCode
1.17.11 handled a 32x100 ioctl resize and two typed turns, returned
`PHASE1_NATIVE_TUI_REPLY`, showed one native session with two user/two assistant
records, and exited cleanly (Pi Ctrl-D, OpenCode Ctrl-C). The synthetic upstream
recorded two hydrations, five model calls, and five authorized requests.

Root's separate Compose launch-failure validation used H
`sha256:f43a3a898ec3685ba7e82d85af62537fb6d441a9075e2f2d00db10da048ecbd4`, C
`sha256:b708f8306a925baf185198f0329b43699305b879eed50c25941464ae82ceff91`, and E
`sha256:a9748058bb647a3165e50930c460b0a6d84b27044ce5bb1e0b01b2790af43cf3`.
With Pi's binary deliberately missing, one real C event failed with typed
`pi binary not found`, H stayed ready, and a following no-secret GitLab
`webhook-script` completed in 18ms with cwd, HOME, and `ACH_WORKSPACE` all under
`/tmp/ach-private`; all roles had zero restarts. Its observed post-scenario usage
was H 67.7 MiB, C 64.03 MiB, E 63.41 MiB, an observation rather than a
performance limit.

These tests are deliberately referenced rather than duplicated as source-string
assertions. The repository pre-push gate excludes `tests/e2e`; the MCP e2e command
above was run separately. Missing native binaries or external services do not count
as acceptance.

## Kubernetes validation and limits

Root ran a disposable `kind` v0.31.0 cluster (`ach-phase1-root`, Kubernetes 1.35.0)
with an explicit task-owned kubeconfig, then deleted it. The first Task 10B image
digests and exact commands are recorded in
`.superpowers/sdd/2026-09-11-phase1-channels-harness-engine-split/kind-validation.md`.
The pod reached 3/3 ready with zero restarts; two real C events completed, the
OpenCode native session reference remained the same across lanes, and a third event
completed after H restart with that same session. H/E/C mounts and sentinel checks
confirmed the private state boundary. Those images predate the final Compose script
and fixture changes, so the Kubernetes evidence is reported as an independent
runtime validation, not as the final image digest.

The shipped pod manifest still needs external ach-runtime rendering and concrete PVC
provisioning. Phase 2 proxy isolation improvements and Phase 3 queue/autoscaler/S3
work remain out of scope. Shared/open egress remains an agent-wide authority, and
native process isolation is weaker inside a single ordinary container; deployment
namespaces and mounts must provide the boundary. In-memory completion/dedup state
and the existing Redis admission semantics remain: authenticated `FULL_QUEUE`
responses acknowledge and drop overload while unauthenticated responses remain
pending for recovery. Phase 2 content masking/proxy isolation is not implemented.

## Final gate

The targeted Docker test command passed after the acceptance changes:

```text
rtk proxy ./scripts/dev.sh uv run pytest tests/integration/test_split_parity.py tests/integration/test_split_failures.py tests/test_prepare.py -q
45 passed in 3.15s
```

The final unchanged repository pre-push gate and its exact result are recorded here
after the final commit. No push, merge, release, or external deployment is part of
this evidence.
