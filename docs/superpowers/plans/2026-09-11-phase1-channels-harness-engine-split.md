> Archived design history. Superseded by the [current split contract](../../references/2026-09-14-three-role-split.md). Do not use as implementation instructions.

# ACH Phase 1 — Channels/Harness/Engine Split Implementation Plan

**Execution status (2026-09-12):** Tasks 0–10 implemented on `feat/phase1-split`;
final production source and unchanged gate verified at `beac73f`. The original planning checklists below
are retained as the task instructions. Completion evidence and limitations are in
[the acceptance report](../../reports/phase1-split-evidence.md),
[independent root validation](../../reports/phase1-split-root-validation.md), and
[the decision log](../../reports/phase1-split-decisions.md). The user subsequently
authorized implementation and local validation; publication and integration into
the external operator remain outside this work.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Run channels, harness and engine in three ordinary containers in one pod while preserving existing sessions, workspace behavior and managed proxy integrations.

**Architecture:** Keep admission, terminal validation, preparation, credentials and proxies in the harness. Move existing native adapters and session mappings behind a mini-harness HTTP API, and connect channels through serializable, authenticated HTTP messages. Deliver the private-preparation correction independently; do not implement phase 2 proxy improvements.

**Tech Stack:** Existing Python >=3.12,<3.14, asyncio, Pydantic, aiohttp/httpx, FastAPI/uvicorn, SQLite, pytest, Docker and Kubernetes. Reuse current dependencies. Use stdlib hmac/hashlib for signatures and tini for container PID 1.

**Spec:** [Revision 8](../specs/2026-09-10-harness-engine-final-spec.md), including the response-signature and shared-conversation amendments.

## Global Constraints

- "One pod, one replica, three ordinary containers from the first implementation: channels, harness and engine."
- "Existing managed proxy routing and credential separation are required in phase 1; new proxy features do not block the split."
- "The harness retains **dedup → backpressure → lane**, finite concurrency/time/queue bounds, per-session FIFO and retention decisions."
- "Managed credentials never enter the execution API, engine arguments, environment, generated config, workspace, responses or ordinary logs."
- "Subsequent turns cannot reset or extend that invocation deadline."
- "No new platform ownership lease is introduced."
- "Shared/open egress remains: arbitrary curl/package traffic can bypass proxies."
- "This specification neither mandates whole-workspace replacement nor adds a configurable handoff target."
- Preserve none/auto/custom conversation semantics, existing native mappings and native TUI support. Do not scope conversation maps by lane.
- Ubuntu Docker for isolated local mode; macOS native supported. Native local mode is not same-UID hostile-process isolation.
- No init-container hydration, transport tunnel, new broker, autoscaler, S3 client, masking subsystem, general CLI proxy or framework for future runtimes.
- Planning only: this document does not authorize implementation, publishing images, changing a cluster or releasing a patch.
- At implementation time create an isolated worktree; retain unrelated working-tree files. Run Python tooling through scripts/dev.sh, not the host interpreter.
- Commands below use rtk. Commit only the task's explicit paths; never git add the entire working tree.

---

## Baseline, scope and delivery gates

Inspected baseline: v0.16.1, commit 462912f. The plan is based on source inspection; no application tests were run while writing it.

Two delivery units, in order:

1. **Security patch:** Tasks 0A–0B, independently reviewable/releasable on the 0.16.x line. The handoff prototype has a real gate because arbitrary existing prepare scripts and preservation cannot be solved by merely changing HOME.
2. **Phase 1 split:** Tasks 1–10. Intermediate commits remain internal; the delivered increment includes three containers and the single local HTTP execution path.

Tasks 0A–0B should be executed/reviewed as a separate sub-project before the transport work. This avoids making the security patch wait for the split without pretending its workspace compatibility question is already implemented.

A hard boundary for implementation: if private preparation cannot preserve an exercised current workflow, report the concrete script, input and before/after behavior. Do not silently add destructive replacement, a configurable target, conversation reset or a generic artifact framework. The decision gate is part of the plan, not permission to weaken the security rule.

## Source map and proposed files

Existing responsibilities, verified in source:

| Existing file | Role / change |
|---|---|
| src/ach_agent/main.py | Currently constructs both roles, hydration, proxies, pool, channels and native TUI; separate wiring without a wholesale rewrite |
| src/ach_agent/boot/engine_runner.py | Preparation, conversation selection, terminal policy, stats and callbacks; replace direct pool/driver calls |
| src/ach_agent/boot/prepare.py | HOME/cwd/secretEnv and cleanup; private preparation correction |
| src/ach_agent/boot/paths.py | Engine home/workDir resolution and .ach-state symlink |
| src/ach_agent/boot/stores.py | Dedup and native session map currently share state/state.db |
| src/ach_agent/channels/message_event.py | reply_future and non-serializable delivery_context currently cross the seam |
| src/ach_agent/channels/{webhook,a2a,tui,queue,cron}.py | Adapter behavior to preserve |
| src/ach_agent/http/app.py | Existing inbound/health routes; channels owns inbound routes after split |
| src/ach_agent/router/{router,lane,slots,dedup}.py | Preserve ordering and bounds; minimal lifecycle notification seam only |
| src/ach_agent/engine/base/{driver,terminal,pool,events}.py | Reuse native drivers/pool/events; keep terminal policy harness-side |
| src/ach_agent/engine/{opencode,pi}/driver.py | Native session resolution and turn execution |
| src/ach_agent/engine/{lifecycle,mcp_passthrough,context,trace,cost,mcp_proxy}.py | Launch, native configuration, hydration, process-local proxy correlation/accounting |
| src/ach_agent/config/schema.py | Explicit role configuration and bounded completion retention |
| Dockerfile, docker/testbed/docker-compose.yaml | Existing image/local integration; add isolated example without breaking testbed |
| docs/schemas/operator-contract.md, CHANGELOG.md | Compatibility and deployment contract |

