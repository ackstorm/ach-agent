# SPDX-License-Identifier: MIT
from __future__ import annotations

import json
import logging
from pathlib import Path

import httpx
import pytest

from ach_runtime.config.canonical import compute_config_version
from ach_runtime.config.schema import parse_config
from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.tokens import engine_verify_key
from ach_runtime.observability import RunRecord, RunUsage, alarm, record_run
from ach_runtime.observability.runs import configure_logging


def test_usage_record_and_alarm_are_finite_safe_and_unpriced(caplog) -> None:
    usage = RunUsage("opencode", "engine_estimate", 0, 1.5, 2, 0, 0, 0.25, 1)
    assert usage.to_dict() == {
        "source": "opencode", "basis": "engine_estimate", "input": 0,
        "output": 1.5, "reasoning": 2, "cache_read": 0, "cache_write": 0,
        "cost_usd": 0.25, "steps": 1,
    }
    record = RunRecord("agent", "dev", "inv-opaque", "run-opaque", "actor", "evt",
                       "webhook", "ws-opaque", "sess-opaque", "version", "model",
                       12.5, "completed", usage)
    with caplog.at_level(logging.INFO):
        record_run(record)
    record_log = caplog.records[-1]
    assert record_log.invocation_id == "inv-opaque"
    assert record_log.usage_basis == "engine_estimate"
    assert not hasattr(record_log, "prompt") and not hasattr(record_log, "error")

    with caplog.at_level(logging.ERROR):
        alarm("usage_invalid", invocation_id="inv-opaque", workspace_ref="ws-opaque")
    assert caplog.records[-1].kind == "usage_invalid"
    assert "prompt-secret" not in caplog.text


def test_completion_log_redacts_legacy_bearer_and_provider_tokens(caplog) -> None:
    record = RunRecord(
        "agent", "dev", "inv-opaque", "run-opaque", "ek_review-secret", "sk-provider-secret",
        "webhook", "ws-opaque", "sess-opaque", "version", "model", 12.5, "completed", None,
    )
    with caplog.at_level(logging.INFO):
        record_run(record)

    record_log = caplog.records[-1]
    assert record.actor == "ek_review-secret"
    assert record.event_id == "sk-provider-secret"
    assert record_log.actor == "[redacted]"
    assert record_log.event_id == "[redacted]"
    assert "ek_review-secret" not in caplog.text
    assert "sk-provider-secret" not in caplog.text


def _catalog_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/platform/hydrate":
        return httpx.Response(200, json={
            "environment": "engineering-prod",
            "runtime": {"models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://model.example/v1"}],
                        "mcpServers": [], "a2aAgents": []},
            "context": {"skills": [], "prompts": [], "artifacts": []},
        })
    if request.url.path == "/v2/model/info":
        return httpx.Response(200, json={"data": []})
    raise AssertionError(request.url)


class _FakeServer:
    """A no-bind stand-in for `uvicorn.Server`: no socket, but a real `uvicorn.Config` is
    still built by the caller and its own `configure_logging()` runs for real here, to
    prove uvicorn's own logging setup never removes `ach_runtime.observability.runs`'s
    owned handler. Emits one completion and one anomaly record immediately, standing in
    for whatever happens once the role has actually booted."""

    def __init__(self, config) -> None:
        self.config = config
        self.started = False
        self.should_exit = False

    async def serve(self) -> None:
        self.config.configure_logging()
        self.started = True
        record_run(RunRecord(
            "gitlab-reviewer", "dev", "inv-boot", "run-boot", "actor-boot", "evt-boot",
            "webhook", "a" * 64, "b" * 64, "cfg-version", "gemini.gemini-flash-latest",
            5.0, "completed",
            RunUsage("opencode", "engine_estimate", 1, 2, 0, 0, 0, 0.01, 1),
        ))
        alarm("pod_unhealthy", invocation_id="inv-boot", workspace_ref="a" * 64)


