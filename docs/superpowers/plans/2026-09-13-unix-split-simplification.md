# Unix Split Simplification Implementation Plan

**SUPERSEDED — do not execute this version.** The consolidated
[2026-09-14 plan](2026-09-14-preserve-behavior-simplify-split.md) replaces it,
including original-behavior characterization, H-side hooks and forwarded values.
The text below is historical context, not the current execution baseline.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement task-by-task. The user selected GPT-5.6-Luna implementers and root review. Do not ask again which execution mode to use, and do not spawn reviewer agents. Steps use checkboxes for tracking.

**Goal:** Replace internal TCP/HMAC and role-bootstrap files with two Unix endpoints, preserving the existing in-memory queue and engine behavior.

**Architecture:** Channels submits to H's existing bounded queue/consumer through channel.sock. E opens agent.sock without configuration; H sends allowlisted settings through controller initialization and the existing acquisition request. Only H reads the private full config.

**Tech Stack:** Existing Python/asyncio, FastAPI, Uvicorn, httpx, Pydantic, Docker and pytest; no new dependency.

**Spec:** [Approved simplification](../specs/2026-09-13-unix-split-simplification.md).

## Global Constraints

- Baseline `8f06991`, existing isolated `feat/phase1-split` worktree. Inspect status before edits.
- This is a plan, not an implementation or a claim the current images implement it.
- One pod, three ordinary containers, one active execution replica. No CR additions.
- Every shell command starts with `rtk`. Python/test tooling runs through `scripts/dev.sh` in Docker, never host pip/Python.
- Luna writes production/tests; root reviews each task and may write docs/diagnostics. Sequential writers for `main.py` and `boot/roles.py`.
- Full config and H secrets are absent from C/E. Preserve `engine.forwardEnv` and native env sanitization.
- Existing Router/Lane are the queue and consumer; no extra inbox, broker abstraction or scheduler.
- Preserve prepare, workspace, session reuse/import, lane/conversation distinction, script-only concurrency, deadlines and cleanup ordering.
- HTTP over UDS reuses current request/event contracts. No control TCP fallback or internal HMAC; preserve external source authentication.
- Keep native local/TUI, source Redis, public ingress and existing capability proxies. Masking, internal Redis, KEDA, S3, leases and remote TUI are excluded.
- No push, publication, merge, external deployment or edits to the ACH operator repository.

## Current code facts and file ownership

| Existing area | Keep / change |
| --- | --- |
| `router/router.py`, `router/lane.py`, `boot/completions.py` | Existing queue, consumers and result tracking; behavior stays intact |
| `channels/client.py`, `boot/channels_api.py` | Keep event/result contract; use UDS, remove internal signatures, expose source config |
| `channels/signing.py` | Remove when internal callers are gone; do not remove webhook/A2A authentication |
| `execution/wire.py` | Reuse `PublicEngineConfig` and `AcquireRequest.config`; add typed controller request containing public config |
| `execution/app.py`, `boot/execution_client.py` | Configure service through controller open; retain streaming/cancel/lifecycle behavior over UDS |
| `execution/service.py`, `execution/state.py` | Keep native behavior and store; construction moves behind public initialization |
| `boot/roles.py`, `boot/local.py`, `main.py` | Remove bootstrap-file dependencies; wire role startup and native TUI |
| `boot/bootstrap.py` | Delete file/key machinery after callers move; retain needed projection types elsewhere |
| New `boot/ipc.py` | Small concrete path/listener/client helpers; no backend/plugin hierarchy |
| Dockerfile, `docker/split/*`, `scripts/test-split.sh` | Socket mounts, probes, one H config, no manual internal settings |

## Task 1: Channels submits to the existing RAM queue over channel.sock

**Files:** Create `src/ach_agent/boot/ipc.py`, `tests/boot/test_ipc.py`; modify `channels/client.py`, `channels/envelopes.py`, `boot/channels_api.py`, `boot/roles.py`, `main.py`, `tests/channels/test_internal_http.py`, `tests/channels/test_completion_wiring.py`; remove `channels/signing.py` and `tests/channels/test_signing.py` after auditing callers. Paths under `src/ach_agent` unless specified.