New modules are deliberately bounded:

| Proposed path | Responsibility |
|---|---|
| src/ach_agent/boot/private_prepare.py | Private scratch lifecycle and file handoff selected by Task 0A |
| src/ach_agent/channels/envelopes.py | Strict JSON wire representations and correlation |
| src/ach_agent/channels/signing.py | Request/response MAC and bounded replay cache |
| src/ach_agent/channels/client.py | Signed HTTP submission/result lookup |
| src/ach_agent/boot/completions.py | In-memory event outcomes and local sinks |
| src/ach_agent/boot/conversations.py | Harness-side conversation mutual exclusion |
| src/ach_agent/boot/execution_client.py | Concrete HTTP client; no fake native driver implementation |
| src/ach_agent/boot/channels_api.py | Harness internal admission/result routes |
| src/ach_agent/boot/roles.py | Role-specific wiring and public config projection |
| src/ach_agent/boot/local.py | One launcher, child lifecycle, native TUI path |
| src/ach_agent/execution/{__init__,wire,service,app,state}.py | Mini-harness DTOs, native supervision, HTTP routes and native map |
| docker/split/{compose.yaml,pod.yaml,README.md} | Reviewable local and Kubernetes deployment examples |
| tests/execution/conftest.py | Controllable fake native driver and subprocess fixtures |

Do not extract unrelated modules just to enforce directory purity. Existing engine-named proxy modules can remain named that way while running harness-side.

## Interface decisions used by the tasks

These are phase-1 implementation choices, not a general agent protocol.

- Event identity is (agent, channel_name, idempotency_key), not the naked ID. Use the same channel namespace as router dedup. Preserve secondary dedup keys; a secondary-only duplicate without a known canonical result returns outcome-unavailable, never invents a result.
- Native mapping identity remains (engine_type, conversation_key). Harness locks that tuple for reused conversations for the whole invocation, including wrap-up, repair and maintenance. No lock is needed for independent none conversations.
- Local ordinary invocations and Kubernetes use the same ExecutionClient. Native TUI is the explicit terminal exception, not a second programmatic driver path.
- Controller HTTP stream owns all executions. Its fresh process instance ID is correlation, not peer authentication or proof against a partitioned node.
- Use NDJSON for our execution stream over HTTP; native SSE/JSONL remains within drivers. One turn per HTTP response, no native byte tunnel. Control/cancel operations use separate connections.
- v1 endpoints: POST /internal/v1/events; POST /internal/v1/results (lookup/wait by EventRef); POST /execution/v1/controller (held stream); POST /execution/v1/acquire; POST /execution/v1/turn; POST /execution/v1/session-op; POST /execution/v1/release; POST /execution/v1/cancel. All engine operations carry controller, execution and invocation IDs as applicable.
- Terminal validation calls a concrete async turn callable. It never acts on a diagnostic native reference. Mini-harness keeps the current reference on the invocation record.
- Use one result-retention setting, runtime.resultRetentionSeconds (default 300, positive bounded integer). Internal limits: 1024 completed records, 16 MiB total retained results, 1 MiB per result; active records remain bounded by maxQueuedTotal. Capacity eviction yields outcome-unavailable.
- Wire defaults: 1 MiB JSON record, 256 queued events AND 4 MiB buffered output per invocation, 32 MiB aggregate output, 30-second stalled-write timeout, 10-second cleanup deadline. Exceeding a bound cancels the affected execution; never silently truncate terminal JSON or tool results.
- These finite defaults must pass the parity fixtures. If an existing supported payload exceeds one, adjust the bounded default with measured evidence, rather than shipping a hidden regression.
- HMAC-SHA256 request input is JSON encoding of ["request", 1, method, target, timestamp, nonce, sha256(body)] with fixed separators. Response input is ["response", 1, request_nonce, status_code, sha256(body)]. Sign all behavior-changing responses. Per-response stream terminal envelopes carry their own MAC bound to the original request nonce and complete envelope bytes.
- Reject request timestamps outside 30 seconds. Keep accepted nonces for 60 seconds, capped at 4096; reject new authenticated requests with a signed retryable response if full rather than evict live nonces. Signatures do not prevent first-use relay or encrypt contents.
- CLI role selection: --role harness|channels|engine; omission retains the single-command launcher. Use ACH_HARNESS_URL, ACH_ENGINE_URL and ACH_CHANNELS_HMAC_KEY for internal wiring. Do not mount the full credential-bearing agent configuration in engine or channels.

