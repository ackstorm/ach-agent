# SPDX-License-Identifier: MIT
"""facades/model.py::present_metadata/public_catalog — pure projection of the hydrated
catalog into the engine owner's fixture shape (contract §5; `plugins/managed-config/
index.js::toModelInfo`). No registry/type is introduced; every key is independently
optional, so absent upstream data is omitted rather than fabricated as a zero/false
default (contract §5's "never a fabricated zero-cost/no-limit default" extended to
booleans — see `harness/hydrate.py::_optional_bool`)."""

from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest

from ach_runtime.facades._proxy import ProcessIdentity
from ach_runtime.facades.model import ModelFacade, present_metadata, public_catalog
from ach_runtime.harness.hydrate import HydratedCatalog, HydratedModel, McpServerEntry, ModelMetadata
from ach_runtime.harness.tokens import facade_token

FULL_METADATA = ModelMetadata(
    context_limit=200000,
    output_limit=8192,
    supports_vision=True,
    supports_pdf_input=True,
    supports_audio_input=False,
    supports_video_input=False,
    mode="chat",
    supports_function_calling=True,
    supports_reasoning=False,
    input_cost_per_token=1e-6,
    output_cost_per_token=2e-6,
    cache_read_input_token_cost=1e-7,
)


def _catalog(
    *,
    model_id: str = "gemini.gemini-flash-latest",
    metadata: ModelMetadata | None,
    mcp_servers: tuple[McpServerEntry, ...] = (),
) -> HydratedCatalog:
    return HydratedCatalog(
        environment="platform",
        model=HydratedModel(id=model_id, endpoint="https://ach.example.com/v1", metadata=metadata),
        mcp_servers=mcp_servers,
        skills=(),
        prompts=(),
        artifacts=(),
    )


# --- present_metadata -------------------------------------------------------------


def test_present_metadata_none_yields_empty_object() -> None:
    assert present_metadata(None) == {}


def test_present_metadata_maps_limit_and_cost_scaled_to_per_million_tokens() -> None:
    out = present_metadata(FULL_METADATA)
    assert out["limit"] == {"context": 200000, "output": 8192}
    assert out["cost"] == {"input": 1.0, "output": 2.0, "cache_read": 0.1}


def test_present_metadata_maps_tool_call_and_reasoning_retaining_explicit_false() -> None:
    out = present_metadata(FULL_METADATA)
    assert out["tool_call"] is True
    assert out["reasoning"] is False  # explicit upstream false, not omitted


def test_present_metadata_modalities_in_legacy_order_with_attachment_true() -> None:
    out = present_metadata(FULL_METADATA)
    assert out["modalities"] == {"input": ["text", "image", "pdf"], "output": ["text"]}
    assert out["attachment"] is True  # vision/pdf supplied true


def test_present_metadata_all_modalities_true_follow_legacy_order() -> None:
    metadata = FULL_METADATA.model_copy(
        update={"supports_audio_input": True, "supports_video_input": True}
    )
    out = present_metadata(metadata)
    assert out["modalities"]["input"] == ["text", "image", "audio", "pdf", "video"]


def test_present_metadata_omits_modalities_and_attachment_when_unsupplied() -> None:
    metadata = ModelMetadata(context_limit=1000)
    out = present_metadata(metadata)
    assert "modalities" not in out
    assert "attachment" not in out


def test_present_metadata_modalities_present_but_attachment_false_when_all_flags_false() -> None:
    metadata = ModelMetadata(
        supports_vision=False, supports_pdf_input=False, supports_audio_input=False, supports_video_input=False
    )
    out = present_metadata(metadata)
    assert out["modalities"] == {"input": ["text"], "output": ["text"]}
    assert out["attachment"] is False


def test_present_metadata_omits_tool_call_and_reasoning_when_unsupplied() -> None:
    metadata = ModelMetadata(context_limit=1000)
    out = present_metadata(metadata)
    assert "tool_call" not in out
    assert "reasoning" not in out


def test_present_metadata_omits_limit_and_cost_keys_entirely_when_all_subfields_absent() -> None:
    metadata = ModelMetadata(supports_function_calling=True)
    out = present_metadata(metadata)
    assert "limit" not in out
    assert "cost" not in out
    assert out == {"tool_call": True}


def test_present_metadata_partial_limit_and_cost_omit_only_the_missing_subfield() -> None:
    metadata = ModelMetadata(context_limit=1000, input_cost_per_token=5e-7)
    out = present_metadata(metadata)
    assert out["limit"] == {"context": 1000}
    assert out["cost"] == {"input": 0.5}


def test_present_metadata_retains_explicit_numeric_zero_not_omitted() -> None:
    """Important5 evidence: an explicit upstream 0 (context/cost) must survive, since
    `is not None` checks (not truthiness) gate every optional field."""
    metadata = ModelMetadata(context_limit=0, output_limit=0, input_cost_per_token=0.0)
    out = present_metadata(metadata)
    assert out["limit"] == {"context": 0, "output": 0}
    assert out["cost"] == {"input": 0.0}


# --- public_catalog ----------------------------------------------------------------


