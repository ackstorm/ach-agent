"""End-to-end round-trip test: real opencode binary + mock model server.

CLOSES ASSUMPTION A1: confirms whether {"actions":[...]} arrives as plain accumulated
message.part.updated text (RESEARCH expectation) or as a structured/typed SSE event.

CONFIRMED FINDINGS (from investigative runs, recorded here for the phase):
  A1: CONFIRMED — {"actions":[...]} arrives as plain text in the LAST message.part.updated
      event (part.type="text"). The text accumulation approach in consume_sse_after_send
      correctly captures it.
  UNEXPECTED: opencode v1.16.0 uses the OpenAI Responses API (POST /v1/responses), NOT
      Chat Completions (POST /v1/chat/completions). The mock must implement /v1/responses.
  UNEXPECTED: opencode validates model names against its internal registry; must use a
      recognized model name like "gpt-4o-mini" (not "mock-gpt-4o-mini").
  UNEXPECTED: OPENCODE_SERVER_PASSWORD, when set, blocks ALL routes including GET /app,
      causing poll_ready() to fail with 401. Do NOT set it.

ENVIRONMENT REQUIREMENTS:
  - /usr/bin/opencode v1.16.0 must be installed and executable
  - fastapi + uvicorn must be installed (in dev dependency-group)
  - opencode is pointed at the in-process mock server via config.model_base_url (the same
    path the localhost model-proxy uses in prod); no real key or ACH endpoint is needed

If opencode is unavailable, the test is skipped automatically.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import threading
import time
from pathlib import Path

import pytest

# Skip entire module if opencode binary is not available
pytestmark = pytest.mark.skipif(
    not Path("/usr/bin/opencode").exists(),
    reason="opencode binary not found at /usr/bin/opencode — round-trip requires real binary",
)

OPENCODE_BINARY = "/usr/bin/opencode"


# ---------------------------------------------------------------------------
# In-process mock model server fixture
# ---------------------------------------------------------------------------


def _get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture()
def mock_model_server():
    """Start the mock model server (Responses API) in a background thread using uvicorn.

    Yields (host, port) tuple. The server is running for the duration of the test.
    """
    import uvicorn

    repo_root = Path(__file__).parent.parent.parent
    mock_model_dir = repo_root / "docker" / "mock-model"

    port = _get_free_port()
    host = "127.0.0.1"

    # Import the mock app
    sys.path.insert(0, str(mock_model_dir))
    try:
        import importlib
        import app as mock_app_module
        importlib.reload(mock_app_module)
        mock_app = mock_app_module.app
    finally:
        sys.path.pop(0)

    config = uvicorn.Config(
        app=mock_app,
        host=host,
        port=port,
        log_level="warning",
    )
    server = uvicorn.Server(config)

    def run_server():
        server.run()

    thread = threading.Thread(target=run_server, daemon=True)
    thread.start()

    # Wait for server to be ready
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.5)
                s.connect((host, port))
            break
        except (ConnectionRefusedError, OSError):
            time.sleep(0.1)
    else:
        pytest.fail(f"Mock model server did not start within 10s on port {port}")

    yield host, port

    server.should_exit = True
    thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Round-trip test
# ---------------------------------------------------------------------------


async def test_roundtrip_launch_to_actions(mock_model_server, tmp_path):
    """Full launch → ready → session → SSE → actions round-trip.

    Empirically closes A1: confirms {"actions":[...]} arrives as plain text
    in message.part.updated events (type=text), matching the RESEARCH hypothesis.

    Environmental findings (recorded in 00-RESEARCH.md):
      - opencode v1.16.0 uses POST /v1/responses (Responses API), not Chat Completions
      - Model name must be a recognized opencode model ID (e.g. "gpt-4o-mini")
      - OPENCODE_SERVER_PASSWORD must NOT be set (blocks GET /app with 401)
      - The mock implements the full Responses API SSE event sequence
    """
    from ach_agent.engine.client import OpenCodeClient, find_free_port
    from ach_agent.engine.lifecycle import (
        EngineConfig,
        ManagedServer,
        launch,
        poll_ready,
    )

    mock_host, mock_port = mock_model_server
    mock_base_url = f"http://{mock_host}:{mock_port}/v1"

    # Point opencode straight at the mock model server via model_base_url (the same path the
    # localhost model-proxy uses in prod). apiKey is a dummy "local-proxy"; the mock ignores
    # auth, so no real key is needed or written.
    config = EngineConfig(
        binary_path=OPENCODE_BINARY,
        work_dir=str(tmp_path / "workspace"),
        # Must use a model name known to opencode's internal registry
        model="gpt-4o-mini",
        system_prompt="You are a test assistant. Reply with JSON only.",
        steps=5,
        startup_timeout_seconds=30,
        model_base_url=mock_base_url,
    )

    (tmp_path / "workspace").mkdir(parents=True, exist_ok=True)
    ephemeral_home = tmp_path / "oc-home"
    ephemeral_home.mkdir(parents=True, exist_ok=True)

    oc_port = find_free_port()
    server: ManagedServer | None = None

    try:
        # Phase 1: launch
        server = await launch(oc_port, ephemeral_home, config, session_key="k1")
        assert server.is_alive(), "opencode subprocess must be running after launch"

        # Phase 2: poll_ready (GET /app returns 200 when ready)
        await poll_ready(server, startup_timeout_seconds=30)

        # Phase 3: create session
        client = server._client
        assert isinstance(client, OpenCodeClient)

        session_data = await client.create_session()
        assert "id" in session_data, f"session response missing 'id': {session_data}"
        session_id = session_data["id"]
        assert session_id.startswith("ses_"), f"unexpected session id format: {session_id!r}"

        # Phase 4: subscribe SSE FIRST, then send message + consume to session.idle
        # Critical ordering: subscribe BEFORE send_message so we don't miss session.idle
        from ach_agent.engine.lifecycle import consume_sse_after_send
        accumulated_text = await consume_sse_after_send(
            client, session_id, "Please reply with the actions JSON."
        )

        # Phase 5: A1 empirical check — extract {"actions":[...]} from accumulated text
        assert accumulated_text, "Accumulated text must be non-empty"

        actions = _extract_actions(accumulated_text)

        # A1 CONFIRMED: print result for CI log / human verification
        print(f"\n[A1] Accumulated text: {accumulated_text[:500]!r}")
        print(f"[A1] Extracted actions: {json.dumps(actions, indent=2) if actions else 'None'}")

        assert actions is not None, (
            f"Could not extract {{\"actions\":[...]}} from accumulated text. "
            f"A1 is not confirmed. Accumulated text: {accumulated_text[:500]!r}"
        )
        assert isinstance(actions, list), "actions must be a list"
        assert len(actions) > 0, "actions list must be non-empty"

        action = actions[0]
        assert action.get("name") == "channel_message", f"unexpected action name: {action}"
        assert action.get("kind") == "reply", f"unexpected action kind: {action}"
        assert action.get("input", {}).get("text") == "Mock reply from ach-agent!", (
            f"unexpected action text: {action}"
        )

        print(f"\n[PASS] A1 CONFIRMED: actions extracted from plain SSE text: {actions}")

    finally:
        if server is not None:
            await server.stop()


# ---------------------------------------------------------------------------
# Action extraction helpers (duplicated from validator.py sketch — 00-03 owns the real impl)
# ---------------------------------------------------------------------------


_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _find_matching_brace(text: str, start: int) -> int:
    """Find the closing brace position for the { at text[start]."""
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_string:
            escape = True
            continue
        if c == '"' and not escape:
            in_string = not in_string
        if not in_string:
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return i
    return -1


def _extract_actions(accumulated_text: str) -> list[dict] | None:
    """Extract {"actions":[...]} from accumulated SSE text deltas."""
    text = accumulated_text
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        text = fence_match.group(1).strip()
    marker = '{"actions"'
    pos = text.rfind(marker)
    if pos == -1:
        return None
    end = _find_matching_brace(text, pos)
    if end == -1:
        return None
    try:
        data = json.loads(text[pos: end + 1])
        return data.get("actions")  # type: ignore[return-value]
    except json.JSONDecodeError:
        return None