### Task 0A: Characterize private preparation and settle its file handoff

**Files:** Read/extend tests/test_prepare.py and docs/schemas/operator-contract.md §9.1; create tests/test_private_prepare.py and docs/reports/phase1-prepare-compatibility.md. No transport code.

**Interfaces:** Existing run_prepare(cfg, event, workspace) and run_cleanup(cfg, event, workspace). Produce executable preservation/security fixtures and a concrete handoff decision for Task 0B.

- [ ] Add characterization cases using the current prepare fixture: first checkout, second event in populated checkout, dirty tracked file, untracked file, local commit, cleanup after warm expiry, failed acquisition and missing/failed cleanup. Assert workspace root inode, .ach-state resolution, HEAD, file contents and retained/deleted files explicitly.
- [ ] Add the hostile-input cases with synthetic credentials. The script runs Git in its normal preparation path; plant .gitconfig, .git/config, core.hooksPath/core.fsmonitor and a symlink to a sentinel outside workspace. Assert no marker is produced by a credential-bearing command. This is a regression harness, not proof against a malicious same-UID process.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/test_private_prepare.py -q
~~~
Expected: existing behavior is characterized; the new security cases expose the current unsafe inputs before the fix.
- [ ] Prototype the supported reference-script path: credentialed clone/fetch in fresh private HOME/cwd/checkout; no imported .git configuration; transfer approved files only after writers stop. Compare each preservation fixture with baseline. A clean first checkout alone is insufficient evidence.
- [ ] Record exact transfer operations and script changes in the report, including how dirty/untracked files and local commits behave. Reject any candidate that silently discards them beyond the existing script's behavior. No new target field and no whole-workspace delete policy.
- [ ] **Gate:** before Task 0B, the report must select a tested transfer algorithm for the supported workflows. If it cannot, stop this sub-project with the failing fixture and concrete compatibility choice. Do not proceed by claiming a generic copy is safe. This is the one remaining preparation detail intentionally delegated by SPEC §9 to implementation planning/prototyping.
- [ ] Commit only the fixtures/report once their expected baseline behavior is explicit: test: characterize preparation security and workspace reuse.

A fixture must check observable behavior, for example:
~~~python
before_inode = workspace.stat().st_ino
before_note = (workspace / "notes.txt").read_bytes()
await run_prepare(prepare_cfg, event, workspace)
assert workspace.stat().st_ino == before_inode
assert (workspace / "notes.txt").read_bytes() == before_note
assert not credential_execution_marker.exists()
~~~
Define prepare_cfg/event/workspace in tests/test_private_prepare.py using the existing test_prepare fixtures, a local Git repository and a temporary sentinel directory; never use a live forge.

### Task 0B: Ship the independent private-preparation correction

**Files:** Create src/ach_agent/boot/private_prepare.py; modify boot/prepare.py, boot/engine_runner.py, boot/paths.py as needed by the approved Task 0A algorithm; tests/test_private_prepare.py, tests/test_prepare.py; operator-contract.md, docker sample prepare scripts and CHANGELOG.md.

**Interfaces:** Implement private_prepare(cfg: PrepareBlock, event: MessageEvent, workspace: Path, scratch_root: Path) -> None and private_cleanup with the same parameters. Keep run_prepare/run_cleanup as the call sites. Scratch root belongs to harness private state; never derive it from engine HOME.

- [ ] Extend the security fixtures to cleanup and webhook-script, with concurrent engine activity simulated and no fixture reading real secrets.
- [ ] Run the targeted test command from Task 0A; confirm the new cleanup/private-root assertions fail for the intended reason.
- [ ] Implement the Task 0A transfer algorithm. Use fresh scratch per credential-bearing step, mode 0700, private HOME, explicit environment and finally-based scratch cleanup. Never pass an agent-owned cwd, HOME, tool config or checkout to credential-bearing Git.
~~~python
with tempfile.TemporaryDirectory(dir=scratch_root, prefix="prepare-") as root:
    private = Path(root)
    home = private / "home"
    checkout = private / "work"
    home.mkdir(mode=0o700)
    checkout.mkdir(mode=0o700)
    env = build_prepare_env(cfg, event, checkout)
    env["HOME"] = str(home)
    # Execute the trusted script here, then use Task 0A's verified handoff.
~~~
The comment above describes the required integration point; the deliverable is the actual verified handoff, not this scaffold alone.
- [ ] Use no-follow, destination-root-relative file operations in the handoff; refuse path escape, special files and symlink traversal. Recreate .ach-state intentionally. Ensure hooks cannot race an active writer. Do not infer quiescence solely from a completed HTTP response.
- [ ] Keep _event_value scalar/printable/repository-path validation. Reject only declared incompatible configuration; do not introduce a shell-analysis validator. Update clone-or-fetch examples to the new private-input contract where required.
- [ ] Run targeted preparation tests and config/schema tests if schema validation changed. Confirm script-only concurrency remains independent of native engine availability.
- [ ] Record the same-UID limitation and the precise script migration in the changelog. Commit: fix: isolate credential-bearing preparation inputs. Patch release remains a separate authorized action.

### Task 1: Replace channel callbacks with serializable envelopes and bounded outcomes

