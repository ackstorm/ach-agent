# SPDX-License-Identifier: MIT
"""harness/hydrate.py — POST /platform/hydrate + GET /v2/model/info (contract §5).

Real httpx request/response plumbing exercised via httpx.MockTransport (header
construction, URL encoding, status handling) rather than mocked internals — only the
TCP socket is stubbed out, per task-7-reuse-handoff.md's "local fixtures" instruction and
task-7-model-metadata-ruling-evidence.md's unresolved live-metadata-availability gap.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import httpx
import pytest

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.harness.hydrate import (
    HydrationInput,
    Hydrator,
    HydrationError,
    fetch_hydration_manifest,
    fetch_model_metadata,
    resolve_model,
)

BASE_URL = "https://ach.example.com"
IDENTITY = ProcessIdentity(agent="classifier", environment="platform")

MANIFEST = {
    "environment": "platform",
    "runtime": {
        "models": [{"id": "gemini.gemini-flash-latest", "endpoint": "https://ach.example.com/v1"}],
        "mcpServers": [
            {"id": "mcp-google-calendar-ro", "endpoint": "https://ach.example.com/mcp/mcp-google-calendar-ro"}
        ],
        "a2aAgents": [],
    },
    "context": {
        "prompts": [],
        "artifacts": [],
        "skills": [
            {
                "name": "frontend-design@anthropics-skills",
                "id": "frontend-design@anthropics-skills",
                "downloadUrl": "https://ach.example.com/content/skill/frontend-design@anthropics-skills",
            }
        ],
    },
}

PRICED_MODEL_INFO = {
    "max_input_tokens": 200000,
    "max_output_tokens": 8192,
    "supports_vision": True,
    "supports_pdf_input": True,
    "supports_audio_input": False,
    "supports_video_input": False,
    "mode": "chat",
    "supports_function_calling": True,
    "supports_reasoning": False,
    "input_cost_per_token": 1e-6,
    "output_cost_per_token": 2e-6,
    "cache_read_input_token_cost": 1e-7,
}


def _transport(handler: Any) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _hydrate_handler(
    *, manifest: dict[str, Any] = MANIFEST, status: int = 200
) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/platform/hydrate"
        return httpx.Response(status, json=manifest)

    return handler


# --- fetch_hydration_manifest -------------------------------------------------


async def test_fetch_hydration_manifest_sends_identity_and_ek_never_authorization() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = request.read()
        return httpx.Response(200, json=MANIFEST)

    client = httpx.AsyncClient(transport=_transport(handler))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    assert manifest.environment == "platform"
    assert manifest.models == ["gemini.gemini-flash-latest"]
    import json as _json

    assert _json.loads(seen["body"]) == {"environment": "platform"}
    headers = seen["headers"]
    assert headers["x-ach-key"] == "ek-abc"
    assert headers["x-ach-agent"] == "classifier"
    assert headers["x-ach-environment"] == "platform"
    assert not any("authorization" == k.lower() for k in headers)


async def test_fetch_hydration_manifest_preserves_model_endpoint_and_skills() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler()))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    assert manifest.runtime.models[0].endpoint == "https://ach.example.com/v1"
    assert manifest.runtime.mcp_servers[0].id == "mcp-google-calendar-ro"
    assert manifest.context.skills[0].download_url.endswith("/skill/frontend-design@anthropics-skills")


async def test_fetch_hydration_manifest_rejects_environment_mismatch() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler()))
    with pytest.raises(HydrationError, match="environment mismatch"):
        await fetch_hydration_manifest(
            BASE_URL, "ek-abc", "requested-other", identity=IDENTITY, client=client
        )
    await client.aclose()


async def test_fetch_hydration_manifest_accepts_unset_environment() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler()))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "", identity=IDENTITY, client=client
    )
    await client.aclose()

    assert manifest.environment == "platform"


async def test_fetch_hydration_manifest_fails_closed_on_missing_environment_field() -> None:
    bad = {key: value for key, value in MANIFEST.items() if key != "environment"}
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler(manifest=bad)))
    with pytest.raises(HydrationError):
        await fetch_hydration_manifest(
            BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
        )
    await client.aclose()


async def test_fetch_hydration_manifest_fails_closed_on_invalid_shape() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler(manifest={"environment": "platform", "runtime": "not-an-object"})))
    with pytest.raises(HydrationError):
        await fetch_hydration_manifest(
            BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
        )
    await client.aclose()


async def test_fetch_hydration_manifest_fails_closed_on_http_error() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler(status=500)))
    with pytest.raises(HydrationError):
        await fetch_hydration_manifest(
            BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
        )
    await client.aclose()


async def test_fetch_hydration_manifest_fails_closed_on_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json", headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=_transport(handler))
    with pytest.raises(HydrationError):
        await fetch_hydration_manifest(
            BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
        )
    await client.aclose()


# --- resolve_model -------------------------------------------------------------


async def test_resolve_model_returns_entry_when_granted() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler()))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    entry = resolve_model(manifest, "gemini.gemini-flash-latest")
    assert entry.endpoint == "https://ach.example.com/v1"


async def test_resolve_model_fails_closed_when_not_granted() -> None:
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler()))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    with pytest.raises(HydrationError, match="not granted"):
        resolve_model(manifest, "gemini.not-there")


async def test_resolve_model_fails_closed_on_empty_catalog() -> None:
    empty = dict(MANIFEST)
    empty["runtime"] = {"models": [], "mcpServers": [], "a2aAgents": []}
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler(manifest=empty)))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    with pytest.raises(HydrationError, match="empty"):
        resolve_model(manifest, "gemini.gemini-flash-latest")


async def test_resolve_model_fails_closed_on_empty_selected_endpoint() -> None:
    """Important3: an empty/absent endpoint on the granted entry must reject before any
    metadata GET, not be returned as a usable catalog entry."""
    manifest_with_blank_endpoint = {
        **MANIFEST,
        "runtime": {
            "models": [{"id": "gemini.gemini-flash-latest", "endpoint": ""}],
            "mcpServers": [],
            "a2aAgents": [],
        },
    }
    client = httpx.AsyncClient(transport=_transport(_hydrate_handler(manifest=manifest_with_blank_endpoint)))
    manifest = await fetch_hydration_manifest(
        BASE_URL, "ek-abc", "platform", identity=IDENTITY, client=client
    )
    await client.aclose()

    with pytest.raises(HydrationError, match="empty endpoint"):
        resolve_model(manifest, "gemini.gemini-flash-latest")


# --- fetch_model_metadata -------------------------------------------------------


async def test_fetch_model_metadata_sends_url_encoded_model_and_ek_header_only_after_selection() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/model/info"
        seen["query"] = dict(httpx.QueryParams(request.url.query))
        seen["headers"] = dict(request.headers)
        entry = {"model_name": "weird model/name", "model_info": PRICED_MODEL_INFO}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "super-secret-ek", "weird model/name", client=client)
    await client.aclose()

    assert seen["query"] == {"model": "weird model/name"}
    assert seen["headers"]["x-ach-key"] == "super-secret-ek"
    assert not any(k.lower() == "authorization" for k in seen["headers"])
    assert metadata is not None
    assert metadata.context_limit == 200000
    assert metadata.output_limit == 8192
    assert metadata.supports_vision is True
    assert metadata.supports_reasoning is False
    assert metadata.input_cost_per_token == 1e-6


async def test_fetch_model_metadata_omits_unsupplied_flags_as_none_not_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sparse_info = {"max_input_tokens": 100, "supports_vision": True}
        entry = {"model_name": "gpt-4", "model_info": sparse_info}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is not None
    assert metadata.supports_vision is True
    assert metadata.supports_pdf_input is None
    assert metadata.supports_audio_input is None
    assert metadata.supports_video_input is None
    assert metadata.supports_function_calling is None
    assert metadata.supports_reasoning is None
    assert metadata.output_limit is None


async def test_fetch_model_metadata_none_when_entry_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_cost_field_is_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        bad_info = dict(PRICED_MODEL_INFO)
        bad_info["input_cost_per_token"] = "not-a-number"
        entry = {"model_name": "gpt-4", "model_info": bad_info}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_upstream_errors_rather_than_fabricating_defaults() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_response_body_is_malformed_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{not-json", headers={"content-type": "application/json"})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_token_limit_is_fractional() -> None:
    """Important4: a fractional token limit is malformed typed metadata — degrade to
    None, never let the underlying ValidationError escape and abort hydration."""
    def handler(request: httpx.Request) -> httpx.Response:
        bad_info = dict(PRICED_MODEL_INFO)
        bad_info["max_input_tokens"] = 1.5
        entry = {"model_name": "gpt-4", "model_info": bad_info}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


async def test_fetch_model_metadata_none_when_mode_is_malformed_shape() -> None:
    """Important4: a non-string `mode` is malformed typed metadata — degrade to None."""
    def handler(request: httpx.Request) -> httpx.Response:
        bad_info = dict(PRICED_MODEL_INFO)
        bad_info["mode"] = {"bad": "shape"}
        entry = {"model_name": "gpt-4", "model_info": bad_info}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    metadata = await fetch_model_metadata(BASE_URL, "ek", "gpt-4", client=client)
    await client.aclose()

    assert metadata is None


# --- Hydrator.load --------------------------------------------------------------


def _combined_handler() -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json=MANIFEST)
        if request.url.path == "/v2/model/info":
            entry = {"model_name": "gemini.gemini-flash-latest", "model_info": PRICED_MODEL_INFO}
            return httpx.Response(200, json={"data": [entry]})
        raise AssertionError(f"unexpected path {request.url.path}")

    return handler


SOURCE = HydrationInput(
    agent="classifier",
    base_url=BASE_URL,
    environment="platform",
    key_env="ACH_SECRET_IDENTITY",
    model="gemini.gemini-flash-latest",
)


async def test_hydrator_load_builds_catalog_with_metadata_and_mcp_and_skills() -> None:
    client = httpx.AsyncClient(transport=_transport(_combined_handler()))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    catalog = await hydrator.load(SOURCE)
    await client.aclose()

    assert catalog.environment == "platform"
    assert catalog.model.id == "gemini.gemini-flash-latest"
    assert catalog.model.endpoint == "https://ach.example.com/v1"
    assert catalog.model.metadata is not None
    assert catalog.model.metadata.context_limit == 200000
    assert catalog.mcp_servers[0].id == "mcp-google-calendar-ro"
    assert catalog.skills[0].name == "frontend-design@anthropics-skills"


async def test_hydration_retains_peer_grants() -> None:
    peers = [
        {"id": "reviewer", "endpoint": "https://peer.example/a2a"},
        {"id": "planner", "endpoint": "https://planner.example/rpc"},
    ]
    manifest = {
        **MANIFEST,
        "runtime": {**MANIFEST["runtime"], "a2aAgents": peers},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            return httpx.Response(200, json=manifest)
        return httpx.Response(200, json={"data": []})

    client = httpx.AsyncClient(transport=_transport(handler))
    catalog = await Hydrator(resolve_ek=lambda name: "ek-private", client=client).load(
        SOURCE
    )
    await client.aclose()

    assert [(agent.id, agent.endpoint) for agent in catalog.a2a_agents] == [
        ("reviewer", "https://peer.example/a2a"),
        ("planner", "https://planner.example/rpc"),
    ]


async def test_hydration_without_peer_grants_keeps_empty_tuple() -> None:
    client = httpx.AsyncClient(transport=_transport(_combined_handler()))
    catalog = await Hydrator(resolve_ek=lambda name: "ek-private", client=client).load(
        SOURCE
    )
    await client.aclose()
    assert catalog.a2a_agents == ()


async def test_hydrator_load_sends_identity_built_from_config_agent_and_environment() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/platform/hydrate":
            seen["headers"] = dict(request.headers)
            return httpx.Response(200, json=MANIFEST)
        entry = {"model_name": "gemini.gemini-flash-latest", "model_info": PRICED_MODEL_INFO}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    await hydrator.load(SOURCE)
    await client.aclose()

    assert seen["headers"]["x-ach-agent"] == "classifier"
    assert seen["headers"]["x-ach-environment"] == "platform"


async def test_hydrator_load_fails_closed_when_ek_resolver_cannot_resolve() -> None:
    def failing_resolver(name: str) -> str:
        raise HydrationError(f"ach.identity env {name!r} is unset")

    client = httpx.AsyncClient(transport=_transport(_combined_handler()))
    hydrator = Hydrator(resolve_ek=failing_resolver, client=client)

    with pytest.raises(HydrationError, match="unset"):
        await hydrator.load(SOURCE)
    await client.aclose()


async def test_hydrator_load_fails_closed_when_model_not_granted() -> None:
    source = dataclasses.replace(SOURCE, model="not-granted-model")

    client = httpx.AsyncClient(transport=_transport(_combined_handler()))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    with pytest.raises(HydrationError, match="not granted"):
        await hydrator.load(source)
    await client.aclose()


async def test_hydrator_load_fails_closed_when_model_not_granted_performs_zero_metadata_requests() -> None:
    """Important5 evidence: an invalid selection must never reach the metadata GET."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json=MANIFEST)

    source = dataclasses.replace(SOURCE, model="not-granted-model")

    client = httpx.AsyncClient(transport=_transport(handler))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    with pytest.raises(HydrationError, match="not granted"):
        await hydrator.load(source)
    await client.aclose()

    assert calls == ["/platform/hydrate"]


