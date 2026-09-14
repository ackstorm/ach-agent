from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from ach_agent.boot.engine_runner import make_engine_runner
from ach_agent.channels.message_event import MessageEvent
from ach_agent.engine.base.driver import TurnResult
from ach_agent.execution.wire import ExecutionHandle, PublicEngineConfig


class _FakeClient:
    controller_id = "controller"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.handle = ExecutionHandle(
            instance_id="instance",
            controller_id=self.controller_id,
            execution_id="execution",
            invocation_id="invocation",
            proxy_route="route-token",
        )

    async def acquire(self, request: Any) -> ExecutionHandle:
        self.calls.append(("acquire", request))
        return self.handle

    def turn_callable(self, handle: ExecutionHandle):
        assert handle is self.handle

        async def run_turn(**kwargs: Any) -> TurnResult:
            self.calls.append(("turn", kwargs))
            kwargs["stats"]["session_ref"] = "ses_http"
            return TurnResult(text='{"action":"none","text":"ok"}', session_ref="ses_http")

        return run_turn

    async def session_op(self, request: Any) -> None:
        self.calls.append(("session_op", request))

    async def release(self, request: Any) -> None:
        self.calls.append(("release", request))

    async def cancel(self, controller_id: str, invocation_id: str) -> None:
        self.calls.append(("cancel", (controller_id, invocation_id)))

    async def cancel_handle(self, handle: ExecutionHandle) -> None:
        await self.cancel(handle.controller_id, handle.invocation_id)

    async def prepare_workspace(self, request: Any) -> dict[str, str]:
        from ach_agent.engine.workspace import workspace_dir

        workspace = workspace_dir(request.work_dir, request.session_key)
        workspace.mkdir(parents=True, exist_ok=True)
        self.calls.append(("prepare", request))
        return {"status": "ok", "workspace": str(workspace)}

    async def handoff_workspace(self, request: Any) -> dict[str, str]:
        from ach_agent.engine.workspace import workspace_dir

        workspace = workspace_dir(request.work_dir, request.session_key)
        return {"status": "ok", "workspace": str(workspace)}


@pytest.mark.asyncio
async def test_runner_uses_execution_client_and_releases_handle(monkeypatch: Any) -> None:
    import ach_agent.engine.base.terminal as terminal

    async def fake_contract(run_turn: Any, **kwargs: Any) -> dict[str, str]:
        return await _contract_result(run_turn, kwargs)

    async def _contract_result(run_turn: Any, kwargs: dict[str, Any]) -> dict[str, str]:
        result = await run_turn(
            prompt=kwargs["prompt"],
            max_tool_calls=kwargs["max_tool_calls"],
            on_text=kwargs["on_text"],
            on_tool=kwargs["on_tool"],
            stats=kwargs["stats"],
        )
        return {"action": "none", "text": result.text}

    monkeypatch.setattr(terminal, "run_contract_turn", fake_contract)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(),
        max_invocation_seconds=30,
    )

    event = MessageEvent(
        idempotency_key="event-1",
        session_key="session-1",
        channel_name="chat",
        payload={"message": "hello"},
    )
    result = await runner(event, lambda: None)

    assert result == {"action": "none", "text": '{"action":"none","text":"ok"}'}
    assert [name for name, _ in client.calls] == ["acquire", "turn", "release"]
    acquire = client.calls[0][1]
    assert acquire.controller_id == "controller"
    assert acquire.lane_key == "session-1"
    assert acquire.conversation_key == "session-1"
    assert acquire.config.model == "gpt-4o-mini"
    release = client.calls[-1][1]
    assert release.execution_id == "execution"
    assert release.invocation_id == "invocation"


@pytest.mark.asyncio
async def test_runner_executes_prepare_in_h_after_e_reservation(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.engine.base.terminal as terminal
    from ach_agent.config.schema import ChannelConfig

    async def fake_contract(run_turn: Any, **kwargs: Any) -> dict[str, str]:
        result = await run_turn(
            prompt=kwargs["prompt"],
            max_tool_calls=kwargs["max_tool_calls"],
            on_text=kwargs["on_text"],
            on_tool=kwargs["on_tool"],
            stats=kwargs["stats"],
        )
        return {"action": "none", "text": result.text}

    monkeypatch.setattr(terminal, "run_contract_turn", fake_contract)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(work_dir=str(tmp_path / "workspace")),
        max_invocation_seconds=30,
        channels_by_name={
            "chat": ChannelConfig.model_validate(
                {
                    "name": "chat",
                    "type": "cron",
                    "cron": {"schedule": "* * * * *"},
                    "prepare": {"script": "printf prepared > h-marker"},
                }
            )
        },
    )

    await runner(
        MessageEvent(
            idempotency_key="event-prepare",
            session_key="session-prepare",
            channel_name="chat",
            payload={},
        ),
        lambda: None,
    )

    names = [name for name, _ in client.calls]
    assert names[:3] == ["prepare", "acquire", "turn"]
    assert names[-1] == "release"
    workspace = client.calls[0][1].work_dir
    from ach_agent.engine.workspace import workspace_dir

    assert (workspace_dir(workspace, "session-prepare") / "h-marker").read_text() == "prepared"