**Files:** Create channels/envelopes.py and boot/completions.py; modify channels/message_event.py, boot/engine_runner.py, main.py, channels/{webhook,a2a,tui}.py; minimal router/lane.py lifecycle notification; create tests/channels/test_envelopes.py and tests/test_completions.py.

**Interfaces:**
~~~python
class EventRef(BaseModel):
    agent: str
    channel_name: str
    idempotency_key: str

class Completion(BaseModel):
    ref: EventRef
    invocation_id: str
    state: Literal["queued", "running", "completed", "failed", "outcome_unavailable"]
    result: JsonValue = None
    error: str | None = None
~~~
Use Pydantic JsonValue; exclude unknown fields. EventEnvelope carries every existing MessageEvent field except reply_future, plus free_form. CompletionRegistry exposes async submit(event: MessageEvent) -> Completion, lookup(ref: EventRef) -> Completion, wait(ref: EventRef) -> Completion, and finish(ref: EventRef, result: JsonValue, error: str | None) -> None. Constructor receives the existing router.handle callable and finite limits. Local text/tool sinks live in a separate ID-keyed registry, not envelope payloads.

- [ ] Test JSON round-trip, rejection of callbacks/futures, identical raw ID in different channels, secondary dedup without a result, lost-ACK retry and cancellation of a waiter without cancellation of work.
~~~python
waiter = asyncio.create_task(registry.wait(ref))
waiter.cancel()
await registry.finish(ref, {"text": "done"}, None)
assert registry.lookup(ref).state == "completed"
~~~
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/channels/test_envelopes.py tests/test_completions.py -q
~~~
Expected first run: missing modules/interfaces; final run: all scenarios pass.
- [ ] Implement atomic per-event submission: reserve an in-flight record before awaiting router.handle; same-ID submissions join it. On ACCEPTED preserve the record; on FULL_QUEUE remove the reservation; on DUPLICATE without an existing result return outcome-unavailable. The registry does not replace dedup or make accepted work durable.
- [ ] Add minimal runner start/finish notifications; ensure timeout, queued expiration, prepare failure and script-only outcomes resolve waiters. A running result cannot stay queued forever because the engine path was bypassed.
- [ ] Remove reply_future and delivery_context callbacks. Preserve local on_text/on_tool sinks and explicit A2A task correlation through EventRef; do not substitute session_key for an A2A task ID.
- [ ] Bound completed entries, aggregate bytes, waiter counts and retention; use injected clocks for expiry tests. Do not evict active entries or let simultaneous retries consume new router slots.
- [ ] Run existing channels, router and main-wiring tests covering touched code. Commit: refactor: correlate channel delivery with serializable event IDs.

### Task 2: Preserve shared-conversation ordering without a second scheduler

**Files:** Create boot/conversations.py; modify boot/engine_runner.py; create tests/test_conversation_ownership.py.

**Interfaces:** ConversationLocks.hold(engine_type: str, conversation_key: str) -> AsyncContextManager[None]. Reference-count lock entries including waiters; remove an entry only after the last holder/waiter exits. none bypasses this lock.

- [ ] Test two lanes sharing one custom conversation: second invocation cannot resolve/use the native session until first invocation maintenance/cleanup completes. A third lane with a different conversation proceeds when existing permits are available.
- [ ] Test cancellation while waiting, timeout, exception during maintenance and entry eviction.
~~~python
async with locks.hold("opencode", "repo"):
    waiter = asyncio.create_task(enter_same_conversation())
    await asyncio.sleep(0)
    assert not waiter.done()
~~~
Define enter_same_conversation in the test to enter the same lock and set an asyncio.Event. Use barriers rather than elapsed-time guesses.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/test_conversation_ownership.py tests/router/test_bounds.py tests/router/test_fifo_lane.py -q
~~~
- [ ] Compute conversation identity before native acquisition. Hold the mutex from session resolution through all turns, discard/compact/rotate and quiescence. Keep existing lane/global/channel permits and maxInvocationSeconds; a waiter gets no fresh timeout budget.
- [ ] Do not change _NamespacedSessionMap keys or route FIFO by conversation key. Commit: fix: serialize shared native conversation access.

### Task 3: Define the public execution API and isolate native session storage

**Files:** Create execution/{__init__,wire,state}.py and tests/execution/{conftest,test_wire,test_state}.py; modify boot/stores.py and engine/base/pool.py only for explicit map ownership; add tests/test_session_store.py migration coverage.

**Interfaces:** Pydantic DTOs with extra="forbid":
- ControllerHello(version: int, instance_id: str, controller_id: str).
- AcquireRequest(controller_id, invocation_id, lane_key, conversation_key, reuse, remaining_seconds, config: PublicEngineConfig).
- ExecutionHandle(instance_id, controller_id, execution_id, invocation_id, proxy_route).
- TurnRequest(controller_id, execution_id, invocation_id, turn_id, prompt, max_tool_calls).
- SessionOperation(controller_id, execution_id, invocation_id, operation: discard|compact|forget).
- ReleaseRequest(controller_id, execution_id, invocation_id, idle_ttl_seconds).
- ExecutionEvent(kind: text|tool|usage|session_resolved|turn_done|error, execution_id, invocation_id, turn_id, payload: JsonValue).

