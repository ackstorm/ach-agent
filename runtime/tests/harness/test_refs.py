# SPDX-License-Identifier: MIT
"""harness.refs — opaque digest refs (contract §2/§11)."""

from __future__ import annotations

import hashlib

import pytest

from ach_runtime.harness.refs import (
    invocation_digest,
    session_ref,
    workspace_name,
    workspace_ref,
)

AGENT_UID = "3fa0b3b2-9c7a-4e1d-8a2f-6d1c0e9b4a77"


def test_workspace_ref_is_64_hex_chars() -> None:
    ref = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")
    assert len(ref) == 64
    assert all(c in "0123456789abcdef" for c in ref)


def test_workspace_ref_deterministic() -> None:
    a = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")
    b = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")
    assert a == b


def test_workspace_ref_distinguishes_namespace_and_key_boundary() -> None:
    """Ambiguous string concatenation (e.g. 'a' + ':' + 'b:c' vs 'a:b' + ':' + 'c') must
    never collide — the digest is over a JSON ARRAY (order-preserving, boundary-safe), not
    a joined string (task-3 dispatch: "do not split keys on ':', use them as paths")."""
    a = workspace_ref("ach", AGENT_UID, "gitlab:x")
    b = workspace_ref("ach:gitlab", AGENT_UID, "x")
    assert a != b


def test_workspace_ref_unicode_and_colon_and_path_segments_safe() -> None:
    key = "gitlab:ch/with:colons/and/ünïcödé-segmênts"
    ref = workspace_ref("ach", AGENT_UID, key)
    assert len(ref) == 64


REF = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")


def test_workspace_name_short_agent_name_kept_as_is() -> None:
    assert workspace_name("gitlab-reviewer", REF) == f"agent-gitlab-reviewer-{REF[:20]}"


def test_workspace_name_exactly_25_chars_kept_as_is() -> None:
    name = "a" * 24 + "z"
    assert workspace_name(name, REF) == f"agent-{name}-{REF[:20]}"
    assert len(workspace_name(name, REF)) == 52  # contract §11 maximum


def test_workspace_name_long_agent_name_trimmed_and_hashed() -> None:
    name = "gitlab-merge-request-reviewer"  # 29 chars
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    assert workspace_name(name, REF) == f"agent-gitlab-merge-req-{digest}-{REF[:20]}"
    assert workspace_name(name, REF) == workspace_name(name, REF)


def test_workspace_name_long_names_sharing_a_prefix_differ() -> None:
    a = workspace_name("gitlab-merge-request-reviewer-a", REF)
    b = workspace_name("gitlab-merge-request-reviewer-b", REF)
    assert a != b
    assert a.startswith("agent-gitlab-merge-req-") and b.startswith("agent-gitlab-merge-req-")


def test_workspace_name_strips_trailing_dash_after_trim() -> None:
    name = "gitlab-reviewer--of-merge-requests"  # name[:16] ends with "-"
    digest = hashlib.sha256(name.encode()).hexdigest()[:8]
    assert workspace_name(name, REF) == f"agent-gitlab-reviewer-{digest}-{REF[:20]}"


def test_workspace_name_maps_dots_to_dashes() -> None:
    assert workspace_name("team.reviewer", REF) == f"agent-team-reviewer-{REF[:20]}"


@pytest.mark.parametrize(
    "name", ["a" * 25, "a" * 26, "a" * 253, "a" * 15 + "-" * 5 + "z", "a." * 12 + "a", ("ab." * 84)[:-2]]
)
def test_workspace_name_fits_controller_revision_hash_label(name: str) -> None:
    """A host may append "-<revision hash>" (observed up to 10 chars) to the name in a
    label, which is capped at 63 chars."""
    unit_name = workspace_name(name, REF)
    assert len(unit_name) <= 52
    assert len(unit_name + "-0123456789") <= 63
    assert len(unit_name + "-0") <= 63  # a replica or host name


@pytest.mark.parametrize("name", ["", "Bad Name", "UPPER", "-lead", "trail-", "a..b", "a" * 254])
def test_workspace_name_rejects_invalid_agent_names(name: str) -> None:
    with pytest.raises(ValueError):
        workspace_name(name, REF)


def test_workspace_name_full_ref_kept_for_collision_rejection() -> None:
    """The full 64-hex ref (not just the 20-hex suffix used in the name) must be kept by
    the caller to detect a genuine collision — refs.py itself never truncates the ref it
    returns from workspace_ref(), only workspace_name() truncates for the name."""
    ref = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")
    assert len(ref) == 64, "workspace_ref must return the FULL digest, not pre-truncated"


def test_session_ref_scoped_by_workspace_ref_not_raw_namespace() -> None:
    ws_ref = workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42")
    sref = session_ref(ws_ref, "mr:7")
    # Recomputing directly from (namespace, agent_uid, workspace_key, session_key) must NOT
    # match — session_ref is scoped by the already-opaque workspace_ref, not the raw triple.
    assert sref != workspace_ref("ach", AGENT_UID, "gitlab:gitlab-mr-review:42:mr:7")


def test_invocation_digest_includes_agent_channel_event_id() -> None:
    d1 = invocation_digest(AGENT_UID, "gitlab-mr-review", "evt-1")
    d2 = invocation_digest(AGENT_UID, "gitlab-mr-review", "evt-2")
    d3 = invocation_digest(AGENT_UID, "other-channel", "evt-1")
    assert len({d1, d2, d3}) == 3


def test_invocation_digest_deterministic() -> None:
    a = invocation_digest(AGENT_UID, "gitlab-mr-review", "evt-1")
    b = invocation_digest(AGENT_UID, "gitlab-mr-review", "evt-1")
    assert a == b
