# ACH Channels–Harness–Engine — Implementation Review Specification

**Revision:** 8 · 2026-09-11
**Status:** Architecture consolidated for implementation planning, incorporating the final review with the corrections below. This document does not authorize implementation or claim external sign-off.
**Audience:** Reviewers without access to ACH code.
**Supersedes:** Revisions 1–7 and the earlier HTML companion. Filename retained for existing links. Code baseline reviewed: v0.16.1 (`462912f`).

## 1. Objective and deployment

**One pod, one replica, three ordinary containers from the first implementation: channels, harness and engine.** No new inter-component broker, autoscaler, persistence system or application init container. Acceptance/results remain in memory; existing dedup persistence and source queue semantics remain as described below.

Phase 1 separates components while preserving current behavior, including conversation continuity and workspace reuse. Phase 2 improves proxy policy and content protection. Phase 3 records deferred ideas for evaluation, not an obligation to build them. Existing managed proxy routing and credential separation are required in phase 1; new proxy features do not block the split.

- **Channels:** source authentication, inbound adapters, event normalization and result delivery.
- **Harness:** ACH hydration, logical sessions, admission/router, execution credentials, preparation and capability proxies.
- **Engine:** our mini-harness supervising Pi/OpenCode, owning their native protocols and native session state.

Local use keeps one launcher, which starts the mini-harness as a child process. All programmatic engine execution uses the same HTTP API as Kubernetes. Channels may use an in-process adapter over the same serializable envelopes. Native local mode provides no hostile-engine isolation guarantee. Native TUI attachment is an explicit terminal path: OpenCode attach uses its native server; Pi requires its native terminal attachment. Neither introduces a second programmatic driver path or a remote TUI guarantee.

The roles can reuse image bases; separate env, mounts and entrypoints define the deployment. Per-engine images include the mini-harness at build time. Ubuntu Docker isolated mode shares the harness container's network namespace among all three roles; it does not use host networking. macOS native use remains available.

## 2. Boundaries

    Channels container
      source adapters / authentication / result delivery
              │ internal HTTP: events and correlated results
              ▼
    Harness container
      hydrate / logical sessions / admission + router
      credentials / capability proxies / private preparation
              │ our HTTP execution API + streamed events
              ▼
    Engine container
      tini (PID 1) → our mini-harness (only direct child)
        ├─ Pi integration       → native stdio → Pi
        └─ OpenCode integration → native HTTP/SSE → OpenCode
              │
              └─ model / MCP / A2A calls → harness proxies

The central harness may know which engine configuration is selected, but does not parse Pi JSONL or OpenCode SSE, manage their native conversation IDs, or call their native APIs directly. The mini-harness reuses existing native integration where practical.

HTTP carries our execution semantics. It is not a raw OpenCode byte tunnel. There is no custom Unix framing, HTTP multiplexing tunnel, proxy-socket bridge or IPC-volume layout in this revision.

The two internal APIs have different contracts: channels submit events; the harness requests engine execution. Reusing HTTP does not merge their responsibilities.

## 3. Session and execution ownership

- **Lane key (`session_key`):** harness-owned FIFO and execution-pool identity.
- **Conversation key:** native-session reuse identity derived from `channel.session` (`none`, `auto`, `custom`); distinct from the lane key.
- **Native session ID/state:** Pi/OpenCode-specific identity and files, managed by the mini-harness. Its conversation-key mapping lives under engine home. Native references may appear in diagnostic turn statistics; the harness does not use them to drive native APIs.
- **Execution ID:** identifies an acquired native execution.
- **Invocation ID:** identifies an admitted unit of work, containing one or more turns on its current native session. Main turn, step-budget wrap-up and terminal repair continue that session.

IDs are correlation data, not credentials. The harness accepts results only for work it requested.

The harness retains **dedup → backpressure → lane**, finite concurrency/time/queue bounds, per-session FIFO and retention decisions. Mini-harness launches, retains, stops and reaps requested engines and enforces process limits; it introduces no second work queue or scheduler.