PublicEngineConfig explicitly copies approved fields from EngineConfig: engine type, paths, model/type/params/thinking, prompt composition, steps/startup timeout, public proxy URLs, MCP templates, exclude_tools and codemem/Pi paths. It has no forward_env, resolved managed headers, full AgentConfig, SecretSource or arbitrary environment dictionary. Unknown fields are rejected.

- [ ] Test native reference round-trip as diagnostic data, tool state variants and every approved public config field. Use synthetic token-bearing excluded fields to prove they cannot serialize.
- [ ] Test that current opencode:/pi: map prefixes survive movement; use a legacy SQLite fixture containing both oc_sessions and dedup data.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/execution/test_wire.py tests/execution/test_state.py tests/test_session_store.py tests/engine/base/test_pool_namespacing.py -q
~~~
- [ ] Keep the native store in engine home at .ach-execution/sessions.db. Preserve engine home absolute paths so Pi's stored session file references remain valid. Do not rewrite native reference values.
- [ ] Perform a one-time bounded export of only oc_sessions from harness-private SQLite before engine admission. Transfer at most the existing map's 1024 entries as JSON through bootstrap; mini-harness imports transactionally only when migration has not completed. Commit the import marker with the rows; do not overwrite newer mappings on restart. A failed initial import blocks split startup with an explicit error, rather than resetting continuity. Engine never mounts the old state.db.
- [ ] Do not turn the narrowly typed migration endpoint into ongoing harness ownership of native sessions; remove/reject import once its completion marker exists. Native session IDs remain opaque to the harness.
- [ ] Commit: refactor: define execution DTOs and preserve native session storage.

### Task 4: Wrap existing drivers in the mini-harness service

**Files:** Create execution/service.py; modify engine/base/driver.py and native drivers only at session-resolution/launch seams; modify engine/lifecycle.py typed failure; create tests/execution/test_service.py.

**Interfaces:** ExecutionService.acquire(AcquireRequest) -> ExecutionHandle; turn(TurnRequest) -> AsyncIterator[ExecutionEvent]; session_op(SessionOperation) -> None; release(ReleaseRequest) -> None; cancel(controller_id: str, invocation_id: str) -> None. Constructor takes the existing driver and map. Add NativeLaunchFailed(Exception); never catch arbitrary SystemExit as a normal engine result.

- [ ] Implement a FakeDriver in tests/execution/conftest.py with explicit launch, resolve, turn, stop, discard and compact barriers. It returns existing TurnResult/OpenCodeToolUpdate values and records session refs.
- [ ] Test one invocation with main/wrap-up/repair: only first turn resolves the conversation; subsequent turns use service-held current_ref.
~~~python
assert fake.resolved_conversations == [("repo", True)]
assert fake.turn_session_refs == [native_ref, native_ref, native_ref]
assert [event.kind for event in events][-1] == "turn_done"
~~~
The fixture assigns native_ref and records each actual native driver call, including resolution before the first turn.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/execution/test_service.py tests/engine/base/test_terminal.py tests/engine/pi/test_driver.py tests/engine/test_opencode_driver.py -q
~~~
- [ ] Move native session resolution into an explicit async driver resolve_session operation, reusing current create/reuse/404-recreate logic. Preserve Pi switch_session and OpenCode recreate semantics. Store current_ref on the invocation; expose it only in diagnostic events/stats.
- [ ] Reuse EnginePool for launch/warm TTL where practical, but remove its harness-secret callbacks/accountant from the engine process. Distinguish release of an invocation from destruction of a warm server.
- [ ] Replace native startup sys.exit paths with NativeLaunchFailed at their source; service cleans the failed process and returns LaunchFailed. A failure to clean escalates to service shutdown.
- [ ] Apply independent local hard deadlines once at acquire, capped by remaining harness budget. Turn and maintenance never extend them.
- [ ] Commit: refactor: supervise native engines behind execution service.

### Task 5: Add HTTP streaming, controller lifetime and process cleanup

**Files:** Create execution/app.py, tests/execution/test_http.py, tests/execution/test_cleanup.py; extend execution/service.py; add tests/execution/fixtures/process_tree.py.

**Interfaces:** HTTP routes from the interface section; create_execution_app(service: ExecutionService) -> FastAPI. Fresh instance_id generated at process boot. Controller response stays open; no launches accepted before ownership is established.