async def test_hydrator_load_fails_closed_on_empty_selected_endpoint_performs_zero_metadata_requests() -> None:
    """Important3 evidence through the real call path: an unusable (empty) selected
    endpoint must raise HydrationError before any metadata GET is attempted."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/platform/hydrate":
            manifest = {
                **MANIFEST,
                "runtime": {
                    "models": [{"id": "gemini.gemini-flash-latest", "endpoint": ""}],
                    "mcpServers": [],
                    "a2aAgents": [],
                },
            }
            return httpx.Response(200, json=manifest)
        raise AssertionError("metadata must not be fetched for an unusable selected endpoint")

    client = httpx.AsyncClient(transport=_transport(handler))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    with pytest.raises(HydrationError, match="empty endpoint"):
        await hydrator.load(SOURCE)
    await client.aclose()

    assert calls == ["/platform/hydrate"]


async def test_hydrator_load_fails_closed_on_omitted_selected_endpoint_performs_zero_metadata_requests() -> None:
    """Important3 evidence: an endpoint field entirely absent from the manifest (not an
    explicit empty string) still defaults to "" (`ModelEntry.endpoint`) and must reject
    before any metadata GET, same as the explicit-empty case above."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/platform/hydrate":
            manifest = {
                **MANIFEST,
                "runtime": {
                    "models": [{"id": "gemini.gemini-flash-latest"}],
                    "mcpServers": [],
                    "a2aAgents": [],
                },
            }
            return httpx.Response(200, json=manifest)
        raise AssertionError("metadata must not be fetched for an omitted selected endpoint")

    client = httpx.AsyncClient(transport=_transport(handler))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    with pytest.raises(HydrationError, match="empty endpoint"):
        await hydrator.load(SOURCE)
    await client.aclose()

    assert calls == ["/platform/hydrate"]