**Interfaces produced:**

```python
# boot/ipc.py: fixed deployment defaults; local parent may pass a short root.
def channel_socket_path(root: Path = Path('/run/ach-agent')) -> Path:
    return root / 'channels' / 'channel.sock'

def engine_socket_path(root: Path = Path('/run/ach-agent')) -> Path:
    return root / 'engine' / 'agent.sock'

```

Remaining signatures (implementation behavior is specified in the steps below):

```text
bind_listener(path: Path) -> socket.socket
ChannelInputs: agent_name: str (JSON agentName), channels: list[ChannelSourceConfig]
create_channels_app(registry: CompletionRegistry, *, inputs: ChannelInputs,
                    max_body_bytes: int = MAX_CHANNEL_BODY_BYTES) -> FastAPI
ChannelsClient(socket_path: Path, *, agent: str = 'default',
               channel_name: str | None = None, timeout: float = 30.0,
               http_client: httpx.AsyncClient | None = None,
               clock: Callable[[], float] = time.time,
               poll_interval: float = 0.25, wait_timeout: float | None = None)
```

The `http_client` and result-deadline `clock` injections remain for tests;
nonce/signature injection goes away. `submit`, `handle`, `wait`,
scope/status/body-size validation and close semantics remain unchanged.

- [ ] Add failing real-UDS tests: a signed key is unnecessary; submit/retry returns the same invocation; queue saturation returns existing FULL_QUEUE; source result waiting works. Adapt existing completion fixtures instead of introducing new queue fixtures. Add socket-owner tests:

```python
def test_live_listener_is_not_replaced(tmp_path):
    path = channel_socket_path(tmp_path)
    listener = bind_listener(path)
    try:
        with pytest.raises(RuntimeError, match='in use'):
            bind_listener(path)
        assert stat.S_ISSOCK(path.stat().st_mode)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        listener.close()
```

- [ ] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/boot/test_ipc.py tests/channels/test_internal_http.py -q`; verify failures are the missing UDS behavior.
- [ ] Implement `bind_listener`: create private parents mode0700 if absent, but do not chmod an existing Kubernetes mount root (root ownership with fsGroup write access is valid); `lstat` existing entry; reject symlink/regular file/wrong owner. Probe an existing socket with a bounded connection: successful connection means in-use; ECONNREFUSED permits stale unlink; other errors fail. Bind using restrictive umask, chmod0600, listen and set nonblocking; close on failure. Never recursively remove the mount.
- [ ] Use the socket with existing libraries:

```python
transport = httpx.AsyncHTTPTransport(uds=str(socket_path))
http = httpx.AsyncClient(base_url='http://ach-internal', transport=transport,
                         timeout=timeout)
listener = bind_listener(channel_socket_path())
await uvicorn.Server(uvicorn.Config(app=app)).serve(sockets=[listener])
```

- [ ] Remove key arguments, signature headers/checks, nonce cache and signed response wrapper from the internal C/H client/app. Keep bounded body decoding, event/channel scope, completion correlation, rejection status mapping and draining behavior. Do not change Router/Lane or existing source Redis ACK decisions.
- [ ] Add `GET /internal/v1/config` returning a typed `ChannelInputs(agent_name: str, channels: list[ChannelSourceConfig])`, with JSON alias `agentName`. Build from the current `_source_projection`; exclude scripts/private values. `ChannelsClient.fetch_config() -> ChannelInputs` reads it with the existing finite response bound before source adapters start. No HMAC, key or harness URL in that payload. H mounts its private config only.
- [ ] Change C startup to wait for this endpoint using the existing 300-second startup allowance, then construct source adapters/client identity from the response. Keep source credentials read from C env. H exposes the app once router/config are ready. Delete the C bootstrap-file wait; preserve ordinary H behavior until Task 2 removes E bootstrap publication.
- [ ] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/boot/test_ipc.py tests/channels tests/router -q`. Keep tests for external webhook authentication; delete only internal-signature tests.
- [ ] Commit as `refactor: use unix queue ingress without internal signing`; root reviews event parity and socket ownership before Task 2.