def test_public_catalog_projects_exactly_selected_model_and_facade_urls() -> None:
    catalog = _catalog(metadata=FULL_METADATA)
    result = public_catalog(
        catalog,
        provider_id="gemini",
        model_url="https://facade.internal/t/tok123/v1",
        mcp_urls={"mcp-google-calendar-ro": "https://facade.internal/t/tok123/mcp/mcp-google-calendar-ro"},
        memory_url="https://facade.internal/t/tok123/memory",
    )

    assert result["providers"]["gemini"]["baseURL"] == "https://facade.internal/t/tok123/v1"
    assert result["providers"]["gemini"]["apiKey"] == "local-proxy"
    assert set(result["providers"]["gemini"]["models"]) == {"gemini.gemini-flash-latest"}
    assert result["mcp"]["mcp-google-calendar-ro"] == {
        "url": "https://facade.internal/t/tok123/mcp/mcp-google-calendar-ro",
        "headers": {},
    }
    assert result["mcp"]["memory"] == {"url": "https://facade.internal/t/tok123/memory", "headers": {}}


def test_public_catalog_omits_memory_entry_when_unavailable() -> None:
    catalog = _catalog(metadata=None)
    result = public_catalog(
        catalog, provider_id="gemini", model_url="https://facade.internal/t/tok/v1", mcp_urls={}
    )

    assert "memory" not in result["mcp"]
    assert result["providers"]["gemini"]["models"]["gemini.gemini-flash-latest"] == {}


def test_public_catalog_no_credential_sentinel_or_raw_backend_endpoint_in_result() -> None:
    catalog = _catalog(metadata=FULL_METADATA)
    result = public_catalog(
        catalog,
        provider_id="gemini",
        model_url="https://facade.internal/t/tok123/v1",
        mcp_urls={"m1": "https://facade.internal/t/tok123/mcp/m1"},
    )

    serialized = str(result)
    assert "ach.example.com" not in serialized  # the real hydrated backend endpoint
    assert "ek-" not in serialized


def test_public_catalog_with_prefiltered_granted_mcp_servers_only_emits_provided_entries() -> None:
    """Important5 evidence: `mcp_urls` arrives already grant/filter-restricted (a raw
    excluded server id is simply never passed in) — public_catalog performs no filtering
    of its own, it only ever emits what it was given. The hydrated catalog itself carries
    `raw-memory-mcp` (a real granted server), proving the omission is an upstream filter
    decision, not an artifact of the fixture never containing that entry."""
    hydrated_servers = (
        McpServerEntry(id="mcp-google-calendar-ro", endpoint="https://ach.example.com/mcp/mcp-google-calendar-ro"),
        McpServerEntry(id="mcp-notion-ro", endpoint="https://ach.example.com/mcp/mcp-notion-ro"),
        McpServerEntry(id="raw-memory-mcp", endpoint="https://ach.example.com/mcp/raw-memory-mcp"),
    )
    catalog = _catalog(metadata=FULL_METADATA, mcp_servers=hydrated_servers)
    granted_only = {
        server.id: f"https://facade.internal/t/tok123/mcp/{server.id}"
        for server in hydrated_servers
        if server.id != "raw-memory-mcp"
    }
    result = public_catalog(
        catalog,
        provider_id="gemini",
        model_url="https://facade.internal/t/tok123/v1",
        mcp_urls=granted_only,
    )

    assert set(result["mcp"]) == {"mcp-google-calendar-ro", "mcp-notion-ro"}
    assert "raw-memory-mcp" not in result["mcp"]


async def test_public_catalog_call_emits_no_credential_or_raw_backend_sentinel_in_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Important5 evidence: facade-module log records produced around an actual
    authenticated facade request must never carry the real ek_/backend-endpoint sentinel.
    Filtered to `ach_runtime.facades` records only — third-party httpx request-URL debug
    logs legitimately mention the (non-secret) upstream host and must not fail this."""

    async def _one_chunk(data: bytes) -> AsyncIterator[bytes]:
        yield data

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_one_chunk(b"{}"))

    async def _owned(candidate: str) -> bool:
        return True

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    facade = ModelFacade(
        endpoint="https://ach.example.com/v1",
        headers={"x-ach-key": "ek-super-secret-sentinel"},
        api="openai",
        model="m",
        key=b"fixture-key",
        owned_workspace=_owned,
        identity=ProcessIdentity(agent="classifier", environment="platform"),
        client=upstream,
    )
    try:
        token = facade_token(b"fixture-key", "owned-ws")
        transport = httpx.ASGITransport(app=facade.app())
        with caplog.at_level("DEBUG"):
            async with httpx.AsyncClient(transport=transport, base_url="http://facade") as client:
                resp = await client.post(f"/t/{token}/chat/completions", json={"model": "m"})

            catalog = _catalog(metadata=FULL_METADATA)
            public_catalog(
                catalog,
                provider_id="gemini",
                model_url="https://facade.internal/t/tok123/v1",
                mcp_urls={"mcp-google-calendar-ro": "https://facade.internal/t/tok123/mcp/mcp-google-calendar-ro"},
            )

        assert resp.status_code == 200
        facade_records = [r for r in caplog.records if r.name.startswith("ach_runtime.facades")]
        assert not any("ach.example.com" in r.getMessage() for r in facade_records)
        assert not any("ek-super-secret-sentinel" in r.getMessage() for r in facade_records)
    finally:
        await facade.aclose()
        await upstream.aclose()