@pytest.mark.asyncio
async def test_runner_carries_one_deadline_through_memory_and_prepare(
    monkeypatch: Any, tmp_path: Any
) -> None:
    import ach_agent.boot.engine_runner as runner_module
    from ach_agent.config.schema import ChannelConfig

    async def slow_memory(*args: Any, **kwargs: Any) -> tuple[dict[str, str], str]:
        await asyncio.sleep(0.02)
        return {}, ""

    monkeypatch.setattr(runner_module, "select_memory_wiring_async", slow_memory)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(
            home=str(tmp_path / "home"), work_dir=str(tmp_path / "workspace")
        ),
        max_invocation_seconds=0.08,
        channels_by_name={
            "chat": ChannelConfig.model_validate(
                {
                    "name": "chat",
                    "type": "cron",
                    "cron": {"schedule": "* * * * *"},
                    "prepare": {"script": "true"},
                }
            )
        },
    )
    await runner(
        MessageEvent(
            idempotency_key="deadline",
            session_key="deadline-session",
            channel_name="chat",
            payload={},
        ),
        lambda: None,
    )
    prepare = next(request for name, request in client.calls if name == "prepare")
    acquire = next(request for name, request in client.calls if name == "acquire")
    assert prepare.remaining_seconds < 0.08
    assert acquire.remaining_seconds <= prepare.remaining_seconds


@pytest.mark.asyncio
async def test_runner_rotate_discards_before_forgetting(monkeypatch: Any) -> None:
    import ach_agent.engine.base.terminal as terminal
    from ach_agent.config.schema import ChannelConfig

    async def fake_contract(run_turn: Any, **kwargs: Any) -> dict[str, str]:
        result = await run_turn(
            prompt=kwargs["prompt"],
            max_tool_calls=kwargs["max_tool_calls"],
            on_text=kwargs["on_text"],
            on_tool=kwargs["on_tool"],
            stats=kwargs["stats"],
        )
        return {"action": "none", "text": result.text}

    monkeypatch.setattr(terminal, "run_contract_turn", fake_contract)
    client = _FakeClient()
    runner = make_engine_runner(
        client=client,
        engine_cfg=PublicEngineConfig(),
        max_invocation_seconds=30,
        channels_by_name={
            "chat": ChannelConfig.model_validate(
                {
                    "name": "chat",
                    "type": "cron",
                    "cron": {"schedule": "* * * * *"},
                    "session": {
                        "type": "custom",
                        "key": "{{ payload.key }}",
                        "maxTokens": 1,
                        "overflow": "rotate",
                    },
                }
            )
        },
    )
    event = MessageEvent(
        idempotency_key="event-2",
        session_key="session-2",
        channel_name="chat",
        payload={"key": "conversation"},
    )
    original = client.turn_callable

    def turn_callable(handle: ExecutionHandle):
        run_turn = original(handle)

        async def wrapped(**kwargs: Any) -> TurnResult:
            result = await run_turn(**kwargs)
            kwargs["stats"]["usage"] = SimpleNamespace(input_tokens=2, cost=1.0)
            return result

        return wrapped

    client.turn_callable = turn_callable  # type: ignore[method-assign]
    await runner(event, lambda: None)

    assert [request.operation for name, request in client.calls if name == "session_op"] == [
        "discard",
        "forget",
    ]


@pytest.mark.asyncio
async def test_runner_drives_real_execution_service_over_http() -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver

    fake_driver = FakeDriver()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    app = create_execution_app(service)
    from tests.execution.test_http import _running_server

    async with _running_server(app) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(),
            max_invocation_seconds=10,
        )
        event = MessageEvent(
            idempotency_key="http-event",
            session_key="http-session",
            channel_name="chat",
            payload={"message": "hello"},
        )
        result = await runner(event, lambda: None)
        assert result == {"action": "none", "text": "reply"}
        assert fake_driver.resolved_conversations == [("http-session", True)]
        assert fake_driver.turn_session_refs == ["native-ref"]
        await client.close()


