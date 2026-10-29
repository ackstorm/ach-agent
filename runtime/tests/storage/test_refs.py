# SPDX-License-Identifier: MIT
"""StateRef identity/domain/key confinement (dispatch §required-behavior 1)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ach_runtime.config.schema import AgentIdentity
from ach_runtime.storage.refs import FrozenAgentIdentity, StateRef

_AGENT = AgentIdentity(name="a", namespace="agents", uid="11111111-1111-1111-1111-111111111111")
_WS = "a" * 64
_SESS = "b" * 64


def test_workspace_ref_round_trips_registry_and_snapshot_keys() -> None:
    ref = StateRef(agent=_AGENT, workspace_ref=_WS, domain="workspace")
    assert ref.registry_key("prefix") == f"prefix/{_AGENT.namespace}/{_AGENT.uid}/{_WS}/registry.json"
    assert (
        ref.snapshot_key("prefix", "snap-1")
        == f"prefix/{_AGENT.namespace}/{_AGENT.uid}/{_WS}/snapshots/snap-1.tar.gz"
    )


def test_different_namespace_same_uid_produces_different_keys() -> None:
    """Key derivation must not omit namespace — two namespaces must never collide."""
    other_ns = StateRef(
        agent=AgentIdentity(name="a", namespace="other-ns", uid=_AGENT.uid),
        workspace_ref=_WS,
        domain="workspace",
    )
    same_ns = StateRef(agent=_AGENT, workspace_ref=_WS, domain="workspace")
    assert other_ns.registry_key("p") != same_ns.registry_key("p")


def test_session_ref_scopes_keys_under_its_workspace() -> None:
    ref = StateRef(agent=_AGENT, workspace_ref=_WS, domain="session", session_ref=_SESS)
    assert (
        ref.registry_key("p")
        == f"p/{_AGENT.namespace}/{_AGENT.uid}/{_WS}/sessions/{_SESS}/registry.json"
    )
    assert (
        ref.snapshot_key("p", "snap-1")
        == f"p/{_AGENT.namespace}/{_AGENT.uid}/{_WS}/sessions/{_SESS}/snapshots/snap-1.tar.gz"
    )


def test_session_domain_requires_session_ref() -> None:
    with pytest.raises(ValidationError):
        StateRef(agent=_AGENT, workspace_ref=_WS, domain="session")


def test_workspace_domain_forbids_session_ref() -> None:
    with pytest.raises(ValidationError):
        StateRef(agent=_AGENT, workspace_ref=_WS, domain="workspace", session_ref=_SESS)


@pytest.mark.parametrize(
    "bad_ref",
    [
        "short",
        "g" * 64,  # not hex
        "A" * 64,  # not lowercase
        "a" * 63,
    ],
)
def test_non_opaque_hex_ref_rejected(bad_ref: str) -> None:
    with pytest.raises(ValidationError):
        StateRef(agent=_AGENT, workspace_ref=bad_ref, domain="workspace")


def test_unknown_domain_rejected() -> None:
    with pytest.raises(ValidationError):
        StateRef(agent=_AGENT, workspace_ref=_WS, domain="artifact")


def test_state_ref_is_frozen() -> None:
    ref = StateRef(agent=_AGENT, workspace_ref=_WS, domain="workspace")
    with pytest.raises(ValidationError):
        ref.workspace_ref = _SESS  # type: ignore[misc]


def test_embedded_identity_is_a_genuinely_immutable_snapshot() -> None:
    """Review: the frozen outer StateRef contained a mutable AgentIdentity — mutating the
    source identity after construction must never change an already-validated StateRef,
    and the embedded identity itself must refuse direct mutation."""
    source = AgentIdentity(name="a", namespace="agents", uid="22222222-2222-2222-2222-222222222222")
    ref = StateRef(agent=source, workspace_ref=_WS, domain="workspace")

    assert isinstance(ref.agent, FrozenAgentIdentity)
    with pytest.raises(Exception):  # noqa: B017 — dataclasses.FrozenInstanceError
        ref.agent.uid = "33333333-3333-3333-3333-333333333333"  # type: ignore[misc]

    # Pydantic models are mutable by default; proving StateRef holds a copy, not the live
    # object, is the actual regression the review named.
    source.uid = "44444444-4444-4444-4444-444444444444"
    assert ref.agent.uid == "22222222-2222-2222-2222-222222222222"


def test_mutated_shared_agent_identity_is_revalidated_not_trusted() -> None:
    """`AgentIdentity` has no `validate_assignment` — `.uid = "garbage"` succeeds silently
    on the source object. `StateRef` must revalidate the CURRENT field values at
    construction time, not simply copy them (review finding 7)."""
    source = AgentIdentity(name="a", namespace="agents", uid="22222222-2222-2222-2222-222222222222")
    source.uid = "not-a-uuid-at-all"  # unvalidated mutation, succeeds on AgentIdentity itself

    with pytest.raises(ValidationError):
        StateRef(agent=source, workspace_ref=_WS, domain="workspace")


def test_directly_supplied_frozen_identity_with_malformed_uid_rejected() -> None:
    """A bare `FrozenAgentIdentity` is just a dataclass with no validation of its own —
    `StateRef` must still run it through canonical-UUID validation, not accept it as-is."""
    forged = FrozenAgentIdentity(name="a", namespace="agents", uid="not-a-uuid")
    with pytest.raises(ValidationError):
        StateRef(agent=forged, workspace_ref=_WS, domain="workspace")


def test_directly_supplied_frozen_identity_with_valid_uid_accepted() -> None:
    valid = FrozenAgentIdentity(
        name="a", namespace="agents", uid="22222222-2222-2222-2222-222222222222"
    )
    ref = StateRef(agent=valid, workspace_ref=_WS, domain="workspace")
    assert ref.agent.uid == "22222222-2222-2222-2222-222222222222"
