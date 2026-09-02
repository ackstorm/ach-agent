# Channel Cleanup Hook Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a generic per-session `channels[].cleanup` script that runs after the session engine stops, so configuration-owned resources created by `prepare` can be released without leaking across a long-lived Pod.

**Architecture:** The harness registers a cleanup callback in `EnginePool` before running `prepare`; the pool owns that callback until immediate release, idle-TTL expiry, failure, or graceful shutdown, and serializes cleanup against new activity with its existing per-key lock. The operator reuses `PrepareSpec` for the singular cleanup block, resolves its independent `forwardEnv` allowlist, and renders the same internal `script`/`env`/`secretEnv`/`timeoutSeconds` shape accepted by ach-agent. Both components remain resource-agnostic: Git clone, cache, worktree, locking, and deletion behavior exist only in user-authored scripts and documentation examples.

**Tech Stack:** Python 3.12, asyncio, Pydantic v2, pytest, Prometheus client, Go, controller-runtime, Kubebuilder CRDs/CEL, Kubernetes `EnvVar`, JSON Schema Draft 2020-12, Helm, Make.

**Spec:** `docs/superpowers/specs/2026-09-02-channel-cleanup-hook-design.md`

## Global Constraints

- This is an additive `v1alpha1` API and agent-config change.
- `cleanup` is one optional sibling of `prepare`, never a list, and it is invalid unless the same channel has `prepare`.
- `cleanup` has the operator-facing shape `{script, forwardEnv, timeoutSeconds}` and the rendered shape `{script, env, secretEnv, timeoutSeconds}`.
- Cleanup runs only after the session engine is stopped: on idle-TTL expiry, `idleTtlSeconds: 0`, preparation or launch failure after reservation, and graceful shutdown.
- New activity for the same session cancels a pending expiry before prepare starts; cleanup already in progress completes under the per-session lock before the new prepare starts.
- Prepare remains fail-closed; cleanup spawn, timeout, and exit failures are logged and counted but never fail an invocation or pool release.
- Unknown cleanup `forwardEnv` names are ignored and remain unset.
- Secret plaintext never enters the generated ConfigMap; cleanup secrets use `ACH_SECRET_<CHANNEL>_CLEANUP_<NAME>` Pod aliases.
- The harness and operator contain no Git commands, repository-cache policy, worktree management, resource-specific locking, or automatic workspace deletion.
- No new runtime or Go dependencies are introduced.

---

### Task 1: Harness cleanup contract and safe script execution

**Files:**
- Modify: `src/ach_agent/config/schema.py:647-730`
- Modify: `src/ach_agent/boot/prepare.py:1-230`
- Modify: `src/ach_agent/boot/secrets.py:22-38`
- Modify: `src/ach_agent/engine/metrics.py:37-45`
- Modify: `tests/test_prepare.py`
- Modify: `tests/test_secret_forward_guard.py`

**Interfaces:**
- Consumes: existing `PrepareBlock`, `build_prepare_env(cfg, event, workspace)`, `_kill_process_group(proc)`, and `MessageEvent` normalization.
- Produces: `ChannelConfig.cleanup: PrepareBlock | None`.
- Produces: `async def run_cleanup(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None`.
- Produces: `CLEANUP_FAILURES`, Prometheus counter `ach_agent_cleanup_failures_total{reason="spawn|timeout|exit"}`.
- Produces: cleanup `secretEnv` names join the existing log-redaction and
  `engine.forwardEnv` stripping set.
- Preserves: `async def run_prepare(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None` and its fail-closed exception behavior.

- [ ] **Step 1: Add failing schema tests for the singular cleanup block and its dependency on prepare**

Add these tests to `tests/test_prepare.py`:

```python
def test_cleanup_uses_prepare_shape() -> None:
    ch = ChannelConfig.model_validate(
        {
            "name": "review",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
            "cleanup": {
                "script": "rm -rf -- \"$ACH_WORKSPACE\"",
                "env": {"MODE": "review"},
                "secretEnv": {"TOKEN": {"env": "CLEANUP_TOKEN"}},
                "timeoutSeconds": 30,
            },
        }
    )
    assert ch.cleanup is not None
    assert ch.cleanup.env == {"MODE": "review"}
    assert ch.cleanup.secret_env["TOKEN"].env == "CLEANUP_TOKEN"
    assert ch.cleanup.timeout_seconds == 30


def test_cleanup_requires_prepare() -> None:
    with pytest.raises(ValidationError, match="cleanup.*requires.*prepare"):
        ChannelConfig.model_validate(
            {
                "name": "review",
                "type": "cron",
                "cron": {"schedule": "* * * * *"},
                "cleanup": {"script": "true"},
            }
        )
```

- [ ] **Step 2: Run the schema tests and verify the red state**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest \
  tests/test_prepare.py::test_cleanup_uses_prepare_shape \
  tests/test_prepare.py::test_cleanup_requires_prepare -q
```

Expected: both tests fail because `ChannelConfig` forbids the undeclared `cleanup` field.

- [ ] **Step 3: Add `cleanup` to the harness schema using the existing hook shape**

In `ChannelConfig`, add the field and dependency check without adding a second block model:

```python
cleanup: PrepareBlock | None = None
```

At the start of `check_type_block_coherence`, add:

```python
if self.cleanup is not None and self.prepare is None:
    raise ValueError("channel cleanup requires channel prepare")
```

Update `PrepareBlock`'s docstring and validation messages from prepare-only wording to
`channel hook`, while retaining the public class name so the existing JSON Schema definition
and Python imports remain stable.

- [ ] **Step 4: Run the schema tests and verify they pass**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest \
  tests/test_prepare.py::test_cleanup_uses_prepare_shape \
  tests/test_prepare.py::test_cleanup_requires_prepare -q
```

Expected: `2 passed`.

- [ ] **Step 5: Add a failing cleanup secret-isolation test**

Add to `tests/test_secret_forward_guard.py`:

```python
def test_cleanup_secret_is_redacted_and_stripped_from_engine() -> None:
    channel = ChannelConfig.model_validate(
        {
            "name": "cleanup",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
            "cleanup": {
                "script": "true",
                "secretEnv": {"TOKEN": {"env": "ACH_SECRET_CLEANUP_TOKEN"}},
            },
        }
    )
    cfg = AgentConfig(
        channels=[channel],
        engine=EngineBlock(forward_env=["SAFE_VAR", "ACH_SECRET_CLEANUP_TOKEN"]),
        **_base_kwargs(),
    )

    assert "ACH_SECRET_CLEANUP_TOKEN" in collect_secret_env_names(cfg)
    assert strip_forwarded_secrets(cfg) == ["SAFE_VAR"]
```