- [ ] Test chunk-split NDJSON, oversized records, slow output reader, separate cancellation, duplicate turn ID and obsolete controller.
- [ ] Test two concurrent executions: saturate one output stream, cancel it while the second finishes. Assert bounded queued bytes and control responsiveness.
- [ ] Spawn a process-tree fixture with a detached grandchild holding an open workspace file. Check actual process death/EOF, not only a stop RPC response.
~~~python
await service.cancel(controller_id, invocation_id)
assert not child_is_alive(child_pid)
assert not child_is_alive(detached_pid)
assert service.can_accept_controller
~~~
Define child_is_alive using OS process observation in the fixture; container fallback is tested in Task 10, not simulated by setting a boolean.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/execution/test_http.py tests/execution/test_cleanup.py -q
~~~
- [ ] Implement per-invocation plus aggregate byte accounting, bounded write deadlines and independent HTTP connections for control/cancel. Buffer overflow fails/cancels the affected invocation instead of dropping data.
- [ ] On controller EOF stop all owned active and warm processes before admitting another controller. On failed cleanup close admission and exit nonzero; no per-lane quarantine state. Keep the controller FD out of subprocesses.
- [ ] In container mode tini reaps orphans and mini-harness exit must end the container. In native mode use a separate process session/group and explicit owned-child cleanup; document no PID-namespace guarantee. Never use kill(-1) on a workstation.
- [ ] A new controller connection to the same healthy instance after successful cleanup is allowed; recovery from failed cleanup requires a fresh process instance. Do not infer cross-node fencing.
- [ ] Commit: feat: add bounded execution HTTP and controller lifecycle.

### Task 6: Switch central execution to HTTP and preserve proxy tracing/costs

**Files:** Create boot/execution_client.py; modify boot/engine_runner.py, engine/base/terminal.py, engine/trace.py, engine/cost.py and native resolution seam; create tests/execution/test_client.py and tests/test_split_proxy_correlation.py.

**Interfaces:** ExecutionClient implements the same acquire/turn/session_op/release/cancel method signatures as the HTTP service (concrete client, not another backend hierarchy). Terminal policy consumes:
~~~python
RunTurn = Callable[..., Awaitable[TurnResult]]
# Required call vocabulary:
# await run_turn(prompt=..., max_tool_calls=..., on_text=..., on_tool=..., stats=...)
~~~
Refactor run_contract_turn to accept this callable plus its existing free_form, terminal_action, terminal_retries, max_tool_calls, stats and sinks. Closure binds execution/invocation IDs and monotonically increasing turn_id; no native session_ref argument is passed by the harness.

- [ ] Run existing terminal repair/free-form/step-budget tests through the HTTP client with FakeDriver, rather than creating a fake native driver in central code.
- [ ] Test cost.source=engine, proxy and none and first-model-call correlation for both engines. A diagnostic session event emitted after model traffic is too late.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/execution/test_client.py tests/test_split_proxy_correlation.py tests/engine/base/test_terminal.py tests/engine/test_trace.py tests/engine/test_cost_integration.py tests/stats/test_pi_turn_stat_parity.py -q
~~~
- [ ] Harness creates the public proxy route token and begins trace/accounting before turn start. The mini-harness reports session resolution and waits for a controller acknowledgement before making the first model request. Add a single session-ready acknowledgement operation to the controller API, scoped to invocation/turn; this moves the existing trace.set_session ordering across processes without letting engine headers choose arbitrary harness identity.
- [ ] Implement that operation as POST /execution/v1/session-ready with controller_id, execution_id, invocation_id and turn_id. ExecutionClient validates the pending event, calls harness trace.set_session, then acknowledges it. If acknowledgement fails, no native prompt is sent and normal deadline/cleanup applies. Repeat after a native 404/recreate changes the reference.
- [ ] Keep CostAccountant and proxy registries harness-side. Engine sends existing usage statistics; harness combines them exactly as before and drops correlations when execution closes. Token remains correlation/ambient routing, not a newly claimed local authorization boundary.
- [ ] Send discard/compact/forget operations by invocation ID. Keep terminal validation, memory preparation, tool sinks, cost policy and stats generation central.
- [ ] Replace runner pool.cleanup coupling with harness-owned workspace cleanup after confirmed release/expiry notification. Warm expiry must notify harness before its hook runs; on endpoint loss cleanup waits for execution replacement under the agreed scope.
- [ ] Commit: refactor: drive engine execution over HTTP without session regressions.

### Task 7: Authenticate channel HTTP and all delivery-changing responses

**Files:** Create channels/signing.py, channels/client.py, boot/channels_api.py; extend boot/completions.py; tests/channels/test_signing.py, tests/channels/test_internal_http.py and tests/channels/test_queue.py.

**Interfaces:** request_mac(key: bytes, method: str, target: str, timestamp: int, nonce: str, body: bytes) -> str; response_mac(key: bytes, request_nonce: str, status: int, body: bytes) -> str. ChannelsClient.submit(EventEnvelope) -> Completion and wait(EventRef) -> Completion. Harness app consumes the Task 1 registry.

- [ ] Write test vectors binding method, target, timestamp, nonce, exact body and response status; changing any bound value fails verification.
- [ ] Test nonce-window boundaries/cache saturation, same-event/fresh-nonce retry, signed FULL_QUEUE and forged FULL_QUEUE. An invalid MAC must raise a transport/authentication exception before the Redis adapter can call XACK.
~~~python
with pytest.raises(SubmissionFailed):
    await client.submit(event)