Terminal-contract validation and decisions to repair, compact, discard or forget a conversation mapping remain harness-side. The mini-harness performs the corresponding native operations. Token-overflow rotation and warm idle-TTL reuse retain their current meaning.

Conversation mappings retain their existing engine-type/conversation-key scope; adding the lane key would change reuse semantics. Different lanes may therefore select the same conversation. The harness must serialize access to a reused conversation across those lanes, covering mapping resolution, all invocation turns, native session maintenance and confirmed cessation of invocation activity. Use mutual exclusion within the existing bounded execution path, not another scheduler or unbounded queue; waiting remains subject to cancellation and the invocation deadline. Unrelated conversations remain concurrent. `session: none` creates independent conversations. This is a targeted concurrency correction to verify during implementation, not a claim that current per-lane FIFO already provides it.

Moving native adapters must not move credential resolution or upstream authorization. Mini-harness receives public proxy URLs, approved context, engine options and workspace paths, and generates native engine configuration without managed secrets.

## 4. Startup and hydration

All three ordinary containers start independently:

1. Channels starts its source adapters and connects/retries to the harness.
2. Mini-harness exposes its execution API with zero Pi/OpenCode processes.
3. Harness hydrates from ACH, resolves capabilities/context, starts model/MCP/A2A proxies and prepares public engine bootstrap data.
4. Harness checks mini-harness API compatibility and establishes a controller lifetime. It sends no launch request before bootstrap is complete.
5. After admitted work and preparation, harness requests execution; mini-harness opens the native session and checks native readiness.

**Harness bootstrapped**, **mini-harness reachable** and **engine execution ready** are different observations, not a separate multi-stage notification protocol. Port existence alone is not readiness. Startup retries have a bounded deadline; engine binaries need not exist in the harness image for hydration.

Only approved engine-needed hydration outputs are shared. Harness private manifests/home/secrets remain private. Mini-harness manages native files in engine home/workspace.

The current layout defaults to `<mountPath>/home` with persistence enabled, otherwise `/tmp/ach-home`; workDir defaults to `<home>/workspace`. Preparation creates a session-keyed workspace and links `.ach-state` to hydrated prompts/artifacts under home. Workspaces can survive successive events; they are not merely temporary hydration buffers. Preserve configured paths, retention and link resolution when rendering mounts. Containers share explicitly mounted volumes, not their whole root filesystems. Use ephemeral or persistent storage according to the existing configuration; do not force an emptyDir-only lifecycle or add a storage subsystem for the split.

Per-event channel.prepare remains on the harness lane after admission, before invocation. It is neither hydration nor a channel-container startup operation.

## 5. Channels API

Channels is physically separate in Kubernetes immediately. Local mode can use an in-process adapter with the same serializable envelopes.

| Envelope | Required semantics |
|---|---|
| Event | Stable event/idempotency ID, target agent, source channel, validated identity/context, existing session-routing inputs, payload, `free_form` and correlation |
| Submission outcome | Accepted/rejected/retryable with reason and event ID; invocation ID if assigned; current queued/running/completed state for an existing event |
| Completion | Event/invocation correlation, terminal status and serializable result/error |

No future, callback, live HTTP object or engine handle crosses this boundary. `reply_future` and `delivery_context.on_complete` / `on_fail` become an ID-keyed completion registry. Harness-internal `on_text` / `on_tool` sinks stay internal. Preserve existing session derivation, dedup identifiers and channel-specific acknowledgement/reply behavior. Removing callbacks does not remove `_event_value` scalar conversion or printable/repository-path validation.

Use direct HTTP submission, with a correlated response/event stream when the source waits for completion. No durable result service or arbitrary source-provided callback URL is introduced.

Harness authenticates channels-service requests and validates target scope. A platform-injected per-pod HMAC key exists only in channels/harness environments. Sign method, request target, body, timestamp and nonce with an unambiguous encoding; verify signatures, a short validity window and a bounded nonce cache. Retries use a fresh nonce with the same event ID. Use standard HMAC primitives and constant-time verification; no reusable bearer is sent.