Run:

```bash
rtk ./scripts/dev.sh uv run pytest \
  tests/test_secret_forward_guard.py::test_cleanup_secret_is_redacted_and_stripped_from_engine -q
```

Expected: FAIL because `collect_secret_env_names` only visits `ch.prepare.secret_env`.

- [ ] **Step 6: Extend secret collection across both channel hooks**

Replace the prepare-only branch in `collect_secret_env_names` with:

```python
for hook in (ch.prepare, ch.cleanup):
    if hook is not None:
        names.extend(src.env for src in hook.secret_env.values() if src.env)
```

Re-run the focused test and expect `1 passed`.

- [ ] **Step 7: Add failing cleanup execution tests**

Add tests that use the existing `_event()` and `prepare_workspace(...)` helpers:

```python
# Add to the existing imports.
from unittest.mock import AsyncMock, patch

from ach_agent.boot.prepare import run_cleanup


async def test_cleanup_runs_from_workspace_parent_with_isolated_env(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    marker = ws.parent / "cleanup.txt"
    cfg = _block(
        'printf "%s|%s|%s" "$ACH_WORKSPACE" "$ACH_SESSION_KEY" "$ONLY_CLEANUP" '
        f'> "{marker}"',
        env={"ONLY_CLEANUP": "yes"},
    )

    await run_cleanup(cfg, _event(), ws)

    assert marker.read_text() == f"{ws}|42:7|yes"


async def test_cleanup_nonzero_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures:
        await run_cleanup(_block("echo failed >&2; exit 7"), _event(), ws)

    failures.labels.assert_called_once_with(reason="exit")
    failures.labels.return_value.inc.assert_called_once_with()


async def test_cleanup_timeout_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures:
        await run_cleanup(_block("sleep 30", timeoutSeconds=1), _event(), ws)

    failures.labels.assert_called_once_with(reason="timeout")


async def test_cleanup_spawn_failure_is_best_effort(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")

    with (
        patch(
            "ach_agent.boot.prepare.asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("no shell")),
        ),
        patch("ach_agent.boot.prepare.CLEANUP_FAILURES") as failures,
    ):
        await run_cleanup(_block("true"), _event(), ws)

    failures.labels.assert_called_once_with(reason="spawn")


async def test_cleanup_cancellation_kills_process_group(tmp_path: Path) -> None:
    ws = prepare_workspace(str(tmp_path / "home"), str(tmp_path / "work"), "k")
    pid_file = ws.parent / "cleanup-pid"
    task = asyncio.create_task(
        run_cleanup(
            _block(f'echo $$ > "{pid_file}"; exec sleep 30'),
            _event(),
            ws,
        )
    )
    async with asyncio.timeout(2):
        while not pid_file.exists():
            await asyncio.sleep(0.01)

    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
```

- [ ] **Step 8: Run the cleanup execution tests and verify the red state**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/test_prepare.py -k 'cleanup' -q
```

Expected: import or collection fails because `run_cleanup` and `CLEANUP_FAILURES` do not exist.

- [ ] **Step 9: Extract the existing subprocess mechanics into one private executor**

In `src/ach_agent/boot/prepare.py`, keep environment construction in
`build_prepare_env` and replace duplicated subprocess concerns with this private interface:

```python
class _HookSpawnFailed(RuntimeError):
    pass


class _HookTimedOut(RuntimeError):
    pass


async def _execute_hook(
    script: str,
    timeout_seconds: int,
    *,
    cwd: Path,
    env: dict[str, str],
) -> tuple[int, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "/bin/sh",
            "-eu",
            "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            start_new_session=True,
        )
    except OSError as exc:
        raise _HookSpawnFailed(str(exc)) from exc

    try:
        _, stderr = await asyncio.wait_for(
            proc.communicate(script.encode()), timeout=timeout_seconds
        )
    except TimeoutError:
        await _kill_process_group(proc)
        raise _HookTimedOut from None
    except asyncio.CancelledError:
        await _kill_process_group(proc)
        raise
    return proc.returncode or 0, stderr
```

Refactor `run_prepare` to call `_execute_hook(..., cwd=workspace, env=env)` and translate
`_HookSpawnFailed`, `_HookTimedOut`, and nonzero return codes into its existing metric labels
and `PrepareFailed` messages. Do not change successful prepare logging or failure semantics.

- [ ] **Step 10: Implement best-effort `run_cleanup` and its metric**

Add to `src/ach_agent/engine/metrics.py`:

```python
CLEANUP_FAILURES: prometheus_client.Counter = prometheus_client.Counter(
    "ach_agent_cleanup_failures_total",
    "channel.cleanup scripts that failed after the session engine stopped",
    ["reason"],
)
```

Add `run_cleanup` to `src/ach_agent/boot/prepare.py`. It must run from
`workspace.parent`, use the same isolated environment builder, and consume operational
failures without raising:

```python
async def run_cleanup(cfg: PrepareBlock, event: MessageEvent, workspace: Path) -> None:
    env = build_prepare_env(cfg, event, workspace)
    started = asyncio.get_running_loop().time()
    try:
        returncode, stderr = await _execute_hook(
            cfg.script,
            cfg.timeout_seconds,
            cwd=workspace.parent,
            env=env,
        )
    except _HookSpawnFailed as exc:
        CLEANUP_FAILURES.labels(reason="spawn").inc()
        log.warning("cleanup: script could not be started", error=str(exc))
        return
    except _HookTimedOut:
        CLEANUP_FAILURES.labels(reason="timeout").inc()
        log.warning(
            "cleanup: script timed out",
            session_key=event.session_key,
            timeout_seconds=cfg.timeout_seconds,
        )
        return

    if returncode != 0:
        CLEANUP_FAILURES.labels(reason="exit").inc()
        log.warning(
            "cleanup: script exited nonzero",
            session_key=event.session_key,
            returncode=returncode,
            stderr=stderr.decode("utf-8", "replace")[-_STDERR_TAIL_CHARS:].strip(),
        )
        return

    log.info(
        "cleanup: workspace hook complete",
        session_key=event.session_key,
        workspace=str(workspace),
        duration_ms=int((asyncio.get_running_loop().time() - started) * 1000),
    )
