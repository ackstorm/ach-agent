# SPDX-License-Identifier: Apache-2.0
"""Task 8A role-boundary and engine bootstrap tests."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from ach_agent.config.schema import AgentConfig, ChannelSourceConfig
from ach_agent.execution.wire import PublicEngineConfig


def _cfg(**updates: object) -> AgentConfig:
    raw: dict[str, object] = {
        "schemaVersion": "1",
        "agent": {"name": "agent-a"},
        "model": {"name": "openai.gpt-5", "type": "openai"},
        "capability": {"ach": {"baseUrl": "https://ach.example.test"}},
        "channels": [
            {
                "name": "incoming",
                "type": "webhook-script",
                "source": "gitlab",
                "webhook": {
                    "auth": {"type": "hmac", "secret": {"env": "GITLAB_HMAC"}},
                    "gitlabEvents": ["push"],
                },
                "script": {"script": "echo private"},
            },
            {
                "name": "review",
                "type": "webhook",
                "source": "gitlab",
                "webhook": {"auth": {"type": "none"}},
                "prompt": "Review this",
                "prepare": {"script": "echo prepare"},
            },
        ],
    }
    raw.update(updates)
    return AgentConfig.model_validate(raw)


def test_source_projection_drops_execution_fields_but_accepts_webhook_script() -> None:
    cfg = _cfg()

    from ach_agent.boot.roles import build_role_configs

    channels, public = build_role_configs(cfg)
    assert set(channels) == {"schemaVersion", "channels"}
    source = channels["channels"][0]
    assert source["type"] == "webhook-script"
    assert "script" not in source
    assert "prompt" not in source
    assert "prepare" not in source
    assert "session" not in source
    ChannelSourceConfig.model_validate(source)
    assert public["agentName"] == "agent-a"


def test_split_role_projection_rejects_forward_env() -> None:
    cfg = _cfg(engine={"forwardEnv": ["SAFE_NATIVE_VAR"]})

    from ach_agent.boot.roles import build_role_configs

    with pytest.raises(ValueError, match="forwardEnv"):
        build_role_configs(cfg)


def test_local_projection_keeps_only_sanitized_forward_env_names() -> None:
    cfg = _cfg(engine={"forwardEnv": ["SAFE_NATIVE_VAR"]})

    from ach_agent.boot.roles import build_role_configs

    _channels, public = build_role_configs(cfg, split_mode=False)
    assert public["engineEnvNames"] == ["SAFE_NATIVE_VAR"]


def test_public_bootstrap_has_no_managed_credentials_or_full_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = _cfg(
        memory={"type": "codemem", "codemem": {"dbPath": "/var/lib/ach/state.db"}},
        persistence={"enabled": True, "mountPath": "/var/lib/ach"},
    )

    from ach_agent.boot.roles import build_role_configs

    monkeypatch.setattr("shutil.which", lambda _name: (_ for _ in ()).throw(AssertionError()))
    _channels, public = build_role_configs(cfg)
    assert "capability" not in public
    assert "channels" not in public
    assert "memory" not in public
    assert public["codemem_db_path"] == "/var/lib/ach/state.db"
    assert public["persistenceEnabled"] is True
    PublicEngineConfig.model_validate(public)


def test_default_codemem_path_preserves_existing_layout() -> None:
    from ach_agent.boot.roles import build_role_configs

    _channels, public = build_role_configs(_cfg(memory={"type": "codemem", "codemem": {}}))
    assert public["codemem_db_path"] == "/tmp/ach-home/state/codemem.db"


def test_public_context_paths_are_separate_from_engine_home(tmp_path: Path) -> None:
    cfg = _cfg(persistence={"enabled": True, "mountPath": str(tmp_path)})

    from ach_agent.boot.paths import resolve_role_paths

    paths = resolve_role_paths(cfg)
    assert paths.engine_home != paths.public_context
    assert paths.harness_scratch != paths.engine_home
    assert paths.public_context.parent == tmp_path


def test_engine_context_links_public_state_without_replacing_private_home(tmp_path: Path) -> None:
    from ach_agent.engine.context import link_public_context

    home = tmp_path / "engine-home"
    public = tmp_path / "public"
    (home / "native").mkdir(parents=True)
    link_public_context(home, public)
    assert (home / "native").is_dir()
    assert (home / ".ach-state").is_symlink()
    assert (home / ".ach-state").resolve() == public.resolve()
    assert (home / ".config" / "opencode" / "skills").resolve() == (public / "skills").resolve()


def test_engine_context_migrates_existing_managed_dirs_and_keeps_native_files(
    tmp_path: Path,
) -> None:
    from ach_agent.engine.context import link_public_context

    home = tmp_path / "engine-home"
    public = tmp_path / "public"
    (home / ".ach-state" / "prompts").mkdir(parents=True)
    (home / ".ach-state" / "prompts" / "old.txt").write_text("old")
    (home / ".config" / "opencode" / "skills" / "old-skill").mkdir(parents=True)
    (home / ".config" / "opencode" / "skills" / "old-skill" / "SKILL.md").write_text("old")
    (home / ".local" / "share" / "opencode").mkdir(parents=True)
    (home / ".local" / "share" / "opencode" / "session.db").write_text("native")

    link_public_context(home, public)

    assert (home / ".ach-state").is_symlink()
    assert (home / ".ach-state.pre-split" / "prompts" / "old.txt").read_text() == "old"
    assert (home / ".config" / "opencode" / "skills").is_symlink()
    assert (home / ".config" / "opencode" / "skills.pre-split" / "old-skill" / "SKILL.md").is_file()
    assert (home / ".local" / "share" / "opencode" / "session.db").read_text() == "native"


def test_engine_context_accepts_read_only_public_target(tmp_path: Path) -> None:
    from ach_agent.engine.context import link_public_context

    public = tmp_path / "public"
    (public / "skills").mkdir(parents=True)
    public.chmod(0o555)
    (public / "skills").chmod(0o555)
    try:
        link_public_context(tmp_path / "home", public, create_public=False)
    finally:
        (public / "skills").chmod(0o755)
        public.chmod(0o755)
    assert (tmp_path / "home" / ".ach-state").is_symlink()


@pytest.mark.asyncio
async def test_session_import_is_controller_owned_and_startup_only(tmp_path: Path) -> None:
    from ach_agent.engine.opencode.driver import OpencodeDriver
    from ach_agent.execution.service import ExecutionService
    from ach_agent.execution.state import NativeSessionStore
    from ach_agent.execution.wire import SessionImportRequest

    store = NativeSessionStore(tmp_path / "home")
    service = ExecutionService(OpencodeDriver(), store)
    await service.claim_controller("controller")
    request = SessionImportRequest(
        controller_id="controller",
        rows=[{"key": "opencode:review", "ocSessionId": "ses-old", "lastUsed": 1.0}],
    )
    assert await service.import_legacy_sessions(request) == 1
    assert store.get("opencode:review") == "ses-old"
    assert await service.import_legacy_sessions(request) == 0
    service._acquiring.add("already-started")
    with pytest.raises(ValueError, match="startup"):
        await service.import_legacy_sessions(request)
    store.close()


@pytest.mark.asyncio
async def test_session_import_http_accepts_rows_only(tmp_path: Path) -> None:
    from ach_agent.boot.execution_client import ExecutionClient
    from ach_agent.engine.opencode.driver import OpencodeDriver
    from ach_agent.execution.app import create_execution_app
    from ach_agent.execution.service import ExecutionService
    from ach_agent.execution.state import NativeSessionStore

    store = NativeSessionStore(tmp_path / "home")
    service = ExecutionService(OpencodeDriver(), store)
    app = create_execution_app(service)
    await service.claim_controller("controller")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://engine") as client:
        response = await client.post(
            "/execution/v1/session-import",
            json={
                "controller_id": "controller",
                "rows": [{"key": "pi:review", "ocSessionId": "session-file", "lastUsed": 2.0}],
            },
        )
        assert response.status_code == 200
        assert response.json()["imported"] == 1
        forbidden = await client.post(
            "/execution/v1/session-import",
            json={"controller_id": "controller", "legacyDbPath": "/private/state.db"},
        )
        assert forbidden.status_code == 422
    execution_client = ExecutionClient(
        "http://engine", controller_id="controller", transport=transport
    )
    assert await execution_client.import_legacy_sessions(
        [{"key": "opencode:review", "ocSessionId": "new", "lastUsed": 3.0}]
    ) == 0
    await execution_client.close()
    store.close()
