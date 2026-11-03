# SPDX-License-Identifier: MIT
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


def test_profile_command_prints_the_manifest_profile(tmp_path, capsys) -> None:
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("ach: {profile: support, environment: e}\nmodel: {name: m, type: openai}\n")
    assert main(["profile", "--config", str(manifest)]) == 0
    assert capsys.readouterr().out == "support\n"
    manifest.write_text("ach: {environment: e}\nmodel: {name: m, type: openai}\n")
    assert main(["profile", "--config", str(manifest)]) == 0
    assert capsys.readouterr().out == "\n"


def test_profile_command_rejects_an_invalid_manifest(tmp_path, capsys) -> None:
    manifest = tmp_path / "agent.yaml"
    manifest.write_text("enviroment: typo\n")
    assert main(["profile", "--config", str(manifest)]) == 2
    assert "enviroment" in capsys.readouterr().err