```

Do not catch `asyncio.CancelledError` in `run_cleanup`; `_execute_hook` reaps the process
group and cancellation must continue toward shutdown.

- [ ] **Step 11: Run all hook and secret-isolation tests and commit the contract/executor slice**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/test_prepare.py tests/test_secret_forward_guard.py -q
rtk ./scripts/dev.sh uv run mypy --strict src/ach_agent/boot/prepare.py \
  src/ach_agent/boot/secrets.py src/ach_agent/config/schema.py src/ach_agent/engine/metrics.py
rtk git add src/ach_agent/config/schema.py src/ach_agent/boot/prepare.py \
  src/ach_agent/boot/secrets.py src/ach_agent/engine/metrics.py \
  tests/test_prepare.py tests/test_secret_forward_guard.py
rtk git commit -m "feat(channels): add cleanup script contract"
```

Expected: hook tests and strict type checking pass before the commit.

---

### Task 2: Race-safe cleanup ownership in `EnginePool`

**Files:**
- Modify: `src/ach_agent/engine/base/pool.py:229-480`
- Modify: `tests/engine/test_pool.py`

**Interfaces:**
- Consumes: `CleanupCallback = Callable[[], Awaitable[None]]`.
- Produces: `async def EnginePool.begin_session(session_key: str, cleanup: CleanupCallback | None) -> None`.
- Produces: `async def EnginePool.discard(session_key: str) -> None`.
- Preserves: `acquire`, `release`, `_expire`, and `stop_all` public behavior for sessions without cleanup.

- [ ] **Step 1: Add failing tests for expiry, cancellation, ordering, failure isolation, and shutdown**

Add these focused cases to `tests/engine/test_pool.py` using `AsyncMock` callbacks:

```python
# Add to the existing imports.
from unittest.mock import AsyncMock, MagicMock


async def test_ttl_expiry_stops_engine_before_cleanup() -> None:
    order: list[str] = []
    cleaned = asyncio.Event()
    driver = MagicMock(engine_type="test")
    driver.stop = AsyncMock(side_effect=lambda _server: order.append("stop"))
    pool = EnginePool(driver=driver)
    pool._start_server = AsyncMock(return_value=_make_fake_server())

    async def cleanup() -> None:
        order.append("cleanup")
        cleaned.set()

    await pool.begin_session("k1", cleanup)
    await pool.acquire("k1", _real_config())
    await pool.release("k1", ttl_seconds=0.01)

    assert order == []
    await asyncio.wait_for(cleaned.wait(), timeout=1)

    assert order == ["stop", "cleanup"]


async def test_ttl_zero_runs_cleanup_immediately() -> None:
    cleanup = AsyncMock()
    pool = EnginePool()
    pool._start_server = AsyncMock(return_value=_make_fake_server())

    await pool.begin_session("k1", cleanup)
    await pool.acquire("k1", _real_config())
    await pool.release("k1", ttl_seconds=0)

    cleanup.assert_awaited_once_with()


async def test_begin_session_cancels_pending_cleanup() -> None:
    cleanup = AsyncMock()
    pool = EnginePool()
    pool._start_server = AsyncMock(return_value=_make_fake_server())

    await pool.begin_session("k1", cleanup)
    await pool.acquire("k1", _real_config())
    await pool.release("k1", ttl_seconds=0.05)
    await pool.begin_session("k1", cleanup)
    await asyncio.sleep(0.12)

    cleanup.assert_not_awaited()


async def test_cleanup_in_progress_blocks_new_session_begin() -> None:
    started = asyncio.Event()
    finish = asyncio.Event()

    async def cleanup() -> None:
        started.set()
        await finish.wait()

    pool = EnginePool()
    pool._start_server = AsyncMock(return_value=_make_fake_server())
    await pool.begin_session("k1", cleanup)
    await pool.acquire("k1", _real_config())
    await pool.release("k1", ttl_seconds=0.01)
    await asyncio.wait_for(started.wait(), timeout=1)

    new_begin = asyncio.create_task(pool.begin_session("k1", None))
    await asyncio.sleep(0)
    assert not new_begin.done()
    finish.set()
    await new_begin


async def test_stop_all_runs_cleanup_without_server() -> None:
    cleanup = AsyncMock()
    pool = EnginePool()
    await pool.begin_session("prepare-failed", cleanup)

    await pool.stop_all()

    cleanup.assert_awaited_once_with()


async def test_cleanup_failure_does_not_escape_release() -> None:
    async def cleanup() -> None:
        raise RuntimeError("cleanup failed")

    pool = EnginePool()
    pool._start_server = AsyncMock(return_value=_make_fake_server())
    await pool.begin_session("k1", cleanup)
    await pool.acquire("k1", _real_config())

    await pool.release("k1", ttl_seconds=0)

    assert "k1" not in pool._servers
    assert "k1" not in pool._cleanups
```

- [ ] **Step 2: Run the pool tests and verify the red state**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/engine/test_pool.py -k 'cleanup or begin_session' -q
```

Expected: tests fail because `begin_session` and cleanup ownership are absent.

- [ ] **Step 3: Add cleanup state and the pre-prepare reservation method**

In `pool.py`, define and store the callback without a new class hierarchy:

```python
CleanupCallback = Callable[[], Awaitable[None]]

# EnginePool.__init__
self._cleanups: dict[str, CleanupCallback] = {}

async def begin_session(
    self,
    session_key: str,
    cleanup: CleanupCallback | None,
) -> None:
    """Cancel pending expiry and register the latest event's cleanup before prepare."""
    async with self._get_lock(session_key):
        ttl_task = self._ttl_tasks.pop(session_key, None)
        if ttl_task is not None and not ttl_task.done():
            ttl_task.cancel()
        if cleanup is None:
            self._cleanups.pop(session_key, None)
        else:
            self._cleanups[session_key] = cleanup
```

Keep the existing TTL cancellation inside `acquire`; callers without prepare still rely on it.

- [ ] **Step 4: Centralize stop-then-cleanup under the existing per-key lock**

Add one private helper that is called only while the session lock is held:

```python
async def _stop_locked(self, session_key: str) -> None:
    ttl_task = self._ttl_tasks.pop(session_key, None)
    if (
        ttl_task is not None
        and ttl_task is not asyncio.current_task()
        and not ttl_task.done()
    ):
        ttl_task.cancel()
    server = self._servers.pop(session_key, None)
    cleanup = self._cleanups.pop(session_key, None)
    self._ref_counts.pop(session_key, None)

    if server is not None:
        self._drop_token(server)
        try:
            await self._driver.stop(server)
        except Exception:  # noqa: BLE001
            log.warning("EnginePool: error stopping server", session_key=session_key, exc_info=True)

    if cleanup is not None:
        try:
            await cleanup()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.warning("EnginePool: cleanup callback failed", session_key=session_key, exc_info=True)
