# SPDX-License-Identifier: Apache-2.0
import base64
import json
from datetime import UTC, datetime

from ach_runtime.__main__ import main
from ach_runtime.harness.run import token_expiry


def _jwt(claims: dict) -> str:
    part = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJub25lIn0.{part}.sig"


def test_token_expiry_reads_a_jwt_exp() -> None:
    assert token_expiry(_jwt({"exp": 1_800_000_000})) == datetime.fromtimestamp(1_800_000_000, tz=UTC)


def test_token_expiry_is_none_for_keys_and_garbage() -> None:
    assert token_expiry("ek-abc123") is None
    assert token_expiry("a.b.c") is None
    assert token_expiry(_jwt({"sub": "no-exp"})) is None


def test_inspect_prints_mode_profile_and_header_env_names(tmp_path, capsys) -> None:
    manifest = tmp_path / "agent.yaml"
    manifest.write_text(
        "ach: {profile: support, environment: e}\nmodel: {name: m, api: openai}\n"
        "mcpServers: [{name: gh, url: 'https://gh.example/mcp', headers: {Authorization: {env: GH_AUTH}}}]\n"
    )
    assert main(["inspect", "--config", str(manifest)]) == 0
    assert capsys.readouterr().out == "ach\nsupport\nGH_AUTH\n"
    manifest.write_text(
        "model: {name: m, api: anthropic, baseUrl: 'https://api.example/v1',"
        " headers: {x-api-key: {env: API_KEY}, x-tag: {value: lit}}}\n"
    )
    assert main(["inspect", "--config", str(manifest)]) == 0
    assert capsys.readouterr().out == "standalone\n\nAPI_KEY\n"


def test_inspect_rejects_an_invalid_manifest(tmp_path, capsys) -> None:
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("enviroment: typo\n")
    assert main(["inspect", "--config", str(manifest)]) == 2
    assert "enviroment" in capsys.readouterr().err