@pytest.mark.parametrize("role", ["control", "execution"])
async def test_both_role_boots_emit_safe_info_and_anomaly_stream(
    role, tmp_path: Path, monkeypatch, capsys,
) -> None:
    """Policy gap: both role boots configure logging before config loading/assembly, so
    an owned stderr JSON handler is installed; boot's real (faked-socket) uvicorn.Config
    then runs its own configure_logging() without removing it. The actual stderr stream
    carries a safe INFO completion and an ERROR anomaly -- never a secret, a raw request
    body, an auth header, or an authenticated facade URL."""
    import uvicorn

    monkeypatch.setattr(uvicorn, "Server", _FakeServer)
    wire = json.loads((Path(__file__).parents[1] / "config/fixtures/pr-review-runtime.json").read_text())
    wire["workspace"]["persistence"]["enabled"] = False
    wire["workspace"]["session"]["persistence"]["enabled"] = False
    wire["artifacts"]["enabled"] = False
    wire["channels"][0].pop("handoff", None)
    wire["configVersion"] = compute_config_version(wire)
    config = parse_config(wire)

    if role == "control":
        from ach_runtime.harness.boot import run_control
        from ach_runtime.harness.control import assemble

        async def rigged_assembly(cfg):
            control = await assemble(
                cfg, environ={"ACH_SANDBOX_KEY": "secret", "ACH_SECRET_IDENTITY": "ek"},
                ach_transport=httpx.MockTransport(_catalog_handler),
                mini_transport=httpx.MockTransport(_catalog_handler),
            )
            return control

        config_path = tmp_path / "control.json"
        config_path.write_text(json.dumps(wire), encoding="utf-8")
        with pytest.raises(RuntimeError, match="stopped"):
            await run_control(config_path, assembly=rigged_assembly)
    else:
        from ach_runtime.execution.boot import run_execution

        bootstrap = {
            "bootstrapVersion": "workspace-v2",
            "agent": {"name": config.agent.name, "namespace": config.agent.namespace,
                      "uid": config.agent.uid},
            "facadeEndpoint": "http://ach-control.ach.svc:8081/facades",
        }
        config_path = tmp_path / "bootstrap.json"
        config_path.write_text(json.dumps(bootstrap), encoding="utf-8")
        monkeypatch.setenv("ACH_SANDBOX_VERIFY_KEY", engine_verify_key(b"k" * 64))
        monkeypatch.setenv("ACH_WORKSPACE_NAME", "ach-workspace")
        monkeypatch.setenv("ACH_WORKSPACE_REF", "a" * 64)
        await run_execution(
            config_path, workspace_dir=tmp_path / "workspace", sessions_root=tmp_path / "sessions",
        )

    captured = capsys.readouterr()
    lines = [json.loads(line) for line in captured.err.splitlines() if line.strip()]
    info_lines = [line for line in lines if line.get("level") == "INFO"]
    error_lines = [line for line in lines if line.get("level") == "ERROR"]
    assert info_lines, "expected at least one real stderr INFO completion record"
    assert error_lines, "expected at least one real stderr ERROR anomaly record"
    info = info_lines[-1]
    assert info["invocation_id"] == "inv-boot"
    assert info["run_id"] == "run-boot"
    assert info["workspace_ref"] == "a" * 64
    assert info["session_ref"] == "b" * 64
    assert info["config_version"] == "cfg-version"
    assert info["outcome"] == "completed"
    assert info["duration_ms"] == 5.0
    assert info["usage_source"] == "opencode"
    assert info["usage_basis"] == "engine_estimate"
    error = error_lines[-1]
    assert error["kind"] == "pod_unhealthy"
    assert error["workspace_ref"] == "a" * 64
    for line in lines:
        assert "ek" not in line and "sk" not in line
        assert "raw_body" not in line and "request_body" not in line
        assert "authorization" not in line
        assert not any(
            isinstance(value, str) and "facades" in value for value in line.values()
        )
    full_output = captured.out + captured.err
    assert "Bearer " not in full_output

    # Repeated configure never installs a second owned handler.
    logger = logging.getLogger("ach_runtime.observability.runs")
    owned_before = sum(1 for h in logger.handlers if getattr(h, "_ach_owned_handler", False))
    configure_logging()
    configure_logging()
    owned_after = sum(1 for h in logger.handlers if getattr(h, "_ach_owned_handler", False))
    assert owned_before == owned_after == 1


def test_skipped_run_counts_only_in_runs_total() -> None:
    from ach_runtime.observability.metrics import COST_UNPRICED, RUNS_TOTAL, TURNS_TOTAL

    runs = RUNS_TOTAL.labels(channel="tick", outcome="skipped")
    turns = TURNS_TOTAL.labels(model="model", channel="tick")
    unpriced = COST_UNPRICED.labels(reason="usage_missing")
    before = (runs._value.get(), turns._value.get(), unpriced._value.get())
    record_run(RunRecord("agent", "dev", "inv", "run", None, "evt", "tick", "ws", "sess",
                         "version", "model", 3.0, "skipped", None))
    after = (runs._value.get(), turns._value.get(), unpriced._value.get())
    assert after == (before[0] + 1, before[1], before[2])


def test_completed_run_counts_in_runs_total_and_turns() -> None:
    from ach_runtime.observability.metrics import RUNS_TOTAL, TURNS_TOTAL

    runs = RUNS_TOTAL.labels(channel="tick", outcome="completed")
    turns = TURNS_TOTAL.labels(model="model", channel="tick")
    before = (runs._value.get(), turns._value.get())
    record_run(RunRecord("agent", "dev", "inv", "run", None, "evt", "tick", "ws", "sess",
                         "version", "model", 3.0, "completed", None))
    assert (runs._value.get(), turns._value.get()) == (before[0] + 1, before[1] + 1)