```

Holding the per-key lock through driver stop and cleanup is intentional: it is the smallest
mechanism that prevents a new prepare for the same session from racing cleanup, while other
session keys remain independent.

- [ ] **Step 5: Route every terminal lifecycle path through `_stop_locked`**

Implement `discard` and reduce `_stop` to the same locked path:

```python
async def discard(self, session_key: str) -> None:
    await self._stop(session_key)


async def _stop(self, session_key: str) -> None:
    async with self._get_lock(session_key):
        await self._stop_locked(session_key)
```

In `_expire`, retain the current ref-count and expiry-task identity checks, then call
`await self._stop_locked(session_key)` before releasing the lock. In `stop_all`, cancel
pending timers and iterate the union so cleanup-only reservations are not missed:

```python
keys = set(self._servers) | set(self._cleanups)
for session_key in keys:
    await self._stop(session_key)
```

When `acquire` replaces a dead server, stop only that server and retain the registered
cleanup because the logical session is continuing.

- [ ] **Step 6: Run the complete pool suite and commit**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/engine/test_pool.py -q
rtk ./scripts/dev.sh uv run mypy --strict src/ach_agent/engine/base/pool.py
rtk git add src/ach_agent/engine/base/pool.py tests/engine/test_pool.py
rtk git commit -m "feat(engine): clean session resources on pool expiry"
```

Expected: the existing reuse/ref-count tests and all new cleanup race tests pass.

---

### Task 3: Engine-runner lifecycle integration

**Files:**
- Modify: `src/ach_agent/boot/engine_runner.py:175-445`
- Modify: `tests/test_main_wiring.py`

**Interfaces:**
- Consumes: `EnginePool.begin_session`, `EnginePool.discard`, and `run_cleanup` from Tasks 1-2.
- Produces: callback registration before prepare and immediate discard after prepare/acquire failure.
- Preserves: reply/a2a error resolution and existing channel idle-TTL selection.

- [ ] **Step 1: Add failing runner tests that pin registration order and failure cleanup**

Add a focused pool double to `tests/test_main_wiring.py`:

```python
# Extend the existing typing import.
from collections.abc import Awaitable, Callable
from pathlib import Path


class _HookPool:
    def __init__(self, *, fail_acquire: bool = False) -> None:
        self.sessions: dict[str, str] = {}
        self.calls: list[str] = []
        self.cleanup: Callable[[], Awaitable[None]] | None = None
        self.fail_acquire = fail_acquire

    async def begin_session(self, _key: str, cleanup: Callable[[], Awaitable[None]] | None) -> None:
        self.calls.append("begin")
        self.cleanup = cleanup

    async def acquire(self, _key: str, _cfg: Any) -> Any:
        self.calls.append("acquire")
        if self.fail_acquire:
            raise RuntimeError("launch failed")
        return SimpleNamespace(proxy_token="tok")

    async def release(self, _key: str, ttl_seconds: float) -> None:
        self.calls.append(f"release:{ttl_seconds}")

    async def discard(self, _key: str) -> None:
        self.calls.append("discard")
        if self.cleanup is not None:
            await self.cleanup()


def _hook_channel() -> ChannelConfig:
    return ChannelConfig.model_validate(
        {
            "name": "hooks",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
            "cleanup": {"script": "true"},
        }
    )


def _hook_event() -> MessageEvent:
    return MessageEvent(
        idempotency_key="event-1",
        session_key="session-1",
        channel_name="hooks",
        payload={},
        delivery_context={},
        source_trait="async_no_retry",
    )
```

Add these tests below the helpers:

```python
async def test_engine_runner_registers_cleanup_before_prepare(tmp_path: Path) -> None:
    import ach_agent.engine.base.terminal as terminal
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver

    pool = _HookPool()

    async def prepare(*_args: Any) -> None:
        pool.calls.append("prepare")

    with (
        patch("ach_agent.boot.engine_runner.run_prepare", new=AsyncMock(side_effect=prepare)),
        patch("ach_agent.boot.engine_runner.run_cleanup", new=AsyncMock()),
        patch.object(
            terminal,
            "run_contract_turn",
            new=AsyncMock(return_value={"action": "none", "text": ""}),
        ),
    ):
        runner = make_engine_runner(
            pool=pool,
            driver=OpencodeDriver(),
            engine_cfg=EngineConfig(
                home=str(tmp_path / "home"),
                work_dir=str(tmp_path / "work"),
            ),
            max_invocation_seconds=30,
            channels_by_name={"hooks": _hook_channel()},
        )
        await runner(_hook_event(), lambda: None)

    assert pool.calls[:3] == ["begin", "prepare", "acquire"]


async def test_prepare_failure_discards_reserved_cleanup(tmp_path: Path) -> None:
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.boot.prepare import PrepareFailed
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver

    pool = _HookPool()
    cleanup = AsyncMock()
    with (
        patch(
            "ach_agent.boot.engine_runner.run_prepare",
            new=AsyncMock(side_effect=PrepareFailed("broken")),
        ),
        patch("ach_agent.boot.engine_runner.run_cleanup", new=cleanup),
    ):
        runner = make_engine_runner(
            pool=pool,
            driver=OpencodeDriver(),
            engine_cfg=EngineConfig(
                home=str(tmp_path / "home"),
                work_dir=str(tmp_path / "work"),
            ),
            max_invocation_seconds=30,
            channels_by_name={"hooks": _hook_channel()},
        )
        with pytest.raises(PrepareFailed, match="broken"):
            await runner(_hook_event(), lambda: None)

    assert pool.calls == ["begin", "discard"]
    cleanup.assert_awaited_once()


async def test_launch_failure_discards_reserved_cleanup(tmp_path: Path) -> None:
    from ach_agent.boot.engine_runner import make_engine_runner
    from ach_agent.engine.lifecycle import EngineConfig
    from ach_agent.engine.opencode.driver import OpencodeDriver

    pool = _HookPool(fail_acquire=True)
    cleanup = AsyncMock()
    with (
        patch("ach_agent.boot.engine_runner.run_prepare", new=AsyncMock()),
        patch("ach_agent.boot.engine_runner.run_cleanup", new=cleanup),
    ):
        runner = make_engine_runner(
            pool=pool,
            driver=OpencodeDriver(),
            engine_cfg=EngineConfig(
                home=str(tmp_path / "home"),
                work_dir=str(tmp_path / "work"),
            ),
            max_invocation_seconds=30,
            channels_by_name={"hooks": _hook_channel()},
        )
        with pytest.raises(RuntimeError, match="launch failed"):
            await runner(_hook_event(), lambda: None)

    assert pool.calls == ["begin", "acquire", "discard"]
    cleanup.assert_awaited_once()
```