@pytest.mark.asyncio
async def test_runner_rotate_discards_then_forgets_and_gets_fresh_native_session() -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.engine.base.events import OpenCodeUsage
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    class RotatingDriver(FakeDriver):
        def __init__(self) -> None:
            super().__init__()
            self.discarded_refs: list[str] = []
            self.resolved_refs: list[str] = []

        async def resolve_session(
            self,
            server: Any,
            *,
            conv_key: str,
            reuse: bool,
            sessions: Any,
            stats: Any,
        ) -> str:
            ref = sessions.get(conv_key)
            if ref is None:
                ref = f"native-{len(self.resolved_refs) + 1}"
            self.resolved_refs.append(ref)
            sessions[conv_key] = ref
            self.resolved_conversations.append((conv_key, reuse))
            stats["session_ref"] = ref
            return ref

        async def discard_session(self, server: Any, session_ref: str) -> None:
            self.discarded_refs.append(session_ref)

    fake_driver = RotatingDriver()

    fake_driver.usage = OpenCodeUsage("native", "message", 2, 1, 0, 0, 1.0, 1)
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    channel = ChannelConfig.model_validate(
        {
            "name": "chat",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "session": {
                "type": "custom",
                "key": "{{ payload.key }}",
                "maxTokens": 1,
                "overflow": "rotate",
            },
        }
    )
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(),
            max_invocation_seconds=10,
            channels_by_name={"chat": channel},
            channel_ttl={"chat": 60.0},
        )
        try:
            for event_id in ("rotate-one", "rotate-two"):
                result = await runner(
                    MessageEvent(
                        idempotency_key=event_id,
                        session_key="lane",
                        channel_name="chat",
                        payload={"key": "conversation"},
                    ),
                    lambda: None,
                )
                assert result == {"action": "none", "text": "reply"}
            assert fake_driver.resolved_conversations == [
                ("conversation", True),
                ("conversation", True),
            ]
            assert fake_driver.discarded_refs == ["native-1", "native-2"]
            assert fake_driver.resolved_refs == ["native-1", "native-2"]
            assert fake_driver.turn_session_refs == ["native-1", "native-2"]
        finally:
            await runner.close()
            await client.close()


@pytest.mark.asyncio
async def test_prepare_failure_is_acknowledged_and_client_stays_usable(
    monkeypatch: Any,
    tmp_path: Any,
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.boot.prepare import PrepareFailed
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    cleanup_calls: list[str] = []

    async def fake_cleanup(cfg: Any, event: MessageEvent, workspace: Any) -> None:
        cleanup_calls.append(event.idempotency_key)

    monkeypatch.setattr("ach_agent.boot.prepare.run_cleanup", fake_cleanup)
    private_channel = ChannelConfig.model_validate(
        {
            "name": "private",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {
                "script": "exit 17",
                "secretEnv": {"TOKEN": {"env": "TOKEN"}},
            },
            "cleanup": {
                "script": "true",
                "secretEnv": {"TOKEN": {"env": "TOKEN"}},
            },
        }
    )
    plain_channel = ChannelConfig.model_validate(
        {
            "name": "plain",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
        }
    )
    fake_driver = FakeDriver()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(
                home=str(tmp_path / "home"), work_dir=str(tmp_path / "workspace")
            ),
            max_invocation_seconds=10,
            channels_by_name={"private": private_channel, "plain": plain_channel},
        )
        try:
            with pytest.raises(PrepareFailed, match="exited 17"):
                await runner(
                    MessageEvent(
                        idempotency_key="private-failure",
                        session_key="private-lane",
                        channel_name="private",
                        payload={},
                    ),
                    lambda: None,
                )
            assert cleanup_calls == ["private-failure"]
            result = await runner(
                MessageEvent(
                    idempotency_key="after-private-failure",
                    session_key="plain-lane",
                    channel_name="plain",
                    payload={},
                ),
                lambda: None,
            )
            assert result == {"action": "none", "text": "reply"}
        finally:
            await runner.close()
            await client.close()