Channels must verify signatures on every response that changes source acknowledgement or delivery behavior: acceptance, rejection (including FULL_QUEUE), duplicate, outcome-unavailable and authoritative terminal outcomes. Bind the response status and body to the request nonce and event/invocation correlation. Missing or invalid authentication is a submission failure, never an instruction to acknowledge or discard work. An impostor must not be able to acknowledge or complete work merely by binding the harness port. Streaming progress is not an authoritative acceptance or terminal result. Exact signed-message encoding belongs to implementation planning.

This authenticates messages, not the TCP peer, and does not encrypt content. An impostor can observe or relay a valid request before its nonce has been accepted; HMAC does not prevent that relay. The nonce cache rejects previously accepted requests within its lifetime, not across loss of in-memory state. Event deduplication still applies. No TLS or new certificate infrastructure is introduced.

Unavailable harness means failure/retry according to the source contract, not an accepted message silently discarded. Ambiguous submissions retry with the same ID. Acceptance is not completion or durable storage: current in-memory failure limitations remain, with no exactly-once external-effects promise.

Resubmitting an existing event returns its state and assigned invocation ID, plus its terminal result when retained. A synchronous waiter can reattach by event ID; channels disconnect only detaches that waiter and never cancels admitted work. Retain terminal outcomes for a configurable bounded window, with finite entry/byte limits; active entries are bounded by admission. Expiry or capacity eviction returns an explicit outcome-unavailable response. A retained dedup record without an outcome must not trigger re-execution or invent success. Harness restart can lose accepted work and results; this is not a durable result service.

Future queue consumption must feed the existing router, not another scheduler.

**Existing Redis queue adapter:** the Redis Streams consumer, consumer-group pending-entry recovery and current acknowledgements move with channels. Preserve actual behavior: accepted/duplicate submissions are acknowledged at admission, not execution completion; authenticated FULL_QUEUE follows the existing acknowledge-and-drop path; submission exceptions or unauthenticated responses remain pending for recovery. The `ackMode: onComplete` label does not change that implementation behavior in this increment. Configuration documentation and release notes must state that admission ACK can lose work before completion and overload intentionally drops messages. Changing these source semantics is separate work, not an incidental change in the HTTP split.

## 6. Mini-harness execution API

Our HTTP API expresses:

- Controller connection and fixed API-version compatibility.
- Open/acquire session with public bootstrap configuration and workspace.
- Begin invocation, then execute turns on its current session and stream required events/results.
- Cancel invocation; release/close session.
- Discard or compact the native session; forget a conversation-key mapping.
- Report native launch/readiness failure, process exit and cleanup outcome.

Exact URI names and DTO spelling belong in implementation planning. Session/execution/invocation/controller correlation and the behavior below are required.

Use HTTP operations and independent streamed responses/SSE for invocation output. Mini-harness translates native text, tool/lifecycle and usage events and reports turn completion. The harness validates the terminal contract. There is no engine question/approval return path in this increment: current question/permission/elicitation settings remain in force. Section 11 defines the concrete parity inventory.

Buffers and response sizes are bounded per stream and in aggregate. Heavy output or a slow reader must not prevent cancellation or another session's progress. A stalled invocation stream fails that invocation rather than blocking the API or accumulating unlimited output.

A dedicated long-lived HTTP connection defines the controlling harness lifetime; associated requests reference that controller, with one owner at a time. This is correlation within the agent-wide engine scope, not authentication against a compromised engine. Control descriptors must not leak into child processes.

Controller loss closes launch admission and fails active invocations without replay. Mini-harness cleans every owned execution before another controller is accepted. Lost invocation streams fail their respective invocations; controller loss affects all owned executions. No transparent reconnect, replay history or native-session adoption across controller loss is implemented.