- [ ] **Step 2: Run the focused tests and verify the red state**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/test_main_wiring.py \
  -k 'registers_cleanup or reserved_cleanup' -q
```

Expected: ordering and discard assertions fail because the runner does not call the new pool APIs.

- [ ] **Step 3: Register the latest event's cleanup before prepare**

Import `functools.partial` and `run_cleanup`. Inside `engine_runner`, initialize:

```python
session_reserved = False
```

Replace the current prepare block with:

```python
prepare_cfg = getattr(ch_cfg, "prepare", None) if ch_cfg is not None else None
cleanup_cfg = getattr(ch_cfg, "cleanup", None) if ch_cfg is not None else None
if prepare_cfg is not None:
    workspace = prepare_workspace(engine_cfg.home, engine_cfg.work_dir, event.session_key)
    cleanup = (
        partial(run_cleanup, cleanup_cfg, event, workspace)
        if cleanup_cfg is not None
        else None
    )
    await pool.begin_session(event.session_key, cleanup)
    session_reserved = True
    await run_prepare(prepare_cfg, event, workspace)
    if dataclasses.is_dataclass(invocation_engine_cfg) and not isinstance(
        invocation_engine_cfg, type
    ):
        invocation_engine_cfg = dataclasses.replace(
            invocation_engine_cfg,
            work_dir=str(workspace),
        )
```

The callback captures the newest event, so cleanup receives the most recent validated
`ACH_EVENT_*` values when the session finally expires.

- [ ] **Step 4: Discard a reservation that never acquired an engine**

At the start of the existing `finally`, before the `server is not None` release branch, add:

```python
if session_reserved and not acquired:
    try:
        await pool.discard(event.session_key)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "pool discard error",
            session_key=event.session_key,
            task_id=event.task_id,
            error=str(exc),
        )
```

Do not discard an acquired session here: its existing `release` call owns immediate or
TTL-delayed cleanup. A timed-out acquired invocation already releases with TTL zero.

- [ ] **Step 5: Run runner, router, and pool integration tests and commit**

Run:

```bash
rtk ./scripts/dev.sh uv run pytest tests/test_main_wiring.py tests/router tests/engine/test_pool.py -q
rtk ./scripts/dev.sh uv run mypy --strict src/ach_agent/boot/engine_runner.py
rtk git add src/ach_agent/boot/engine_runner.py tests/test_main_wiring.py
rtk git commit -m "feat(channels): bind cleanup to session lifecycle"
```

Expected: all selected tests pass and existing channels without prepare never call
`begin_session`.

---

### Task 4: Harness schema artifact, contract documentation, and GitLab example

**Files:**
- Generate: `docs/schemas/agent-config-v1.schema.json`
- Modify: `docs/schemas/operator-contract.md`
- Modify: `docs/references/2026-08-31-channel-owned-repo-workspace.md`
- Modify: `CHANGELOG.md`
- Test: `tests/config/test_schema_artifact.py`

**Interfaces:**
- Consumes: the `ChannelConfig.cleanup` schema from Task 1.
- Produces: the authoritative JSON Schema consumed by the sibling operator.
- Documents: ach-agent only schedules generic hooks; configured scripts own Git and deletion.

- [ ] **Step 1: Regenerate the authoritative agent-config schema**

Run:

```bash
rtk make schema
rtk ./scripts/dev.sh uv run pytest tests/config/test_schema_artifact.py -q
```

Expected: the artifact contains `channels[].cleanup` pointing to the same `PrepareBlock`
definition used by `prepare`, and schema drift tests pass.

- [ ] **Step 2: Document lifecycle and failure semantics in the operator contract**

Extend the channel-hook section in `docs/schemas/operator-contract.md` with this normative
text:

```markdown
`cleanup` is an optional singular sibling of `prepare` and is valid only when
`prepare` is present. The lifecycle is:

`reserve session/cancel expiry -> prepare -> acquire engine -> invocation ->
release -> idle TTL -> stop engine -> cleanup`.

Cleanup runs through `/bin/sh -eu -s` from the parent of `ACH_WORKSPACE` and
receives the latest event's validated `ACH_EVENT_*` values plus only its own
configured `env` and `secretEnv`. Spawn, timeout, and nonzero-exit failures are
best-effort: they are logged and counted without changing invocation delivery.
Graceful shutdown attempts every registered cleanup after stopping its engine;
`SIGKILL`, node loss, and container-runtime failure provide no cleanup guarantee.
```

- [ ] **Step 3: Append the shipped cleanup lifecycle to the repository-workspace reference**

Preserve the document's historical typed-design reasoning. Update its shipped-status paragraph
to say that ach-agent executes configuration-owned scripts and contains no Git/cache policy,
then add a `## Shipped prepare/cleanup example` section containing:

```yaml
prepare:
  script: |
    set -eu
    AUTH=$(printf 'oauth2:%s' "$GITLAB_TOKEN" | base64 -w0)
    export GIT_CONFIG_COUNT=1
    export GIT_CONFIG_KEY_0=http.extraHeader
    export GIT_CONFIG_VALUE_0="Authorization: Basic $AUTH"
    export GIT_TERMINAL_PROMPT=0
    export GIT_LFS_SKIP_SMUDGE=1
    REPO="$ACH_WORKSPACE/repo"
    URL="$GITLAB_REPO_BASEURL/$ACH_EVENT_PROJECT_PATH.git"
    if [ -d "$REPO/.git" ]; then
      git -C "$REPO" remote set-url origin "$URL"
      git -C "$REPO" fetch --prune origin
    else
      git clone --filter=blob:none --no-recurse-submodules "$URL" "$REPO"
    fi
    if [ -n "${ACH_EVENT_MR_IID:-}" ] && [ -n "${ACH_EVENT_HEAD_SHA:-}" ]; then
      git -C "$REPO" fetch origin "refs/merge-requests/$ACH_EVENT_MR_IID/head"
      git -C "$REPO" checkout --detach "$ACH_EVENT_HEAD_SHA"
    fi
  forwardEnv: [GITLAB_TOKEN, GITLAB_REPO_BASEURL]
  timeoutSeconds: 120
cleanup:
  script: |
    set -eu
    test -n "$ACH_WORKSPACE"
    rm -rf -- "$ACH_WORKSPACE"
  timeoutSeconds: 30
```

