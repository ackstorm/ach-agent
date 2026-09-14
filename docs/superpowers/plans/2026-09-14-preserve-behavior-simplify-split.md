# Preserve Behavior and Simplify the Split — Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development task-by-task. The user selected GPT-5.6-Luna implementation and root review; do not ask again and do not dispatch reviewer agents. This document authorizes no publishing or external deployment. Checkboxes track execution, not work already completed.

**Execution status (2026-09-14):** Tasks 1–5 completed on `feat/phase1-split`.
Root reviewed Luna's changes and verified the unchanged full gate on `4869d98`:
1176 passed, 3 skipped; 18 conformance tests passed. Both real native TUIs,
persistent Pi/OpenCode Compose, ephemeral startup/model execution and H process
restart were exercised. See [validation evidence](../../reports/unix-split-validation.md)
and [baseline behavior matrix](../../reports/unix-split-behavior-matrix.md).
Implementation uses `ExecutionService.configure` for the planned lazy service
initialization; no extra factory abstraction was needed. No image was published
and no external operator or cluster was changed.

**Goal:** Preserve the original agent's behavior while simplifying the split already built, retaining useful execution, session and result handling code.

**Architecture:** Three ordinary containers in one pod. Channels submits through channel.sock to the existing RAM queues/consumers. H executes the original hooks on the existing shared session workspace and sends selected launch configuration/environment to E through agent.sock; E owns native adapters and process lifecycle.

**Tech Stack:** Existing Python, asyncio, Pydantic, httpx, FastAPI/Uvicorn and Docker. No additional broker or dependency.