## Task 2: Unconfigured mini-harness receives public settings over agent.sock

**Files:** Modify `execution/wire.py`, `execution/app.py`, `boot/execution_client.py`, `boot/roles.py`, `main.py`, `tests/execution/test_wire.py`, `tests/execution/test_http.py`, `tests/execution/test_client.py`, `tests/test_split_roles.py`; add real socket coverage in `tests/execution/test_uds.py`. Keep `ExecutionService` behavior and existing native adapters.

**Interfaces produced:**

```python
class ControllerRequest(ControllerHello):
    config: PublicEngineConfig
```

Signatures for existing implementation units:

```text
create_execution_app(
    service: ExecutionService | None = None, *,
    service_factory: Callable[[PublicEngineConfig], ExecutionService] | None = None,
) -> FastAPI

# Existing test/service injection remains; production passes the factory.
create_engine_service(public: PublicEngineConfig) -> ExecutionService

# ExecutionClient: socket_path replaces deployment base_url.
ExecutionClient.connect(config: PublicEngineConfig) -> ControllerHello [async]
```

- [ ] Add a failing endpoint test: health supplies an instance ID before config; controller request with private `AgentConfig` fields returns422; valid public config creates one service/store but launches zero native processes; first acquire uses existing `AcquireRequest.config`. The response remains `ControllerHello`, without `config`.

```python
def test_controller_config_rejects_private_fields():
    public = PublicEngineConfig().model_dump(mode='json')
    public['channels'] = [{'prepare': {'script': 'PRIVATE_SENTINEL'}}]
    with pytest.raises(ValidationError):
        ControllerRequest.model_validate({
            'version': 1, 'instance_id': 'engine-1', 'controller_id': 'h-1',
            'config': public,
        })
```

- [ ] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/execution/test_wire.py tests/execution/test_uds.py -q`; verify the missing initialization/UDS behavior fails.
- [ ] Extract existing driver selection, E-owned path setup and store opening from `run_engine` into `create_engine_service(public)`. Do not open the native engine in this factory. Leave session map storage/import and pool behavior intact.
- [ ] Let `create_execution_app` own an initially absent service and stable instance ID. Health is available before config; operations requiring a controller return503 while unconfigured. In controller-open, validate version/instance/config, initialize once through the factory, then use existing claim/held-stream logic. No new configure endpoint or handshake stages. Existing app injection tests may supply a prebuilt service.
- [ ] Store only immutable initialization identity for reconnect comparison: agent name, engine type/binary, home/workDir/publicContext, persistence mode, codemem path/project. Reject changes with409 requiring process replacement. Do not compare invocation model/proxy URLs; H restart can supply fresh URLs in subsequent `AcquireRequest.config`. Concurrent controller opens cannot construct two stores; preserve existing single-controller rejection and cleanup-before-reclaim.
- [ ] Replace E TCP bind with `bind_listener(engine_socket_path())`. Construct a **separate** `httpx.AsyncHTTPTransport(uds=str(socket_path), limits=pool_limits)` for each existing execution-client pool (controller, ordinary, acquire, stream, priority, cleanup), where `pool_limits` is that client's current `httpx.Limits` value. Sharing one transport would share its connection pool and defeat cancellation capacity. Preserve stop-EOF protection and the exact stopped acknowledgement regression from `beac73f`.
- [ ] H sends the allowlisted public config on `connect(public_cfg)` before current session import and workspace operations. Delete ordinary E bootstrap-file reads/publication. Do not serialize H env or full AgentConfig; add a wire-capture test with private prepare/script/token sentinels and E-side DEBUG/custom-token forwarding.
- [ ] Adapt existing tests for two active streams, cancel while output is blocked, launch failure, cleanup deadline, warm reuse, same-conversation exclusion and obsolete-controller rejection to the real UDS server. Keep ASGI unit tests too; ASGI-only does not prove socket/transport behavior.
- [ ] Run `rtk proxy ./scripts/dev.sh uv run pytest tests/execution tests/boot/test_engine_runner_http.py tests/test_split_roles.py -q`, then commit `refactor: initialize engine through unix controller connection`. Root reviews lifecycle and session continuity before Task 3.

## Task 3: One startup path without public bootstrap files, including local TUI

**Files:** Modify `main.py`, `boot/local.py`, `boot/roles.py`, `execution/app.py`, `tests/test_task8a_roles.py`, `tests/test_split_roles.py`; remove `boot/bootstrap.py` and `tests/boot/test_bootstrap.py` after moving still-needed constants and coverage. Do not modify native drivers to change their protocols.

**Interfaces produced:**

```text
LocalEngineProcess.start(socket_path: Path, *, env: dict[str, str] | None = None,
                         terminal_mode: bool = False) -> LocalEngineProcess [async]