Controller loss means transport closure, not a new heartbeat. The harness liveness probe must detect an unresponsive controller process and force its restart. Invocation admission carries the remaining `maxInvocationSeconds` budget; the mini-harness establishes a local hard deadline and enforces termination when it expires. Subsequent turns cannot reset or extend that invocation deadline. The harness also enforces its deadline; these are bounded execution timers, not ownership fencing.

## 7. Failure and cleanup

Native startup failure returns typed **LaunchFailed**. Mini-harness kills/reaps the failed process; central harness fails that invocation without exiting or restarting unrelated healthy sessions.

Cancellation and cleanup use bounded deadlines. `tini` is PID 1 and reaps orphaned zombies; process-group signalling alone does not prove detached descendants stopped. Mini-harness must terminate owned live processes and establish cleanup before reuse. If that cannot be established, it refuses new work and exits. `tini` then exits, causing remaining processes in the container PID namespace to be killed; platform supervision handles replacement. `tini` is not a hydration init container and does not itself establish successful cleanup of live descendants before exit.

| Transition | Required ordering |
|---|---|
| Successful final turn | Harness validates the outcome; mini-harness confirms invocation activity has ceased; lane is released after required cleanup. A healthy idle native process may remain for warm reuse. |
| Cancel or stalled execution stream | Request cancellation, stop the affected execution and confirm no remaining invocation writers, then finish cleanup and release the lane. A failed stream alone is not stop confirmation. |
| Cancel races completion | First accepted terminal outcome wins; ignore the later outcome, but still establish quiescence/cleanup before lane release. |
| Duplicate live execute or obsolete controller | Reject by invocation/turn or controller identity; do not execute twice. A subsequent distinct turn is allowed only after the previous turn completes. |
| Cleanup deadline expires | Mini-harness closes admission and exits; controller loss fails all affected invocations. Rearm execution when a new mini-harness instance accepts the controller, not by reconnecting to the failed instance. No per-lane quarantine state is introduced. |
| Controller disconnect | Stop all owned executions, including warm ones, and reap before accepting another controller; otherwise exit. |

Terminal outcome, cessation of invocation writes and native process exit are distinct observations. Successful warm reuse need not kill a healthy idle process. Cleanup failure blocks engine admission as a whole until a new mini-harness instance accepts the controller. Its startup identity distinguishes it from the failed instance; this is supervised recovery under the single-pod model, not proof of termination on a lost node or cross-node fencing.

This is supervised cleanup, not independent enforcement against a fully compromised mini-harness refusing to cooperate. Containment failure or endpoint unhealthiness can require platform replacement. Replacing shared execution explicitly fails all affected invocations.

Ordinary containers have no guaranteed shutdown order. Stop admission, attempt graceful completion/cancellation, then rely on runtime/platform termination. Pod API deletion does not prove an unreachable node stopped execution. Completed external effects cannot be undone by disconnecting.

**No new platform ownership lease is introduced.** Existing governance/revocation remains, but automatic authorization shutdown during a platform partition is not promised. Revocation of one gateway key is not asserted to fence raw forge credentials or every MCP/A2A path. The earlier unresolved lease dependency is removed together with that guarantee.

## 8. Credentials, proxies and security

Managed credentials never enter the execution API, engine arguments, environment, generated config, workspace, responses or ordinary logs. Values legitimately required by the engine come from its own configured environment. Reject `engine.forwardEnv` in split mode; resolve passthrough `${env:NAME}` in the mini-harness from engine-container environment, never from harness environment. Operators may deliberately expose credentials through that engine environment; record exposure without values. Local child launch likewise uses an explicit environment, without inheriting managed harness secrets. No new credential broker is designed here.

Managed model/MCP/A2A integrations use harness loopback proxies. Content filtering may be disabled without disabling routing or authorization. Masking/restoration remains a gateway-side configuration decision, not a new harness subsystem.

Engine processes share agent-wide ambient capability authority. Session directories/IDs are not hostile-session isolation; different agents keep separate private execution state.

Render three ordinary containers with:

- Pod-wide service-account token automount disabled.
- Separate root/PID namespaces, no shared process namespace or host PID/network mode.
- Non-root users, restricted capabilities, no privilege escalation and appropriate seccomp.
- Shared engine workspace/context only where required; no engine access to harness scratch, secrets or runtime sockets.
- Channels limited to ingress/service credentials and adapter configuration; no engine home or preparation scratch.

Harness dedup/logical state and preparation scratch have a private mount. Engine home, native files and conversation-key mapping have an engine-only mount. Only the workspace handoff target is shared writable; approved context may be mounted read-only into engine. Never mount a common parent exposing harness state to engine. Distinct volumes or correctly scoped subpath mounts may implement this: two new operator fields/PVCs are not required unless existing configuration cannot express it. Local native mode keeps separate subtrees without claiming same-UID isolation. Existing session mappings must migrate to engine storage during upgrade without silently discarding reuse.

All three share pod networking. Internal APIs/proxies bind loopback; intentional external ingress/health/metrics listeners bind as required. Inventory routes and enforce authentication on channels submission and privileged harness actions. No loopback authentication is added to the mini-harness or capability proxies; no per-container network isolation is claimed. Upstream ACH authorization remains enforced; local proxy path tokens are not claimed as an authorization boundary.

Shared/open egress remains: arbitrary curl/package traffic can bypass proxies. Managed routing is not universal interception or data-loss prevention.

## 9. Private preparation and script-only work

Private preparation is an independent security correction for the current deployment, scheduled before transport work. It is not deferred until the split.

Credential-bearing steps use harness-private scratch, including cwd, HOME, tool configuration and Git checkout. No credential-bearing tool consumes agent-mutated configuration. Harness image retains Git and tools required by supported operator scripts.

Hand approved files into execution without credentials, authorization headers, credential-bearing remote URLs or helper state. Preserve existing workspace/session reuse and the workspace root inode. This specification neither mandates whole-workspace replacement nor adds a configurable handoff target. The existing checkout convention is `$ACH_WORKSPACE/repo`. Implementation planning must define the private-checkout handoff against existing prepare/cleanup behavior and verify that it does not silently discard retained work. Credential-bearing Git must never fall back to the agent-mutated checkout to preserve compatibility.

Handoff happens only after previous writers have stopped. Reject paths escaping the target, never follow source symlinks, and do not traverse engine-planted destination symlinks. Recreate harness-owned `.ach-state` links deliberately rather than copying them from input. Cleanup hooks also receive private HOME/cwd/config and never run credential-bearing tools in the engine workspace or its mutable parent.

Reject declared configurations requesting the legacy shared credential-bearing cwd/HOME contract, and update shipped examples. Arbitrary shell cannot be proved safe by configuration validation: supported operator scripts must comply with the private-input contract. Trusted script provenance does not make its inputs trusted.

In the current single-container, same-UID mode, a private directory is not an OS security boundary against a concurrently malicious engine. This correction removes the contaminated Git/configuration input path; it does not make live credentials or scratch inaccessible to that process. Separate PID/root namespaces and private mounts in split mode provide that boundary. Do not describe the independent patch as complete hostile-engine isolation.

Publication uses credential-free artifacts and a clean private checkout; never execute imported content with publisher credentials.

**webhook-script** remains harness-side with fresh private per-run scratch, **maxConcurrentScripts**, no engine-concurrency slot and no mini-harness dependency. Results return through channel correlation. Script-only work proceeds when the native engine is unavailable but the mini-harness container is healthy. A broken engine container can make the pod unready and requires platform replacement; no ingress availability through that failure is promised.

Git remote rewriting to a harness reverse proxy remains a bounded extension direction, not a prerequisite once private preparation exists. General gh/glab API interception, SSH/LFS support and transparent CLI brokering are outside this increment.

## 10. Phase 2 proxy improvements and phase 3 deferred ideas

**Phase 2 — proxy improvements:** keep this as planned follow-on work, separate from the split's delivery gate. Build on harness-owned model/MCP/A2A routing to add explicit capability policy and gateway-side content filtering, including secret masking/restoration where supported and validated. Define which values are protected and test both leakage prevention on the managed route and functional correctness. No claim covers arbitrary direct egress. Phase 1 retains existing routing and upstream authorization; it does not introduce a new masking subsystem.