Explain immediately below it that a shared bare mirror/worktree optimization, including its
cross-session locking, must also be implemented in these scripts rather than in ach-agent.

- [ ] **Step 4: Add the unreleased changelog entry and run the full harness gate**

Add under `[unreleased]`:

```markdown
- Add best-effort `channels[].cleanup` hooks bound to session engine expiry and graceful shutdown.
```

Run:

```bash
rtk make verify
rtk make docs-build
rtk git diff --check
rtk git status --short
```

Expected: lint, strict typing, all non-e2e tests, conformance, secret scans, and strict docs build
pass. Only files named in Tasks 1-4 and the user-owned pre-existing untracked paths may differ.

- [ ] **Step 5: Commit generated contract and documentation**

Run:

```bash
rtk git add docs/schemas/agent-config-v1.schema.json \
  docs/schemas/operator-contract.md \
  docs/references/2026-08-31-channel-owned-repo-workspace.md CHANGELOG.md
rtk git commit -m "docs(channels): publish cleanup hook contract"
```

---

### Task 5: Operator API, forwarding, and secret aliases

**Files:**
- Modify: `../ach/api/ach/v1alpha1/achagent_types.go:251-307`
- Modify: `../ach/internal/agentrender/config.go` (`ChannelBlock`)
- Modify: `../ach/internal/agentrender/render.go:137-200,541-575`
- Modify: `../ach/internal/agentrender/render_test.go:681-745`
- Modify: `../ach/internal/controller/ach/achagent_workload_test.go:268-290`
- Modify: `../ach/internal/controller/ach/achagent_envtest_test.go:492-550`

**Interfaces:**
- Consumes: merged `ResolveEnv(agent, profile []corev1.EnvVar) []corev1.EnvVar` already shipped.
- Produces: `ChannelSpec.Cleanup *PrepareSpec` with admission rule `cleanup => prepare`.
- Produces: rendered `ChannelBlock.Cleanup *PrepareBlock`.
- Produces: generated secret alias `ACH_SECRET_<CHANNEL>_CLEANUP_<NAME>`.
- Preserves: missing forward names are ignored and prepare aliases remain byte-compatible.

- [ ] **Step 1: Add failing render and secret-alias tests**

Add a table-driven test to `internal/agentrender/render_test.go` that configures both hooks:

```go
func TestCleanup_ForwardEnvResolvesLiteralsSecretsAndMissing(t *testing.T) {
	tc := renderMatrix()["minimal"]
	tc.profile.Spec.Env = []corev1.EnvVar{
		{Name: "GITLAB_BASE_URL", Value: "https://git.example.com"},
		{Name: "GITLAB_TOKEN", ValueFrom: &corev1.EnvVarSource{
			SecretKeyRef: &corev1.SecretKeySelector{
				LocalObjectReference: corev1.LocalObjectReference{Name: "gitlab"},
				Key: "token",
			},
		}},
	}
	tc.agent.Spec.Channels[0].Prepare = &achv1alpha1.PrepareSpec{Script: "true"}
	tc.agent.Spec.Channels[0].Cleanup = &achv1alpha1.PrepareSpec{
		Script: "rm -rf -- \"$ACH_WORKSPACE\"",
		ForwardEnv: []string{"GITLAB_BASE_URL", "GITLAB_TOKEN", "MISSING"},
	}

	cfg, err := Render(tc.profile, tc.agent, "https://ach")
	if err != nil {
		t.Fatal(err)
	}
	got := cfg.Channels[0].Cleanup
	if got == nil || got.Env["GITLAB_BASE_URL"] != "https://git.example.com" {
		t.Fatalf("cleanup literal env = %#v", got)
	}
	if got.SecretEnv["GITLAB_TOKEN"].Env != "ACH_SECRET_C_CLEANUP_GITLAB_TOKEN" {
		t.Fatalf("cleanup secret env = %#v", got.SecretEnv)
	}
	if _, found := got.Env["MISSING"]; found {
		t.Fatal("missing forwardEnv name must remain absent")
	}
	if _, found := got.SecretEnv["MISSING"]; found {
		t.Fatal("missing forwardEnv name must not become secretEnv")
	}
	refs := ChannelSecretEnv(tc.profile, tc.agent)
	if len(refs) != 1 || refs[0].EnvName != "ACH_SECRET_C_CLEANUP_GITLAB_TOKEN" ||
		refs[0].SecretName != "gitlab" || refs[0].Key != "token" {
		t.Fatalf("cleanup Pod secret alias = %+v", refs)
	}
}
```

For `TestBuildAgentEnv_PrepareSecretGetsGeneratedAlias`, add cleanup to the existing channel:

```go
Cleanup: &achv1alpha1.PrepareSpec{
	Script: "true",
	ForwardEnv: []string{"GITLAB_TOKEN"},
},
```

Replace the alias local with two locals and collect both names:

```go
var original, prepareAlias, cleanupAlias *corev1.EnvVar
for i := range env {
	e := &env[i]
	switch e.Name {
	case "GITLAB_TOKEN":
		original = e
	case "ACH_SECRET_GITLAB_MR_REVIEW_PREPARE_GITLAB_TOKEN":
		prepareAlias = e
	case "ACH_SECRET_GITLAB_MR_REVIEW_CLEANUP_GITLAB_TOKEN":
		cleanupAlias = e
	}
}
for name, alias := range map[string]*corev1.EnvVar{
	"prepare": prepareAlias,
	"cleanup": cleanupAlias,
} {
	if alias == nil || alias.ValueFrom == nil || alias.ValueFrom.SecretKeyRef == nil ||
		alias.ValueFrom.SecretKeyRef.Name != "gl-clone" ||
		alias.ValueFrom.SecretKeyRef.Key != "token" {
		t.Fatalf("%s alias=%+v original=%+v", name, alias, original)
	}
}
```

- [ ] **Step 2: Run focused operator tests and verify the compile-time red state**

From `../ach`, run:

```bash
rtk make test-unit-pkg PKG=./internal/agentrender/... 
```

Expected: Go compilation fails because `ChannelSpec.Cleanup` and `ChannelBlock.Cleanup` do not
exist.