run_native_terminal(public: PublicEngineConfig) -> None [async]
```

`run_native_terminal` is the current terminal-mode branch of `run_engine`
extracted, not a new terminal protocol. The local parent owns a short temporary
runtime directory and passes its path through one launcher-owned internal
`ACH_RUNTIME_DIR` override; deployment defaults require no such env variable.

- [ ] Add failing subprocess tests: E starts with no config path/value and answers socket health; ordinary local execution sends controller config and needs no generated JSON; two local parents get distinct short socket directories; managed H env never reaches child. Verify stale socket restart and bounded missing-peer startup.
- [ ] Refactor `LocalEngineProcess` to own its socket directory instead of `RoleArtifactPaths`. Remove `RoleArtifacts`, `load_artifact`, role JSON creation, and old host/port/config-path injection. Retain process-supervisor, isolated process group and bounded teardown. Only the local parent deletes its task-owned runtime directory after child termination.
- [ ] Preserve the real native-TUI branch: child `--role engine --tui` opens agent.sock, receives the same `ControllerRequest.config`, then runs `run_native_terminal` as an owned task. CLI terminal mode requires inherited TTY; it is not selectable through an ordinary remote request. H holds the controller connection and awaits child exit. Native stdin/stdout remain terminal-only; config/protocol bytes never use them.
- [ ] On terminal controller loss cancel/join the terminal task using existing native cleanup; on normal terminal exit close the controller/server and return the native outcome. Preserve trace adoption and local Ctrl-C handling. Do not add PTY tunneling, a terminal URL, a second config transport or terminal invocation scheduler.
- [ ] Remove internal HMAC env/settings and bootstrap JSON code from role dispatch/main. Keep external webhook HMAC and managed-name exclusions needed to prevent old internal env from being forwarded. The unpublished internal TCP/config-file deployment path is superseded, not retained as a parallel mode. Keep user-facing `env`, `engine.forwardEnv`, `--tui`, `--debug`, `--prompt` and default native launcher behavior.
- [ ] Ensure source configuration fetch and E controller initialization cannot create a readiness cycle. C may wait for H; E health exists before H; H hydrates and connects E before its normal ready state. Script-only work must still proceed when native binary launch fails but the mini-harness remains healthy.
- [ ] Run local/role/native suites, including `tests/boot/test_engine_runner_http.py`, `tests/test_task8a_roles.py`, `tests/engine/pi/test_driver.py`, `tests/engine/test_lifecycle.py`. Commit `refactor: remove role bootstrap files from local and split startup`; root reviews parity and deletion before Task 4.

## Task 4: Packaging, deletion audit and real acceptance

**Files:** Dockerfile, `docker/split/compose.yaml`, `compose-ephemeral.yaml`, `compose-acceptance.yaml`, `pod.yaml`, README and role JSON fixtures; `scripts/test-split.sh`, `tests/test_split_manifest.py`, `docs/schemas/operator-contract.md`, current operator handoff, `/tmp/to-ach.md`, new `docs/reports/unix-split-validation.md`.

- [ ] Add failing manifest assertions: two IPC directories; C lacks engine socket/full config; E lacks channels socket/full config; H writes channels mount and reads engine mount; no bootstrap files or HMAC env; no control ports8090/8081. Keep private state/home/codemem/workspace access unchanged.
- [ ] Replace bootstrap volumes with channels-ipc and engine-ipc at the same parent paths, with ownership direction from the spec. Image precreates owner directories UID10001. Pod fsGroup10001 enables first bind. All three role args preserve tini. Do not add an init container.
- [ ] H/E startup/liveness/readiness execute HTTP over UDS with the installed httpx library; C remains public HTTP8080. Example E startup command:

```python
import httpx
with httpx.Client(transport=httpx.HTTPTransport(
    uds='/run/ach-agent/engine/agent.sock'), base_url='http://ach-internal',
    timeout=2) as client:
    client.get('/healthz').raise_for_status()