**Phase 3 — evaluate when needed:** credential-free Git/gh/glab integrations, broader traffic interception or Envoy, restricted egress, hardened runtimes, stronger session isolation, separate channels deployment with autoscaling, and S3 artifacts. These remain recorded ideas, not prerequisites or automatic implementation commitments. Rejected duplicate mechanisms (per-lane quarantine, general shell safety validation, custom transport framing) are not a future backlog by default.

**Preparation configuration ownership — discussion recorded, not decided:** consider agent-level preparation/cleanup defaults with channel-specific selection or overrides. Configuration placement is distinct from trusted execution placement: the harness coordinates preparation using normalized channel event variables, regardless of where a hook is declared. A GitLab review may prepare the repository identified by the event, while another channel registers a webhook without invoking an engine. Per-event workspace preparation must remain distinct from one-time hydration and native-engine startup, especially with warm session reuse. Phase 1 preserves current channel-specific hook configuration, selection, event variables and lifecycle; it adds no agent-level hook field or precedence rule. Any future change must define those semantics and preserve credential isolation.

**Future execution queue and autoscaling:** later move channels outside the execution pod and define durable dispatch to harness/engine. A platform-owned autoscaler, potentially KEDA, observes work and starts execution. No new broker/scaler/threshold is selected now. Today's single pod cannot keep channels alive while scaling that same pod to zero.

Later work must define durable acceptance, delivery recovery, deduplication, ordering, result persistence and safe draining. Events arriving during shutdown must remain retained. Channels/harness still need no Kubernetes administration access.

**S3 session artifacts:** later recover session files, start a fresh native process and export consistent state on close. Mini-harness knows native state; harness owns logical session identity. This restores files, not process memory. Archiving all HOME is not assumed sufficient or safe.

State selection, consistency, engine-version compatibility, concurrent publication and storage authorization need their own concrete design. No S3 client, catalog, snapshot API or storage credential is added now. Current session identity must simply not assume a permanent PID or permanently mounted disk.

## 11. Acceptance

| Area | Required evidence |
|---|---|
| Deployment | Three separate roles in one pod, intended env/mounts, no init/native-sidecar ordering |
| Channels | Serializable events/results, no futures/lambdas/handles; existing acknowledgement/reply behavior retained |
| Submission failure | Lost acknowledgement followed by same-ID retry returns existing state/result; waiter reattaches without executing again; disconnect does not cancel work; expired/evicted/lost outcome is explicitly unavailable |
| Channels authentication | Stop harness and bind its port from engine: no reusable secret is disclosed and forged acceptance/completion is rejected; retry to real harness with same event ID succeeds. Verify signature binding, replay-cache/window checks and bounded cache behavior without claiming relay prevention |
| Central neutrality | Our API is used centrally; native parsing/session management resides in mini-harness |
| Startup | Either execution container starts first; hydration precedes launch; zero-engine idle state works |
| Compatibility | Existing local Pi/OpenCode features and native TUI remain under one launcher |
| Streaming | Heavy output/slow consumer does not block cancel or another session; bounded memory |
| Launch failure | LaunchFailed cleans one process and fails its invocation without harness restart |
| Controller loss | All owned executions, including detached descendants, stopped before new controller; cleanup failure exits/replaces endpoint; no replay; hung controller is detected by liveness and invocation deadline still terminates execution |
| Ordering | Cancel/completion race has one terminal outcome; no lane reuse before quiescence/cleanup; duplicate turn rejected; warm idle reuse preserved; cleanup timeout fails all affected invocations and blocks engine admission until a new mini-harness instance accepts controller |
| Credentials | In split mode, synthetic managed credentials unreadable/unreturned to engine; forwardEnv rejected; passthrough refs resolve from engine env; local launch does not implicitly inherit harness secrets |
| State isolation | Engine cannot read/write harness dedup/state/scratch; native mapping survives intended engine-home reuse and existing mappings migrate; channels cannot read either private store |
| Preparation: hostile inputs | Seed prior workspace with .gitconfig, .git/config and hooks; credential-bearing preparation and cleanup do not execute any planted command |
| Preparation: handoff | Seed source/destination symlinks and escaping paths; no traversal or credential state reaches target; controlled .ach-state recreation succeeds |
| Preparation: reuse | Fresh private scratch on successive events; existing retained-work behavior and workspace root inode preserved; no handoff while previous execution writes; hydration links resolve across mounts |
| Scripts | webhook-script works with native engine unavailable and mini-harness healthy, respects maxConcurrentScripts and consumes no engine slot; broken engine container correctly makes deployment unhealthy |
| Proxies | Managed calls use proxies regardless of filtering setting; upstream ACH authorization remains enforced |
| Existing Redis source | Admission ACK, duplicate ACK, FULL_QUEUE drop/ACK and exception/pending recovery preserved across HTTP ambiguity |
| Response authentication coverage | Forged FULL_QUEUE, duplicate, rejection or outcome-unavailable never causes source ACK/drop; Redis message stays pending. Authenticated responses retain current source behavior |
| Shared conversation concurrency | Two different lanes selecting the same reused conversation cannot overlap its native turns or maintenance; continuity remains shared, cancellation/deadlines bound waiting, and unrelated conversations can progress |

