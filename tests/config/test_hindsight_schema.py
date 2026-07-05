# SPDX-License-Identifier: Apache-2.0
import pytest
from pydantic import ValidationError

from ach_agent.config.schema import HindsightParams


def _base(**over):
    d = {
        "endpoint": "https://hs.example/mcp",
        "bank": "gitlab-pr-review",
        "auth": {"env": "HINDSIGHT_ADMIN_TOKEN"},
        "mentalModels": [
            {"id": "architecture", "name": "Arch", "sourceQuery": "What is the architecture?"}
        ],
    }
    d.update(over)
    return d


def test_rich_mental_models_parse_with_aliases():
    p = HindsightParams.model_validate(_base())
    assert p.auth is not None and p.auth.env == "HINDSIGHT_ADMIN_TOKEN"
    assert p.mission == ""
    mm = p.mental_models[0]
    assert (mm.id, mm.name, mm.source_query) == ("architecture", "Arch", "What is the architecture?")
    assert mm.auto_refresh is False and mm.max_tokens == 2048


def test_auth_optional_defaults_none():
    d = _base()
    del d["auth"]
    p = HindsightParams.model_validate(d)  # internal URL — no auth needed
    assert p.auth is None


def test_legacy_string_mental_models_rejected():
    with pytest.raises(ValidationError):
        HindsightParams.model_validate(_base(mentalModels=["architecture", "conventions"]))