```

- [ ] Update Compose acceptance client to use channel.sock with no secret lookup. Test both real Pi/OpenCode: two completed events sharing a native conversation, selected E env, held output then cancel/controller loss, H readiness failure, and script-only native-launch failure. Run a disposable H process restart while C/E and the pod network namespace remain intact; do not use Docker network-owner container replacement as a Kubernetes H-process-restart simulation.
- [ ] Verify actual mount permissions, not just YAML: C can submit through its read-only socket mount but cannot unlink it; E cannot access channel.sock/full config/H state; H reconnects after E recreates its owned socket; stale regular files/symlinks fail startup without deletion. Include persistent and ephemeral role startup with no manual projections, HMAC or internal control env.
- [ ] Run actual combined-image `docker -it --tui` for Pi and OpenCode: two typed turns, resize32x100, one native session, clean Ctrl-D/Ctrl-C; confirm tini PID1. Use synthetic upstreams, inspect stored native messages, and remove only task-owned containers/volumes/networks.
- [ ] Delete obsolete role JSON fixtures and signing/bootstrap tests; preserve their still-relevant secret, size, source-auth and lifecycle coverage under socket tests. Search imports/config references before deletion. Report runtime modules/lines removed versus added, separately from tests/docs; every new helper must serve this concrete increment.
- [ ] Run `rtk proxy ./scripts/dev.sh make _lint`, targeted transport/parity suites and unchanged `scripts/pre-push-check.sh` on the final committed source. Use a clean local clone on the project filesystem if the scanner needs a real `.git` directory; `/tmp` is a small tmpfs. Do not weaken checks or dismiss lifecycle failures as flakes.
- [ ] Rewrite `/tmp/to-ach.md` and tracked current contract for the **implemented** socket topology, exact mount permissions/probes, private H config and public socket inputs. Explicitly retain existing schema/forwardEnv and future two-Deployment direction; no operator internal-protocol work. Mark old bootstrap handoff superseded. Record commit/image digests, commands, results and any unexecuted cluster checks in the new report.
- [ ] Commit `package: finalize unix split contract and acceptance`; root final review verifies clean status, no task services left running, preserved native/session behavior and no claims of publication/operator integration.

## Plan self-review and execution order

| Requirement | Task |
| --- | --- |
| RAM queue/consumer, no new scheduler | 1; existing Router/Lane unchanged |
| channel.sock without secret/HMAC | 1 and actual mounts in4 |
| agent.sock with public config only | 2 |
| Native-session/cleanup compatibility | 2–3; real evidence4 |
| Local real TUI without private config/files | 3–4 |
| Simpler operator mounts/probes/no projections | 4 |
| Remove obsolete machinery, retain scope | 3–4 |

Tasks are sequential; no parallel edits to role/main startup. Root reviews each
Luna commit. Individual task commits are review checkpoints, not independently
publishable split versions; release only after all four acceptance gates pass.
This plan does not authorize the implementation of later proxy/queue features.