The engine parity inventory below requires evidence through the shared programmatic API, with terminal attachment tested separately:

| Existing behavior | Required evidence |
|---|---|
| Text and tools | Streaming text and existing OpenCodeToolUpdate lifecycle shape survive translation |
| Usage and diagnostics | Usage plus session_ref / oc_session_id turn statistics remain available where applicable |
| Multi-turn invocation | Main turn, step-budget abort/wrap-up and terminal repair continue the same native session; harness validates terminal contract; deadline never resets between turns |
| Session lifecycle | Discard, compact, token-overflow rotation/forget mapping and warm idle-TTL reuse retain behavior |
| Routing versus conversation | session: none / auto / custom works independently of lane FIFO/pool identity |
| Engines and local use | Pi and OpenCode launch/health, local single-launcher HTTP execution, and each engine's native TUI attachment work; no remote TUI promise |

Tests follow implemented scope. Broker, autoscaling and S3 validation belong to later work.

## 12. Implementation order and review

The following steps deliver **phase 1: the split with functional parity**. The preparation patch has an independent security gate; phase 2 proxy improvements and phase 3 ideas are described in Section 10 and do not block this delivery.

0. Private credential-bearing preparation/cleanup and script-only scratch, with hostile-workspace acceptance evidence. Independent patch for the current release line, before transport work; its ship gate does not claim same-UID isolation.
1. Serializable channel envelopes and bounded ID-keyed completion registry, including resubmission and waiter reattachment. Router untouched.
2. Native integrations behind mini-harness HTTP API: multi-turn/session operations, engine-owned native mapping, bounded streaming, LaunchFailed, cleanup ordering and carried deadline. Local programmatic execution switches to this single path.
3. Channels HTTP adapter with signed requests/acceptance/outcomes; three-container manifest, private state mounts, engine environment rules and local isolated configuration.

Step 0 is independently releasable after its checks. Steps 1–3 ship together as the separation increment; its final deliverable includes physically separate channels. This order is implementation guidance, not authorization to release code.

This revision replaces the Unix transport/lease proposal. AgentTrigger CRDs and Actions-as-OCI deployment are not requirements of this increment; this does not remove deployed resources or claim earlier broader proposals are implemented.

The architecture is closed at this scope. Endpoint paths, signed-message encoding, DTO spelling, finite limit defaults, probe wiring and mount rendering belong to implementation planning and targeted verification. They must satisfy these contracts without adding a broker, heartbeat, ownership lease or speculative storage framework. No further full architecture review is required unless implementation reveals an incompatible constraint.
