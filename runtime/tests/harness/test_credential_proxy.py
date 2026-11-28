# SPDX-License-Identifier: Apache-2.0
import httpx

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.credential_proxy import create_proxy_app
from ach_runtime.harness.keys import KeySource


async def test_proxy_swaps_bearer_for_ach_key_and_refuses_others() -> None:
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    app = create_proxy_app("https://ach.example/", KeySource("ek_test"), "bearer-1",
                           ProcessIdentity(agent="hermes", environment="search"),
                           httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        ok = await client.post("/v1/chat/completions", json={"m": 1},
                               headers={"Authorization": "Bearer bearer-1", "x-ach-key": "forged"})
        mcp = await client.get("/mcp/mcp-serper", headers={"Authorization": "Bearer bearer-1"})
        wrong = await client.post("/v1/chat/completions", headers={"Authorization": "Bearer nope"})
        other = await client.get("/platform/hydrate", headers={"Authorization": "Bearer bearer-1"})

    assert (ok.status_code, mcp.status_code, wrong.status_code, other.status_code) == (200, 200, 401, 404)
    assert [str(r.url) for r in seen] == ["https://ach.example/v1/chat/completions", "https://ach.example/mcp/mcp-serper"]
    sent = seen[0].headers
    assert sent["x-ach-key"] == "ek_test" and "authorization" not in sent
    assert (sent["x-ach-agent"], sent["x-ach-environment"]) == ("hermes", "search")


def _tar_gz(files: dict[str, str]) -> bytes:
    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


async def test_bundle_hydrates_config_skills_and_instructions_from_the_environment() -> None:
    import io
    import json
    import tarfile

    manifest = {
        "environment": "agent-hermes",
        "runtime": {"models": [{"id": "ackstorm.smart", "endpoint": "https://ach.example/v1"}],
                    "mcpServers": [{"id": "mcp-serper", "endpoint": "https://ach.example/mcp/mcp-serper"},
                                   {"id": "elsewhere", "endpoint": "https://other.example/mcp/x"}],
                    "a2aAgents": [{"id": "aws-devops", "endpoint": "https://ach.example/a2a/aws-devops"}]},
        "context": {"skills": [{"name": "s1", "downloadUrl": "https://ach.example/dl/s1"}],
                    "instructions": [{"name": "rules", "downloadUrl": "https://ach.example/dl/rules"}]},
    }

    def ach(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-ach-key"] == "ek_test"
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json=manifest)
        if request.url.path == "/v2/model/info":
            return httpx.Response(200, json={"data": [{"model_name": "ackstorm.smart", "model_info": {"max_input_tokens": 400000}}]})
        if request.url.path == "/dl/s1":
            return httpx.Response(200, content=_tar_gz({"SKILL.md": "---\nname: s1\n---\nhi"}))
        if request.url.path == "/dl/rules":
            return httpx.Response(200, content=_tar_gz({"RULES.md": "Answer in Spanish."}))
        raise AssertionError(request.url.path)

    keys = KeySource("ek_test")
    app = create_proxy_app("https://ach.example", keys, "b", ProcessIdentity(agent="hermes", environment="agent-hermes"),
                           httpx.AsyncClient(transport=httpx.MockTransport(ach)), model="ackstorm.smart",
                           user_config={"model": {"default": "ignored", "max_tokens": 8192},
                                        "mcp_servers": {"mine": {"url": "https://mine.example/mcp"}}},
                           ach_client=httpx.AsyncClient(transport=httpx.MockTransport(ach), auth=keys.auth))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://proxy") as client:
        denied = await client.get("/hermes/bundle")
        response = await client.get("/hermes/bundle", headers={"Authorization": "Bearer b"})

    assert denied.status_code == 401 and response.status_code == 200
    tar = tarfile.open(fileobj=io.BytesIO(response.content))
    config = json.loads(tar.extractfile("config.yaml").read())
    assert config["model"] == {"default": "ackstorm.smart", "max_tokens": 8192, "provider": "custom",
                               "base_url": "${ACH_PROXY_URL}/v1", "api_key": "${ACH_PROXY_TOKEN}", "context_length": 400000}
    assert config["mcp_servers"] == {
        "mine": {"url": "https://mine.example/mcp"},
        "mcp-serper": {"url": "${ACH_PROXY_URL}/mcp/mcp-serper", "headers": {"Authorization": "Bearer ${ACH_PROXY_TOKEN}"}},
        "aws-devops": {"url": "${ACH_PROXY_URL}/a2a/aws-devops/mcp", "headers": {"Authorization": "Bearer ${ACH_PROXY_TOKEN}"}},
    }
    assert config["skills"]["external_dirs"] == ["${HERMES_HOME}/ach-skills"]
    assert "agent" not in config
    assert tar.extractfile(".ach/instructions.md").read() == b"## rules\n\nAnswer in Spanish."
    assert b"def merge_soul" in tar.extractfile(".ach/start.py").read()
    assert tar.extractfile("ach-skills/s1/SKILL.md").read().startswith(b"---\nname: s1")


def test_merge_soul_keeps_the_rest_and_rewrites_only_the_ach_block() -> None:
    from ach_runtime.harness.hermes_start import BEGIN, END, merge_soul

    first = merge_soul("You are Hermes.\n", "## rules\n\nAnswer in Spanish.")
    assert first == f"You are Hermes.\n\n{BEGIN}\n## rules\n\nAnswer in Spanish.\n{END}\n"
    edited = first + "\nA line the user added.\n"
    second = merge_soul(edited, "## tone\n\nBe brief.")
    assert second == f"You are Hermes.\n\nA line the user added.\n\n{BEGIN}\n## tone\n\nBe brief.\n{END}\n"
    assert merge_soul(second, "") == "You are Hermes.\n\nA line the user added.\n"


def test_start_script_seeds_hermes_default_only_when_instructions_exist(tmp_path, monkeypatch) -> None:
    from ach_runtime.harness import hermes_start

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_start, "_hermes_default_soul", lambda: "You are Hermes Agent.")
    hermes_start.main()
    assert not (tmp_path / "SOUL.md").exists()
    (tmp_path / ".ach").mkdir()
    (tmp_path / ".ach" / "instructions.md").write_text("## rules\n\nX")
    hermes_start.main()
    soul = (tmp_path / "SOUL.md").read_text()
    assert soul.startswith("You are Hermes Agent.\n\n<!-- ach:instructions") and "## rules\n\nX" in soul
