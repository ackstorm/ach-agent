# Durable runs — design

Status: draft, 2026-10-31. Depends on `2026-10-31-storage-server-design.md`: the save at
termination must fit in the sandbox's grace period, which the storage server makes possible.

## 1. Goal

A run interrupted by a planned termination of its sandbox (node drain, Karpenter
consolidation, eviction, rollout) continues after the restart instead of failing. The agent
gets a turn saying the system restarted and picks up its work from the saved session.

Out of scope: crashes, OOM kills and node failures. They end the run as today (no save, no
resume). Control pod restarts already survive: the sandbox keeps running and the new control
pod adopts it.

## 2. Decisions

| Decision | Choice |
|---|---|
| When state is saved | At termination (SIGTERM), not continuously during the turn. |
| What is saved | The workspace and every session with a run in the unit, through the normal snapshot path. |
| Which runs resume | Only runs whose termination save committed (`ok`). A failed or late save is `ko`: the run fails as today. |
| How the run continues | Same invocation id, same deadline, a new user turn: "System restarted, continue your work." plus any interrupted tool calls. |
| Interrupted tool calls | Reported to the agent. A tool whose MCP annotations say `readOnlyHint: true` or `idempotentHint: true` is listed as safe to retry; any other is listed as "outcome unknown, verify before repeating". |
| Resume limit | `maxResumes` per run, default 2. |
| Unconfirmed retire | Never fails a new message for that workspace (section 7). |

## 3. Termination flow

1. The sandbox pod gets SIGTERM. The execution unit's handler (in `execution/boot.py`,
   before uvicorn's own shutdown) calls the control pod:
   `POST /unit/v1/terminating` on the facade listener, authenticated with the unit's facade
   token, carrying its instance id.
2. The control pod, for each run active in that unit:
   1. Marks the run `suspending` so admission sends nothing more to the unit.
   2. Aborts the engine turn (opencode abort). The session keeps every message and tool
      result recorded before the abort.
   3. Asks the MCP facade for the run's calls still in flight; the facade records each call's
      start, end and the tool's annotations per invocation.
3. Saves the sessions, then the workspace, through `_persist_close`'s existing path.
4. Writes a resume record per run next to the session state:
   `{invocationId, outcome: ok|ko, interruptedCalls: [{tool, args digest, safeToRetry}],
   attempt, deadlineAt}` and sets the run state to `suspended` (`ok`) or `failed`
   (`ko`, error "interrupted, state not saved").
5. Answers the unit's call; the unit exits. If the grace period runs out first, the save is
   incomplete and every run of the unit is `ko`.

The operator sees the unit gone and, because the workspace is still desired `running` while
it holds a `suspended` run, creates a new unit.

## 4. Resume flow

1. When the workspace is active again, the control pod finds its `suspended` runs.
2. Per run, oldest first: if `attempt >= maxResumes` or the deadline has passed, the run fails
   ("resume limit reached" or "deadline expired"). Otherwise it restores the session and
   starts a turn with the same invocation id:

   ```
   System restarted, continue your work.
   Interrupted tool calls:
   - <tool> (<args digest>): safe to retry
   - <tool> (<args digest>): outcome unknown, verify before repeating
   ```

3. The run goes back to `running`; its channel sees nothing but a longer run. A2A tasks stay
   `working`; webhook channels deliver their reply when the run completes.

## 5. Configuration

`workspace.durable`: `enabled` (default `true` once released), `maxResumes` (default 2),
`stopTimeoutSeconds` (default 10, the abort budget inside the grace period). The sandbox
grace period must cover `stopTimeoutSeconds` plus the save; the operator sets it from the
AgentProfile if OpenShell exposes it (section 8).

## 6. Observability

- `ach_agent_runs_suspended_total{outcome="ok|ko"}` and
  `ach_agent_runs_resumed_total{outcome="completed|failed|limit|deadline"}`.
- Anomaly kinds `run_suspend_failed` and `run_resume_failed`.
- Log lines naming the invocation, the outcome and the interrupted calls.

## 7. Unconfirmed retire never fails a message

Today, when the operator confirms a retire later than `workspace.shutdownTimeoutSeconds`,
`WorkspaceManager` stores `WorkspaceRetireTimeout` in `_errors[key]` and `ensure` re-raises
it (`harness/workspaces.py:381-383`): a new message for that workspace fails at once, although
the commit to storage finished before the retire started.

- `ensure` treats `WorkspaceRetireTimeout` as "previous unit still going away": it waits for
  the late confirmation (the existing slot waiters), bounded by the message's deadline, then
  starts a new unit. It never surfaces the error to the caller. The slot keeps counting
  against `maxConcurrentSessions` while the old unit may still run.
- The anomaly `workspace_retire_timeout` stays, as a signal that termination is slower than
  the configured timeout.
- e2e: `shutdownTimeoutSeconds` in `test/e2e/fixtures/profile.yaml` rises to 60 s so kind's
  slower pod termination stops raising the anomaly on every close.

Durable runs need this: a resumed workspace often receives work while its previous unit is
still terminating.

## 8. Open points to verify in the plan

- **Signal delivery**: OpenShell sandbox pods must deliver SIGTERM to the execution process
  and give it a usable grace period. Check what OpenShell sets and whether it can be
  configured; if not, the save budget is whatever it gives, and the design records it.
- **Engine abort**: opencode's abort must leave the session consistent and resumable with a
  new prompt on the same native session.
- **Unit reachable during the save**: the control pod pulls archives from the unit, so the
  unit's HTTP server must keep serving until step 5 answers.

## 9. Testing

- e2e (local stack): SIGTERM to a unit while the model request is held → run `suspended`
  (`ok`), new unit, resume turn delivered, run `completed`, one business delivery.
- e2e: the same with storage unavailable → `ko`, run `failed` with the interrupted error.
- e2e: a tool call in flight at SIGTERM → listed in the resume turn with the right
  `safeToRetry`.
- e2e: `maxResumes` reached → run fails with "resume limit reached".
- unit: `ensure` waits for a late retire and then starts a unit (section 7).
- kind: drain the sandbox's node during a run; the run completes.

## 10. Rollout

ach-agent 0.4.0, after the storage server is live. `workspace.durable.enabled` defaults to
`true`; an Agent can turn it off.