@pytest.mark.asyncio
async def test_public_workspace_hooks_do_not_accumulate_unconsumed_stop_events(
    tmp_path: Any,
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    channel = ChannelConfig.model_validate(
        {
            "name": "public-hooks",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
            "cleanup": {"script": "true"},
        }
    )
    fake_driver = FakeDriver()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(
                home=str(tmp_path / "home"), work_dir=str(tmp_path / "workspace")
            ),
            max_invocation_seconds=10,
            channels_by_name={"public-hooks": channel},
        )
        try:
            for index in range(65):
                result = await runner(
                    MessageEvent(
                        idempotency_key=f"public-{index}",
                        session_key=f"public-lane-{index}",
                        channel_name="public-hooks",
                        payload={},
                    ),
                    lambda: None,
                )
                assert result == {"action": "none", "text": "reply"}
            assert client._controller_events.empty()
            assert not service._unhealthy
        finally:
            await runner.close()
            await client.close()


@pytest.mark.asyncio
async def test_prepare_only_workspace_release_does_not_wait_for_cleanup_ack(
    tmp_path: Any,
) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.config.schema import ChannelConfig
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    channel = ChannelConfig.model_validate(
        {
            "name": "prepare-only",
            "type": "cron",
            "cron": {"schedule": "* * * * *"},
            "prepare": {"script": "true"},
        }
    )
    fake_driver = FakeDriver()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(
                home=str(tmp_path / "home"), work_dir=str(tmp_path / "workspace")
            ),
            max_invocation_seconds=5,
            channel_ttl={"prepare-only": 0},
            channels_by_name={"prepare-only": channel},
        )
        try:
            result = await runner(
                MessageEvent(
                    idempotency_key="prepare-only-release",
                    session_key="prepare-only-lane",
                    channel_name="prepare-only",
                    payload={},
                ),
                lambda: None,
            )
            assert result == {"action": "none", "text": "reply"}
            assert client._controller_events.empty()
            assert not service._unhealthy
        finally:
            await runner.close()
            await client.close()


@pytest.mark.asyncio
async def test_real_http_runner_cancel_keeps_client_usable_for_peer() -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    fake_driver = FakeDriver()
    fake_driver.turn_barrier = asyncio.Event()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(),
            max_invocation_seconds=10,
        )
        task = asyncio.create_task(
            runner(
                MessageEvent(
                    idempotency_key="cancelled",
                    session_key="cancelled-lane",
                    channel_name="chat",
                    payload={},
                ),
                lambda: None,
            )
        )
        try:
            for _ in range(100):
                if fake_driver.turn_session_refs:
                    break
                await asyncio.sleep(0.01)
            assert fake_driver.turn_session_refs
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert fake_driver.stopped
            assert not service._unhealthy
            fake_driver.turn_barrier = None
            peer = await runner(
                MessageEvent(
                    idempotency_key="peer-after-cancel",
                    session_key="peer-lane",
                    channel_name="chat",
                    payload={},
                ),
                lambda: None,
            )
            assert peer == {"action": "none", "text": "reply"}
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await runner.close()
            await client.close()


@pytest.mark.asyncio
async def test_repeated_real_http_cancellation_consumes_handle_and_cost_metadata() -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.engine.cost import CostAccountant
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from tests.execution.conftest import FakeDriver
    from tests.execution.test_http import _running_server

    fake_driver = FakeDriver()
    service = ExecutionService(fake_driver, {})
    service.controller_required = True
    accountant = CostAccountant("engine", "openai", None, "gpt-4o-mini")
    async with _running_server(create_execution_app(service)) as base_url:
        client = ExecutionClient(base_url, controller_id="controller", timeout=2)
        await client.connect()
        runner = make_engine_runner(
            client=client,
            engine_cfg=PublicEngineConfig(),
            max_invocation_seconds=10,
            accountant=accountant,
        )
        try:
            for index in range(3):
                fake_driver.turn_barrier = asyncio.Event()
                task = asyncio.create_task(
                    runner(
                        MessageEvent(
                            idempotency_key=f"cancel-{index}",
                            session_key=f"cancel-lane-{index}",
                            channel_name="chat",
                            payload={},
                        ),
                        lambda: None,
                    )
                )
                for _ in range(100):
                    if len(fake_driver.turn_session_refs) >= index + 1:
                        break
                    await asyncio.sleep(0.01)
                assert len(fake_driver.turn_session_refs) >= index + 1
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert not client._handles
                assert not client._confirmed_cancellations
                assert not accountant._buckets
            assert not service._unhealthy
        finally:
            await runner.close()
            await client.close()