async def test_hydrator_load_granted_weird_model_name_posts_before_get_in_order() -> None:
    """Important5 evidence: a granted model id containing spaces/slashes goes through the
    real Hydrator.load call order (POST hydrate, then GET metadata with that id URL-encoded
    as a query param) rather than a synthetic direct fetch_model_metadata call."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/platform/hydrate":
            manifest = {
                **MANIFEST,
                "runtime": {
                    "models": [{"id": "weird model/name", "endpoint": "https://ach.example.com/v1"}],
                    "mcpServers": [],
                    "a2aAgents": [],
                },
            }
            return httpx.Response(200, json=manifest)
        assert request.url.path == "/v2/model/info"
        assert dict(httpx.QueryParams(request.url.query)) == {"model": "weird model/name"}
        entry = {"model_name": "weird model/name", "model_info": PRICED_MODEL_INFO}
        return httpx.Response(200, json={"data": [entry]})

    client = httpx.AsyncClient(transport=_transport(handler))
    hydrator = Hydrator(resolve_ek=lambda name: "ek-abc", client=client)

    source = dataclasses.replace(SOURCE, model="weird model/name")

    catalog = await hydrator.load(source)
    await client.aclose()

    assert calls == ["/platform/hydrate", "/v2/model/info"]
    assert catalog.model.id == "weird model/name"
def test_stage_context_extracts_skill_payload_with_ach_key_only_on_download() -> None:
    import asyncio
    import io
    import tarfile
    import tempfile
    from pathlib import Path
    from ach_runtime.harness.hydrate import ContextItem, HydratedCatalog, HydratedModel, stage_context
    async def scenario(root: Path) -> None:
        archive = io.BytesIO()
        with tarfile.open(fileobj=archive, mode="w:gz") as tf:
            payload = b"name: sample\n"
            info = tarfile.TarInfo("sample/SKILL.md")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
        expected = archive.getvalue()
        requests = []
        async def handler(request):
            requests.append(request)
            return httpx.Response(200, content=expected)
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        catalog = HydratedCatalog("prod", HydratedModel("m", "https://model", None), (),
                                  (ContextItem(name="sample", id="1", downloadUrl="https://ach/skill"),), (), ())
        await stage_context(catalog, root, excluded_skills=[], client=client, ek="ek_secret")
        files = [str(p.relative_to(root)) for p in root.rglob("*")]
        assert "skills/sample/SKILL.md" in files, files
        assert (root / "skills" / "sample" / "SKILL.md").read_bytes() == b"name: sample\n"
        assert requests[0].headers.get("x-ach-key") == "ek_secret"
        assert "ek_secret" not in (root / "skills" / "sample" / "SKILL.md").read_text()
        await client.aclose()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        asyncio.run(scenario(Path(d) / "stage"))


def test_stage_context_nests_root_level_skill_archives_under_their_names() -> None:
    """ACH serves each skill's files at the archive root; two skills must not collide."""
    import asyncio
    import io
    import tarfile
    import tempfile
    from pathlib import Path
    from ach_runtime.harness.hydrate import ContextItem, HydratedCatalog, HydratedModel, stage_context

    def archive(skill: str) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            for name, payload in (("SKILL.md", f"name: {skill}\n".encode()), ("LICENSE.txt", b"MIT\n")):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tf.addfile(info, io.BytesIO(payload))
        return buf.getvalue()

    async def scenario(root: Path) -> None:
        async def handler(request):
            return httpx.Response(200, content=archive(request.url.path.rsplit("/", 1)[-1]))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        skills = tuple(ContextItem(name=n, id=n, downloadUrl=f"https://ach/skill/{n}") for n in ("pdf", "docx"))
        catalog = HydratedCatalog("prod", HydratedModel("m", "https://model", None), (), skills, (), ())
        await stage_context(catalog, root, excluded_skills=[], client=client, ek="ek")
        await client.aclose()
        assert (root / "skills" / "pdf" / "SKILL.md").read_bytes() == b"name: pdf\n"
        assert (root / "skills" / "docx" / "SKILL.md").read_bytes() == b"name: docx\n"
        assert (root / "skills" / "docx" / "LICENSE.txt").is_file()
        assert not (root / "skills" / "SKILL.md").exists()

    with tempfile.TemporaryDirectory() as d:
        asyncio.run(scenario(Path(d) / "stage"))