**Spec:** [Approved target, updated 2026-09-14](../specs/2026-09-13-unix-split-simplification.md). The [README diagrams](../../../README.md#how-it-works) explain the target lifecycle. This plan supersedes the 2026-09-13 transport-only plan.

## Global constraints and references

- **Behavioral reference:** original v0.16.1, `462912f`. Do not use our intermediate split decisions as proof of original behavior.
- **Implementation starting point:** existing `feat/phase1-split` worktree; latest runtime snapshot `929397a`, later `8f06991` / `d855fa5` are documentation. Inspect status before starting and preserve concurrent user changes.
- This is a scoped refactor, not a reset to the old tree. No whole-file `git restore` from the baseline and no unrelated fixes or feature removals.
- Keep logical workspace/home/state paths, existing data, session reuse/import, prompt/terminal behavior, original channel responses, limits, source Redis ACK policy, native TUI and script-only behavior.
- Preserve the exact existing `workspace_dir(work_dir, session_key)` mapping. Workspace-per-session is not new functionality.
- H alone receives full config. C keeps source credentials. Managed ACH/model/MCP credentials stay in H. Explicit eligible engine env values are deliberately sent to E; do not transfer the whole H environment.
- Every shell command starts with `rtk`. Python, tests and schema checks run in Docker via `scripts/dev.sh`; no host Python/pip.
- One active replica, one pod, three containers. No new public schema or CR fields in this simplification; do not remove existing fields as incidental cleanup.
- No new internal Redis, KEDA, S3, proxy masking, egress enforcement, lease, heartbeat, generic transport framework or remote TUI.
- Luna writes production/tests. Root reviews each task. Sequential production writers; commits are review checkpoints, not separate releases.

## What to keep and what to replace

| Keep | Replace / remove only after equivalent path works |
| --- | --- |
| Router, lane queues/consumers, admission bounds | No new queue implementation |
| Serializable events, result IDs, completion registry | Internal HMAC and its key distribution |
| Execution API, native adapters, streaming, cancel and stop confirmation | TCP control listeners with Unix sockets |
| Allowlisted public configuration and native file writers | Public bootstrap files with socket delivery |
| Native sessions, store/import, warm TTL, terminal repair, source behavior | Assumption that E already has forwarded env values |
| Original H-side hooks, environment and workspace semantics | E-side hooks and mandatory scratch/bundle clone path introduced by split |
| Useful process supervision and IPC correlation | Only hook-transfer fields/routes made unnecessary by H ownership |
| Existing tests of observable behavior | Assertions that pin a superseded implementation mechanism |

Do not delete a module solely because its name says `private`, `bootstrap` or
`workspace`. Audit its callers, preserve required behavior, and remove only unused
machinery. In particular, stop notifications/acknowledgements can still be necessary
for H cleanup after E's warm idle expiry; they are not Git/bundle functionality.

## Task 1: Establish original-behavior acceptance before refactoring

**Files:** Create `tests/compat/test_original_split_behavior.py` and `docs/reports/unix-split-behavior-matrix.md`. Read original and current `boot/prepare.py`, `boot/engine_runner.py`, `boot/secrets.py`, `engine/base/pool.py`, `engine/lifecycle.py`, `engine/pi/config.py`, `channels/queue.py`, `router/router.py`. Existing tests in `tests/test_prepare.py`, `tests/router`, `tests/channels`, `tests/engine` supply fixtures and expected behavior.

**Consumes:** `462912f` behavior and current runtime. **Produces:** executable characterization tests and a matrix assigning each observed difference to a later task. No production changes.

- [x] Read the original functions with `rtk proxy git show 462912f:src/ach_agent/boot/prepare.py` and equivalent commands for the files above. Record source function names beside expectations. Do not infer behavior from the latest SPEC alone.
- [x] Capture these original cases: repeated event on same key preserves populated checkout; different keys retain their original distinct paths; prepare precedes acquire; cleanup follows stop and is deferred during warm reuse; prepare/cleanup cwd and HOME; selected env and event variables; prepare failure, timeout and best-effort cleanup; script-only payload/temp-directory lifecycle; session modes, prompt/repair and source ACK behavior.
- [x] Add a direct shared-workspace hook characterization in the new test module. Reuse `MessageEvent` and `PrepareBlock`; the sentinel is synthetic:

```python
@pytest.mark.asyncio
async def test_credentialed_prepare_keeps_original_workspace(tmp_path, monkeypatch):
    ws = workspace_dir(str(tmp_path), 'repo:42')
    ws.mkdir()
    (ws / 'retained').write_text('original checkout')
    monkeypatch.setenv('TEST_FORGE_TOKEN', 'synthetic')
    block = PrepareBlock.model_validate({
        'script': 'test "$PWD" = "$ACH_WORKSPACE"; '
                  'test "$HOME" = "$ACH_WORKSPACE"; '
                  'test "$TOKEN" = synthetic; test -f retained; '
                  'printf ok >> runs',
        'secretEnv': {'TOKEN': {'env': 'TEST_FORGE_TOKEN'}},
    })
    event = MessageEvent(idempotency_key='e1', session_key='repo:42',
                         channel_name='review')
    await run_prepare(block, event, ws)
    await run_prepare(block, event, ws)
    assert (ws / 'retained').read_text() == 'original checkout'
    assert (ws / 'runs').read_text() == 'okok'
```

- [x] Run characterization against a task-owned baseline checkout and the current branch, using each checkout's Docker tooling. Validate tests on the original first, then record expected current failures caused by private-clone semantics. Keep expected-failure notes in the matrix, not permanent xfails that would hide a regression.
- [x] Add concrete ordering records using existing fake drivers/hook fixtures: `prepare → acquire → turn`; warm completion does not call cleanup; expiry/close records `stop → cleanup`; prepare failure records zero native turns. Include two lanes resolving to the same conversation as an observation of baseline behavior, not permission to invent a new sharing policy.
- [x] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/compat/test_original_split_behavior.py tests/test_prepare.py tests/router -q`. Record baseline versus current outcomes. Commit `test: pin original behavior for split simplification`. Root verifies expectations before production work.

## Task 2: Forward explicit environment values without operator duplication

**Files:** `boot/roles.py`, `execution/wire.py`, `execution/service.py`, `engine/base/driver.py`, `engine/lifecycle.py`, `engine/pi/config.py`, `engine/mcp_passthrough.py`, `tests/test_task8a_roles.py`, `tests/engine/test_sanitized_env.py`, `tests/execution/test_wire.py` (production paths under `src/ach_agent`).

**Consumes:** existing public projection, original forwarding/sanitization rules, Task1 matrix. **Produces:** `PublicEngineConfig.engine_env: dict[str, str]` (JSON `engineEnv`) and matching `EngineConfig.engine_env`. `engine.forwardEnv` remains the unchanged public name selector.

- [x] Replace the names-only test with a red test where values exist exclusively in H; E has no DEBUG/custom-token env. Reuse `_cfg` in `tests/test_task8a_roles.py`:

```python
def test_projection_carries_only_selected_values(monkeypatch):
    monkeypatch.setenv('DEBUG', '1')
    monkeypatch.setenv('CUSTOM_TOOL_TOKEN', 'synthetic-custom')
    monkeypatch.setenv('ACH_TOKEN', 'synthetic-managed')
    cfg = _cfg(engine={'forwardEnv': ['DEBUG', 'CUSTOM_TOOL_TOKEN']})
    _, public = build_role_configs(cfg)
    assert public['engineEnv'] == {
        'DEBUG': '1', 'CUSTOM_TOOL_TOKEN': 'synthetic-custom',
    }
    assert 'synthetic-managed' not in json.dumps(public)
```

- [x] Run the wire/role/native-env tests and confirm the failure is the names-only behavior. Resolve selected names using existing sanitization; do not replace it with a new blanket policy. Keep baseline handling of missing env and passthrough MCP references. H builds `{name: os.environ[name] for name in eligible_names if name in os.environ}` after applying the existing selector.
- [x] Add the explicit map with `repr=False`; validate names/values for subprocess environment compatibility without echoing values in errors. Stop emitting redundant `engineEnvNames` in the unpublished wire format once its callers are migrated. Retain internal name helpers only where still consumed; do not remove public `forwardEnv`.
- [x] Apply the received map in native env builders at the same precedence as original forwarding; keep launcher-owned PATH/config/trace/proxy settings authoritative as before. Do not call `os.environ.update` in E. Translate passthrough MCPs with the existing `to_engine_entry(spec, env=selected_mapping)` argument; no extra credential broker.
- [x] Prove native Pi/OpenCode children see H-selected DEBUG/custom token when E's ambient values are absent or different. Prove managed credentials are absent, the config response/logs omit values, and separate invocations do not mutate global E env. Keep existing warm-reuse semantics; do not add live environment rotation.
- [x] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/test_task8a_roles.py tests/engine/test_sanitized_env.py tests/execution/test_wire.py tests/engine/pi -q`. Commit `refactor: deliver selected engine environment values`; root checks baseline parity and secret boundaries.

## Task 3: Restore original H-side hooks on the existing shared workspace

**Files:** `boot/prepare.py`, `boot/engine_runner.py`, `boot/private_prepare.py`, `engine/workspace.py`, `execution/service.py`, `execution/wire.py`, `boot/execution_client.py`, `boot/paths.py`; tests in `tests/compat`, `tests/test_prepare.py`, `tests/test_private_prepare.py`, `tests/execution/test_workspace_hooks.py`, `tests/boot/test_engine_runner_http.py`.

**Consumes:** current reservation/stop event/cleanup-ack protocol and Task1 hook expectations. **Produces:** unchanged public `run_prepare(cfg, event, workspace)` / `run_cleanup(cfg, event, workspace)` signatures, always executed in H. E receives workspace identity/lifecycle requests, not scripts, hook env or channel payload.

- [x] Add red integration tests covering prepare and cleanup with and without `secretEnv`, executed only in H; E sees prepared files through the shared volume. Assert current path formula, retained checkout content, prepare cwd/workspace HOME, cleanup parent cwd/workspace HOME, original errors/timeouts and script-only temp workspace.
- [x] Remove the added private-clone branches from `run_prepare` / `run_cleanup`, preserving the original `_execute_hook` path, selected environment, static script-on-stdin behavior, `_event_value` checks, bounded output and redaction. Do not change shell flags, default timeouts or policy based on whether credentials exist.
- [x] In H's runner, reserve the existing lane/workspace lifecycle before running prepare, so an old idle timer cannot clean it concurrently. Keep the existing deterministic workspace mapping. Execute `await run_prepare(prepare_cfg, event, workspace)` before acquisition; store the configured cleanup/event context on H for its original teardown point.
- [x] Remove `_public_hook` transfer and E-side script execution. Reduce the existing workspace reservation request to identity/path/deadline and cleanup-coordination fields; keep existing endpoint/client names initially to avoid a parallel API. Drop hook text, hook env and delivery payload fields once callers/tests move.
- [x] Keep E's native pool stop/expiry notifications and H acknowledgement where necessary for the original `stop → cleanup → reuse` ordering. Generalize the existing cleanup registry from credentialed-only to all configured cleanup hooks; do not add a second registry or scheduler. New same-key work waits for any previous teardown; ordinary warm reuse cancels idle expiry before prepare. E retains no script config.
- [x] Remove bundle export/import routes and automatic scratch/reset logic made unused by that path. Audit each `private_prepare.py` consumer before deletion; restore original `webhook-script` workDir/temp-directory behavior separately from engine acquisition. Preserve any unrelated reusable utility until its callers have moved.
- [x] Keep `.ach-state` usable from H and E using the shared public-context path. Do not mount H private state into E or E private home into H. No workspace rename, inode replacement, fresh clone mandate or Git-config scanner. Record original trust model in docs, without describing it as an isolation guarantee.
- [x] Rerun Task1 characterization and `rtk proxy ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/execution/test_workspace_hooks.py tests/boot/test_engine_runner_http.py -q`. Cases that failed because of our private-clone path must now pass. Keep cleanup failure/cancellation races tested. Commit `refactor: restore original harness workspace hooks`; root checks retained behavior before transport changes.

## Task 4: Replace TCP/bootstrap with Unix sockets, retaining the API

**Files:** new `boot/ipc.py`, `tests/boot/test_ipc.py`, `tests/execution/test_uds.py`; modify `channels/client.py`, `channels/envelopes.py`, `boot/channels_api.py`, `execution/app.py`, `execution/wire.py`, `boot/execution_client.py`, `boot/roles.py`, `boot/local.py`, `main.py`; adapt `tests/channels/test_internal_http.py`, `tests/execution/test_http.py`, `tests/execution/test_client.py`, `tests/test_split_roles.py`, `tests/test_task8a_roles.py`.

**Consumes:** existing event/result API, Task2 public config, Task3 workspace lifecycle. **Produces:** two concrete Unix endpoints; no new queue/protocol hierarchy.

```text
channel_socket_path(root: Path = Path('/run/ach-agent')) -> Path
    root / 'channels' / 'channel.sock'       # H owns, C connects
engine_socket_path(root: Path = Path('/run/ach-agent')) -> Path
    root / 'engine' / 'agent.sock'           # E owns, H connects
bind_listener(path: Path) -> socket.socket
ChannelInputs: agent_name: str (alias agentName), channels: list[ChannelSourceConfig]
ChannelsClient.fetch_config() -> ChannelInputs [async]
ExecutionClient.connect(config: PublicEngineConfig) -> ControllerHello [async]
create_engine_service(public: PublicEngineConfig) -> ExecutionService
```

- [x] Add red tests for real Unix connections: source config fetch, normal submit/duplicate/FULL_QUEUE/result lookup, E health before configuration, public-only initialization, two active streams with cancellation, and stop-EOF behavior. Preserve current API payloads except the explicitly changed initialization/env fields.
- [x] Implement the listener helper: bounded live-socket probe, refuse live listener/non-socket/symlink/wrong-owner entries; remove only a stale owned socket. Bind AF_UNIX with mode0600, listen, nonblocking. Create absent local private parents0700; do not chmod/chown root-owned Kubernetes volume roots with valid fsGroup write access. No recursive mount cleanup.
- [x] Use existing HTTP libraries, not custom framing:

```python
transport = httpx.AsyncHTTPTransport(uds=str(socket_path), limits=pool_limits)
client = httpx.AsyncClient(base_url='http://ach-internal', transport=transport)
listener = bind_listener(socket_path)
await uvicorn.Server(uvicorn.Config(app=app)).serve(sockets=[listener])
```

- [x] Each execution-client connection pool gets its own UDS transport with its current limits. Retain independent controller, stream, acquisition and priority cleanup capacity. Do not merge transports merely because the pathname is shared. Preserve the explicit stopped response/EOF regression from `beac73f`.
- [x] C fetches typed source-only `ChannelInputs` from `GET /internal/v1/config` on channel.sock before creating source adapters. Reuse `_source_projection`; source secrets remain C env references. Remove internal request/response signing and nonce/key distribution while keeping external webhook/A2A authentication, bounds, correlation and result semantics. Router/Lane remain unchanged.
- [x] E opens agent.sock with no config/native process. Extend the existing controller-open request with `config: PublicEngineConfig`; keep the response `ControllerHello` without config. Extract current driver/path/store setup into `create_engine_service`, invoked once at initialization, without native launch. App health/instance identity exists before configuration; uninitialized execution operations return503. Existing `AcquireRequest.config` configures acquisition; `TurnRequest` still carries only prompt/IDs/budget.
- [x] Preserve single-controller ownership and existing cleanup-before-reclaim. Initialize the session store before current legacy import. Reject incompatible engine/layout changes requiring process replacement, but allow fresh per-execution proxy URLs after H restart. No new configure endpoint, lease, generations or reload mechanism.
- [x] Adapt the local parent/child launcher to a short private socket directory; one parent-owned `ACH_RUNTIME_DIR` override is sufficient, not deployment boilerplate. Remove role JSON artifact creation and config-path injection. Local RPC uses the same UDS client; preserve supervisor, process groups and teardown.
- [x] Preserve native TUI using the same public controller request on agent.sock: child `--role engine --tui` inherits the terminal, accepts configuration, then calls the extracted existing native-terminal routine as an owned task. No config bytes on stdin, remote terminal API or PTY tunnel. Hold controller ownership until normal terminal exit; controller loss cancels/joins that task. Preserve Pi/OpenCode trace adoption and Ctrl-C/Ctrl-D behavior.
- [x] Delete obsolete bootstrap file/key helpers and internal signing code only after all callers move; migrate their valid size/secret/lifecycle tests. Do not retain the unpublished TCP path as a second production backend. C waits boundedly for H; E is healthy before H connects; H hydrates/configures E before readiness. Native launch failure must not disable healthy script-only execution.
- [x] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/boot/test_ipc.py tests/channels tests/execution tests/test_task8a_roles.py tests/test_split_roles.py tests/boot/test_engine_runner_http.py -q`. Commit `refactor: use unix sockets for existing split interfaces`; root reviews data/stream/lifecycle parity and local subprocess coverage.

## Task 5: Deployment examples and final behavior evidence

**Files:** Dockerfile; `docker/split/compose.yaml`, `compose-ephemeral.yaml`, `compose-acceptance.yaml`, `pod.yaml`, README/config fixtures; `scripts/test-split.sh`, `tests/test_split_manifest.py`; README, SPEC, `docs/schemas/operator-contract.md`, `/tmp/to-ach.md`, new `docs/reports/unix-split-validation.md`.

**Consumes:** Tasks1–4, existing images/native tests. **Produces:** tested minimal deployment contract and completed original-behavior matrix; no external rollout.

- [x] Add failing manifest tests, then replace bootstrap mounts with two distinct IPC directory volumes at the same parent paths. H writes channels IPC and mounts engine IPC read-only; C mounts channels IPC read-only and cannot see engine IPC; E writes engine IPC and cannot see channels IPC. Workspace remains H/E read-write; native home E-only; public context H-write/E-read; full config H-only. Keep UID/GID/fsGroup10001 and tini entrypoint/role args, without init containers.
- [x] Remove mandatory internal HMAC/config-file/host/port/URL settings and E env duplication for `forwardEnv`. Keep actual source credentials, H credentials, required public metadata and original config. Public ingress remains C8080; model/MCP/native HTTP remains where existing clients need it. No control TCP8090/8081.
- [x] H/E probes execute HTTP over their socket; C probes remain public HTTP. Keep bounded startup allowance. Example engine liveness:

```python
import httpx
with httpx.Client(transport=httpx.HTTPTransport(
    uds='/run/ach-agent/engine/agent.sock'), base_url='http://ach-internal',
    timeout=2) as client:
    client.get('/healthz').raise_for_status()
```

- [x] Adapt synthetic Compose acceptance to both real Pi/OpenCode: shared checkout survives two same-key events; original hooks run in H with correct cwd/HOME/env; native child receives selected H values with no E duplication; same native conversation continues; model/MCP configuration is generated by existing adapters; results and streaming match baseline. Include distinct keys and configured custom conversation reuse without changing the rules characterized in Task1.
- [x] Verify cancellation, prepare/launch failure, warm TTL cleanup and same-key expiry/prepare race. Force held model output before killing E, observe failed work and readiness. H process restart with C/E alive must preserve native mappings and reconnect via sockets. Do not treat Docker network-owner replacement as a Kubernetes process restart.
- [x] Verify real filesystem mounts: C connects through read-only IPC but cannot unlink H socket; E cannot read private config/H state/channel socket; E can read files H prepared at the original workspace path. Check ephemeral and persistent startup. Preserve original logical home/workDir/state data; do not add storage migrations to solve path naming.
- [x] Build combined image and run real `docker -it --tui` with Pi/OpenCode: two typed turns, resize32x100, native-session continuity, clean Ctrl-D/Ctrl-C and tini PID1. No piped smoke test substituted for terminal behavior. Use synthetic upstreams and task-owned resources only.
- [x] Rerun the Task1 matrix; every supported functional case must match original. Document any unavoidable new IPC-failure behavior separately. A mismatch blocks completion; do not silently amend expected behavior to fit the new implementation.
- [x] Run unchanged `scripts/pre-push-check.sh` from a clean local clone on the project filesystem if needed for gitleaks, plus affected acceptance tests. Avoid `/tmp` for large clones because it is a small tmpfs. Record exact source/image digests and commands. No weakening checks, hiding failures or unbounded reruns.
- [x] Finish README state diagrams and SPEC as implemented only after verification. Update `/tmp/to-ach.md` and the tracked operator handoff with exact socket mounts/probes and H-resolved forwarding; retain future two-Deployment direction as deferred. State image publication/operator rendering are separate. Record unexecuted cluster checks honestly.
- [x] Audit deletions by responsibility and callers, not line-count targets. List retained versus removed modules, and preserve useful tests instead of deleting failing assertions wholesale. Remove task-owned containers/networks/volumes; leave unrelated services alone. Commit `package: validate simplified split against original behavior`; root performs final review and records clean status.

## Self-review / execution handoff

| Agreed requirement | Proof / task |
| --- | --- |
| Original behavior is the reference | Task1 baseline characterization; Task5 parity gate |
| Workspace mapping is unchanged | Task1 exact paths, Task3 retained shared checkout, Task5 real mounts |
| All original hooks execute in H | Task3, with original env/cwd/HOME/TTL/error cases |
| Explicit env names and values cross the socket | Task2 child-process and secret-leak tests |
| Keep useful adapters, sessions and IPC behavior | All tasks' narrow scope; Task5 native tests/deletion audit |
| Two sockets, no HMAC/bootstrap files | Task4; actual mounts/probes in Task5 |
| No replacement queue/framework | Router/Lane unchanged; existing source tests remain |
| Local TUI and original channels/results survive | Task4 subprocess coverage, Task5 actual terminals and source parity |

Execute sequentially with Luna implementation and root review. Stop a task for
review if it requires an unapproved functional change; do not reinterpret this
plan as permission to redesign behavior. Do not ask again for execution mode.
The execution status and linked validation report record the completed implementation;
the original task instructions above remain as the review checklist.