assert redis_mock.xack.await_count == 0
~~~
Define SubmissionFailed in channels/client.py as the explicit client exception. Use the existing QueueConsumer test mock and route the forged response through the actual client.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/channels/test_signing.py tests/channels/test_internal_http.py tests/channels/test_queue.py tests/channels/test_a2a.py -q
~~~
- [ ] Implement signatures on ACCEPTED, DUPLICATE, FULL_QUEUE, outcome-unavailable, rejections and terminal results. Unknown or malformed responses never drive ACK/drop. Bound raw-body reading before parsing; no arbitrary callback URL.
- [ ] Preserve submission ambiguity semantics: each retry keeps event identity and gets a new request nonce. Source ACK behavior remains unchanged only after an authenticated response.
- [ ] Keep terminal response verification separate from best-effort progress streaming. Verify event/invocation IDs before resolving waiters. Result TTL loss is explicit, never a fresh execution.
- [ ] Add config/release-note text for current admission ACK and FULL_QUEUE drop despite ackMode: onComplete.
- [ ] Commit: feat: connect channels with signed submissions and outcomes.

### Task 8: Separate role boot, public hydration and engine environment

**Files:** Create boot/roles.py and boot/local.py; modify main.py, boot/paths.py, config/schema.py, engine/context.py, engine/mcp_passthrough.py and Dockerfile entrypoint; tests/test_split_roles.py, tests/test_split_hydration.py, tests/test_main_wiring.py, tests/config/test_schema.py.

**Interfaces:** run_harness(cfg: AgentConfig), run_channels(channel_config: JsonValue), run_engine(public_config: JsonValue) async entry functions. build_role_configs(cfg: AgentConfig) -> tuple[dict, dict] returns channels configuration and public engine bootstrap; the full cfg stays harness-side. Internal URLs are role environment, not serialized credential dictionaries.

- [ ] Test channels and engine starting before harness: bounded retry, zero native processes until acquired work, hydration success before launch. Script-only work needs no native engine.
- [ ] Test synthetic source/prepare/proxy credentials exist only in intended roles. A full-config mount into engine/channels fails the manifest test even if the process chooses not to read it.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/test_split_roles.py tests/test_split_hydration.py tests/test_main_wiring.py tests/config/test_schema.py tests/engine/test_mcp_passthrough.py tests/test_secret_forward_guard.py -q
~~~
- [ ] Keep hydration requests and context fetch with harness credentials. Publish only public prompts/artifacts/skills to an engine-readable context volume, mounted at paths preserving .ach-state and skill discovery. Engine home stays engine-owned; harness must not populate secrets into it.
- [ ] (Superseded by the 2026-09-12 operator contract.) Preserve nonempty forwardEnv in split mode as sanitized engineEnvNames. Pass MCP templates with env references unresolved; mini-harness resolves against its own explicit environment. No runtime environment copied from harness wholesale in local mode either.
- [ ] Move inbound HTTP/webhook/A2A/cron/Redis startup to channels. Keep source auth there and script/prepare configs plus their secrets in harness. Standalone roles receive filtered config artifacts supplied by deployment; local launcher writes those artifacts into separate directories before starting children.
- [ ] Keep model/MCP/A2A proxies unchanged in behavior and harness-side. Explicitly retain memory facades and codemem engine-owned database placement; public context is not a route to mounted tokens.
- [ ] Implement local launcher process startup, teardown and signals. Preserve --prompt and --debug over HTTP. For native OpenCode TUI keep attach; for native Pi TUI run the engine-role terminal mode as a child owning the terminal, with sanitized env and public bootstrap. Docker Pi uses docker exec -it in engine container; do not build remote PTY streaming.
- [ ] Add role health: harness liveness checks its event loop, readiness needs hydration and engine endpoint health; native LaunchFailed does not make script execution depend on native readiness. Channels readiness checks authenticated harness connectivity. Engine endpoint health requires no native process.
- [ ] Regenerate frozen schema if changed:
~~~sh
rtk make schema
~~~
- [ ] Commit: refactor: launch isolated roles with public engine configuration.

### Task 9: Render three-container images and shared mounts

**Files:** Modify Dockerfile; create docker/split/{compose.yaml,pod.yaml,README.md}; create tests/test_split_manifest.py; update operator-contract.md and README.md. No live cluster mutation.

**Interfaces:** Harness/channels role images share the Python base; per-engine targets engine-opencode and engine-pi include mini-harness+tini and the appropriate native dependencies. Harness retains Git/script tools. No download/init-container endpoint injection.

- [ ] Test YAML shape: exactly three ordinary containers, no initContainers, automountServiceAccountToken false, no shared PID namespace, no host networking, no runtime socket mounts, restricted security contexts and correct role environments.
- [ ] Test persistence-enabled and ephemeral mount maps, including custom engine.home/workDir. Harness has private state/scratch, engine has private home, shared workspace and approved public context only; channels sees neither private state nor workspace.
- [ ] Run:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/test_split_manifest.py -q
rtk proxy docker compose -f docker/split/compose.yaml config --quiet
~~~
- [ ] Implement Compose with channels and engine using network_mode: service:harness; publish external channels ingress through that shared namespace. Internal endpoints bind 127.0.0.1. Use task-owned named volumes and no host-network fallback.
- [ ] Implement Pod volume mounts using existing mountPath-derived subpaths where sufficient. Create missing private subdirectories in harness startup before reporting readiness; engine retries until its directory layout is ready. Do not require an init container or a broad parent mount into engine.
- [ ] Document an operator rendering handoff: the example validates this repository's contract, but production ach-runtime rendering is a separate repository change, not implicitly delivered by adding YAML here.
- [ ] Preserve declared persistence rather than substituting emptyDir for everything. Use a single appropriate PVC with private subpaths where configured; no engine access to the parent holding dedup.
- [ ] Run image --version smokes for Pi/OpenCode/codemem as applicable and verify tini is PID 1, mini-harness its launched child. No credential is needed for binary smokes.
- [ ] Commit: feat: package one-pod three-container execution.