- [ ] **Step 3: Add the CRD field and rendered config field by reusing existing types**

Append the new CEL marker to the existing `ChannelSpec` markers:

```go
// +kubebuilder:validation:XValidation:rule="!has(self.cleanup) || has(self.prepare)",message="channels.cleanup requires channels.prepare"
```

Then add the field directly after `Prepare`:

```go
// +optional
Prepare *PrepareSpec `json:"prepare,omitempty"`
// +optional
Cleanup *PrepareSpec `json:"cleanup,omitempty"`
```

Add to `internal/agentrender/config.go`:

```go
Cleanup *PrepareBlock `json:"cleanup,omitempty"`
```

Keep `PrepareSpec` and `PrepareBlock` names; cleanup deliberately shares those stable shapes.

- [ ] **Step 4: Generalize the existing renderer across exactly two hook phases**

Replace `prepareSecretEnvName` with:

```go
func hookSecretEnvName(ch *achv1alpha1.ChannelSpec, phase, varName string) string {
	return "ACH_SECRET_" + sanitizeEnvSegment(ch.Name) + "_" + phase + "_" + varName
}
```

Replace `renderPrepare` with this shared helper:

```go
func renderHook(
	ch *achv1alpha1.ChannelSpec,
	hook *achv1alpha1.PrepareSpec,
	resolvedEnv []corev1.EnvVar,
	phase string,
) *PrepareBlock {
	if hook == nil {
		return nil
	}
	out := &PrepareBlock{Script: hook.Script, TimeoutSeconds: hook.TimeoutSeconds}
	env := indexEnv(resolvedEnv)
	for _, name := range hook.ForwardEnv {
		e, ok := env[name]
		if !ok {
			continue
		}
		if e.ValueFrom != nil && e.ValueFrom.SecretKeyRef != nil {
			if out.SecretEnv == nil {
				out.SecretEnv = map[string]SecretSourceBlock{}
			}
			out.SecretEnv[name] = SecretSourceBlock{Env: hookSecretEnvName(ch, phase, name)}
			continue
		}
		if out.Env == nil {
			out.Env = map[string]string{}
		}
		out.Env[name] = e.Value
	}
	return out
}
```

Render both siblings in `renderChannel`:

```go
Prepare: renderHook(ch, ch.Prepare, resolvedEnv, "PREPARE"),
Cleanup: renderHook(ch, ch.Cleanup, resolvedEnv, "CLEANUP"),
```

- [ ] **Step 5: Collect generated Pod aliases for both independent allowlists**

In `ChannelSecretEnv`, iterate these two explicit phases after channel-auth handling:

```go
hooks := []struct {
	phase string
	spec  *achv1alpha1.PrepareSpec
}{
	{phase: "PREPARE", spec: ch.Prepare},
	{phase: "CLEANUP", spec: ch.Cleanup},
}
for _, hook := range hooks {
	if hook.spec == nil {
		continue
	}
	for _, name := range slices.Sorted(slices.Values(hook.spec.ForwardEnv)) {
		e, ok := env[name]
		if !ok || e.ValueFrom == nil || e.ValueFrom.SecretKeyRef == nil {
			continue
		}
		ref := e.ValueFrom.SecretKeyRef
		out = append(out, ChannelSecretEnvRef{
			EnvName: hookSecretEnvName(ch, hook.phase, name),
			SecretName: ref.Name,
			Key: ref.Key,
		})
	}
}
```

No controller production change is required: `BuildAgentEnv` already consumes every entry
returned by `ChannelSecretEnv`.

- [ ] **Step 6: Add an envtest admission and reconciliation case**

Extend `TestACHAgent_EnvInheritancePrepareAndSecretRotation` with cleanup forwarding and assert:

```go
Cleanup: &achv1alpha1.PrepareSpec{
	Script: "true",
	ForwardEnv: []string{"GITLAB_BASE_URL", "GITLAB_TOKEN", "MISSING"},
},
```

Add `ACH_SECRET_REVIEW_CLEANUP_GITLAB_TOKEN` to the existing `wantSecrets` map. After the
existing ConfigMap decode, add:

```go
cleanup := cfg["channels"].([]any)[0].(map[string]any)["cleanup"].(map[string]any)
if cleanup["env"].(map[string]any)["GITLAB_BASE_URL"] != "https://git.example.com" {
	t.Fatalf("cleanup literals = %v", cleanup["env"])
}
if cleanup["secretEnv"].(map[string]any)["GITLAB_TOKEN"].(map[string]any)["env"] != "ACH_SECRET_REVIEW_CLEANUP_GITLAB_TOKEN" {
	t.Fatalf("cleanup secret aliases = %v", cleanup["secretEnv"])
}
if _, ok := cleanup["env"].(map[string]any)["MISSING"]; ok {
	t.Fatal("unknown cleanup forwardEnv name must remain unset")
}
```

The existing Secret rotation assertion then proves that the cleanup alias participates in the
same referenced-secret hash. Add this admission test:

```go
func TestACHAgent_CleanupRequiresPrepare(t *testing.T) {
	ctx := context.Background()
	agent := &achv1alpha1.ACHAgent{
		ObjectMeta: metav1.ObjectMeta{Name: "cleanup-without-prepare", Namespace: WatchNamespace},
		Spec: achv1alpha1.ACHAgentSpec{
			ProfileRef: achv1alpha1.LocalObjectRef{Name: "unused"},
			Identity: achv1alpha1.IdentitySpec{SecretRef: achv1alpha1.SecretKeyRef{
				Name: "unused", Key: "ek",
			}},
			Channels: []achv1alpha1.ChannelSpec{{
				Name: "nightly",
				Type: "cron",
				Cron: &achv1alpha1.CronSpec{Schedule: "0 1 * * *"},
				Cleanup: &achv1alpha1.PrepareSpec{Script: "true"},
			}},
		},
	}

	err := k8sClient.Create(ctx, agent)
	if err == nil || !strings.Contains(err.Error(), "channels.cleanup requires channels.prepare") {
		t.Fatalf("Create error = %v", err)
	}
}
```

- [ ] **Step 7: Run focused unit tests before generation**

Run from `../ach`:

```bash
rtk make test-unit-pkg PKG=./internal/agentrender/...
```

Expected: renderer tests pass; controller/envtest and generated-code drift are handled in Task 6.

---

### Task 6: Operator generated surfaces, examples, and cross-repository verification