### Task 10: Validate the integrated split and prepare the implementation handoff

**Files:** Create tests/integration/test_split_parity.py, tests/integration/test_split_failures.py and scripts/test-split.sh; extend existing tests/e2e/test_pi_e2e.py as needed; create docs/reports/phase1-split-evidence.md; update CHANGELOG.md and operator-contract.md.

**Interfaces:** Reuse ExecutionClient, ChannelsClient, FakeDriver and the docker/split deployment. scripts/test-split.sh owns a uniquely named Compose project, synthetic upstream fixtures and its cleanup trap; never targets existing user containers.

- [ ] Implement a deterministic real-process smoke scenario with local fake model/MCP/forge upstreams, then run the same scenario with actual Pi and OpenCode. Reuse the existing e2e fixtures; missing binaries/upstreams are recorded as not-run and do not count as release evidence.
- [ ] Verify the named parity inventory: text, tool-state shapes, usage and session stats, step abort/wrap, terminal repair, discard/compact/rotate, none/auto/custom, warm TTL and native TUI.
- [ ] Demonstrate two lanes/same custom conversation, another independent conversation, saturated streaming and cancel. Test cancellation racing completion, repeated execute ID, cumulative repair deadline and cleanup escalation.
- [ ] Kill/drop the controller while children run; verify owned process cleanup and new-instance recovery, without replaying prompts. Force native LaunchFailed and show the harness survives and script-only work proceeds; then break the engine container and show pod readiness correctly fails.
- [ ] Verify first provider call correlation/cost with engine/proxy/none modes and all managed proxy routes with synthetic credentials. Engine may call permitted proxies but cannot read harness/action secrets or state through mounts or ordinary process interfaces.
- [ ] Verify previous session mappings survive upgrade and second review continues its native conversation; workspace contents/links satisfy Task 0A's characterization.
- [ ] Run targeted integration checks:
~~~sh
rtk proxy ./scripts/dev.sh uv run pytest tests/integration/test_split_parity.py tests/integration/test_split_failures.py -q
rtk proxy bash scripts/test-split.sh
~~~
- [ ] After targeted checks pass, run the repository's final integration gate once for the completed increment:
~~~sh
rtk make lint
rtk make test
rtk make conformance
~~~
These commands are implementation-time checks, not executed by this planning task. Run the repository's configured secret-scanning/pre-push checks before a separately authorized merge/release.
- [ ] Record command, environment, result and limitations in the evidence report. For the Kubernetes example, test in an explicitly designated disposable cluster before claiming Kubernetes acceptance; rendering/Compose is not sufficient evidence.
- [ ] Record startup/idle footprint and streaming/cancel latency as measurements, without inventing performance thresholds.
- [ ] Commit: test: verify split parity security and lifecycle. Review changed files and release notes; no automatic deployment, merge or version bump.

## Spec-to-task coverage

| SPEC v8 requirement | Tasks |
|---|---|
| Three roles, local launcher, ordinary containers | 8–10 |
| Native API remains inside engine; central terminal policy | 3–6 |
| Lane/conversation distinction, shared conversation serialization | 2–4, 6, 10 |
| Hydration before launch; public context and path preservation | 0A–0B, 8–10 |
| Serializable channels, outcomes, bounds, replay ambiguity | 1, 7 |
| All source-action responses authenticated, forged FULL_QUEUE | 7, 10 |
| Controller lifetime, LaunchFailed, cumulative deadline, no replay | 4–6, 10 |
| Warm reuse versus stop confirmation and cleanup escalation | 4–6, 10 |
| Private state and native map continuity | 3, 8–10 |
| No managed engine secrets; passthrough engine env | 3, 8–10 |
| Private credential preparation and script-only behavior | 0A–0B, 1, 8, 10 |
| Existing Redis semantics and operator documentation | 7, 10 |
| Text/tools/usage/TUI/maintenance parity | 4, 6, 8, 10 |
| Existing trace/cost/proxy parity across process memory boundary | 6, 10 |
| Phase 2 and phase 3 deferred | Excluded explicitly; no implementation task |

## Plan self-review and execution status

- Source paths above were checked against 462912f; proposed files are explicitly marked as new.
- API names, event identities and method signatures are shared between tasks; there is one concrete HTTP execution path.
- Phase 1 does not reset conversations, redefine workspace retention or expand proxy features.
- The preparation handoff is a required prototype gate, not a falsely completed design. Do not start transport work until its concrete preservation/security decision is recorded.
- Cross-process trace/cost ordering and warm-expiry cleanup are included because source inspection found process-local coupling; they are parity work.
- The production operator integration needs its own rendering change; no local manifest can silently complete another repository.
- No implementation or tests have been executed by writing this plan.