**Files:**
- Generate: `../ach/api/ach/v1alpha1/zz_generated.deepcopy.go`
- Generate: `../ach/config/crd/bases/ach.ackstorm.ai_achagents.yaml`
- Generate: `../ach/deploy/helm/ach/crd-sources/ach.ackstorm.ai_achagents.yaml`
- Generate: `../ach/docs/api-reference/ach.ackstorm.ai.md`
- Modify generated golden: `../ach/api/ach/v1alpha1/testdata/achagent_field_shapes.golden`
- Copy generated schema: `../ach/internal/agentrender/testdata/agent-config-v1.schema.json`
- Modify: `../ach/examples/agent-runtime/agent.yaml`
- Modify: `../ach/examples/agent-runtime/README.md`
- Modify: `../ach/CHANGELOG.md`
- Test: `../ach/internal/agentrender/schema_test.go`

**Interfaces:**
- Consumes: authoritative `docs/schemas/agent-config-v1.schema.json` from Task 4.
- Produces: synchronized CRD, Helm, API-reference, golden, example, and vendored-schema surfaces.
- Produces: one operator implementation commit; no release, tag, or remote push is part of this plan.

- [ ] **Step 1: Regenerate Go and CRD surfaces**

From `../ach`, run:

```bash
rtk ./scripts/dev.sh make gen-code gen-manifests helm-sync gen-crd-ref-docs
rtk ./scripts/dev.sh bash -lc \
  'UPDATE=1 go test ./api/ach/v1alpha1 -run TestACHAgentCRD_FieldShapesStable -count=1'
```

Expected: the CRD exposes singular `channels[].cleanup`, includes the CEL dependency on
`prepare`, generated deepcopy code copies the pointer, and the golden adds cleanup fields
without changing existing shapes.

- [ ] **Step 2: Add cleanup to the schema render matrix**

Add this case to `renderMatrix()` in `internal/agentrender/schema_test.go`:

```go
cleanup := base("cleanup", cron)
cleanup.agent.Spec.Env = []corev1.EnvVar{{Name: "MODE", Value: "review"}}
cleanup.agent.Spec.Channels[0].Prepare = &achv1alpha1.PrepareSpec{Script: "true"}
cleanup.agent.Spec.Channels[0].Cleanup = &achv1alpha1.PrepareSpec{
	Script: "true", ForwardEnv: []string{"MODE"},
}
m["cleanup"] = cleanup
```

- [ ] **Step 3: Vendor the exact harness schema and run schema compatibility tests**

From `../ach`, run:

```bash
rtk cp ../ach-agent/docs/schemas/agent-config-v1.schema.json \
  internal/agentrender/testdata/agent-config-v1.schema.json
rtk make test-unit-pkg PKG=./internal/agentrender/...
```

Expected: `TestSchema_NoDrift` byte-compares cleanly and the render matrix validates cleanup
against the vendored Draft 2020-12 schema.

- [ ] **Step 4: Update the runnable operator example without adding Git behavior to Go**

In `examples/agent-runtime/agent.yaml`, retain the idempotent Git logic inside `prepare`, add
the cleanup block below, and update the channel prompt to tell the agent where its configured
checkout is:

```yaml
prompt: >-
  Review this event. The channel prepare hook maintains a repository checkout at
  $ACH_WORKSPACE/repo; inspect and test that working tree before replying.
cleanup:
  script: |
    set -eu
    test -n "$ACH_WORKSPACE"
    rm -rf -- "$ACH_WORKSPACE"
  timeoutSeconds: 30
```

In `examples/agent-runtime/README.md`, document that both blocks are generic user scripts,
cleanup is attempted only after engine stop, abrupt termination is not guaranteed, and mirror/
worktree caching belongs in these scripts rather than the operator or harness.

Use this exact note:

```markdown
`prepare` and `cleanup` are generic configuration-owned shell hooks. The
operator and harness do not clone, cache, lock, or delete repositories. Cleanup
runs after the session engine stops on idle-TTL expiry (or immediately when the
TTL is zero) and is also attempted during graceful shutdown; abrupt Pod/node
termination cannot guarantee it. If you use a shared bare mirror and per-session
worktrees, implement both the Git operations and cross-session locking inside
these scripts.
```

- [ ] **Step 5: Add the operator changelog entry**

Add under `[unreleased]`:

```markdown
- Add singular `channels[].cleanup` hooks with independent environment forwarding and secret aliases.
```

- [ ] **Step 6: Run focused envtest and all generation/lint gates**

From `../ach`, run:

```bash
rtk make test-envtest-pkg PKG=./internal/controller/ach/... \
  FOCUS='TestACHAgent_EnvInheritancePrepareAndSecretRotation|TestACHAgent_CleanupRequiresPrepare'
rtk make test-full
rtk make qa-lint
rtk make helm-sync-check
rtk ./scripts/dev.sh make docs-build
rtk git diff --check
```

Expected: focused admission/reconciliation coverage, all non-cluster tests with race detection,
lint, generated Helm sync, and documentation build pass.

- [ ] **Step 7: Review the operator diff and commit it**

Run from `../ach`:

```bash
rtk git status --short
rtk git diff --stat
rtk git add api/ach/v1alpha1/achagent_types.go \
  api/ach/v1alpha1/zz_generated.deepcopy.go \
  api/ach/v1alpha1/testdata/achagent_field_shapes.golden \
  internal/agentrender/config.go internal/agentrender/render.go \
  internal/agentrender/render_test.go internal/agentrender/schema_test.go \
  internal/agentrender/testdata/agent-config-v1.schema.json \
  internal/controller/ach/achagent_workload_test.go \
  internal/controller/ach/achagent_envtest_test.go \
  config/crd/bases/ach.ackstorm.ai_achagents.yaml \
  deploy/helm/ach/crd-sources/ach.ackstorm.ai_achagents.yaml \
  docs/api-reference/ach.ackstorm.ai.md \
  examples/agent-runtime/agent.yaml examples/agent-runtime/README.md CHANGELOG.md
rtk git commit -m "feat(channels): render cleanup lifecycle hooks"
```

Expected: no unrelated user files are staged.

- [ ] **Step 8: Run final clean-tree verification in both repositories**

From `ach-agent`:

```bash
rtk make verify
rtk make docs-build
rtk git status --short
```

From `../ach`:

```bash
rtk make pre-push
rtk git status --short
```

Expected: both verification bundles pass. The only remaining untracked files may be the
pre-existing user-owned planning directories plus this approved spec and plan if they were not
included in an explicit documentation commit.
