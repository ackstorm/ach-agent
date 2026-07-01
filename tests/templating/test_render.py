from ach_agent.templating.render import (
    build_template_context,
    render_template,
    resolve_path,
)


def _ctx():
    return build_template_context(
        {"project": {"full_name": "backend/payments"}, "commits": [{"id": "abc"}]},
        channel_name="gitlab-mr",
        channel_type="webhook",
        channel_source="gitlab",
        agent_name="reviewer",
        memory_bank="gitlab-pr-review",
        event_id="evt-1",
        session_key="ses-1",
    )


def test_resolve_payload_nested():
    assert resolve_path(_ctx(), "payload.project.full_name") == "backend/payments"


def test_resolve_list_index():
    assert resolve_path(_ctx(), "payload.commits.0.id") == "abc"


def test_resolve_internal_namespace():
    ctx = _ctx()
    assert resolve_path(ctx, "internal.channel.name") == "gitlab-mr"
    assert resolve_path(ctx, "internal.channel.source") == "gitlab"
    assert resolve_path(ctx, "internal.agent.name") == "reviewer"
    assert resolve_path(ctx, "internal.memory.bank") == "gitlab-pr-review"


def test_resolve_missing_returns_none():
    assert resolve_path(_ctx(), "payload.nope.deeper") is None


def test_resolve_non_scalar_returns_none():
    # a dict/list is not a usable scalar
    assert resolve_path(_ctx(), "payload.project") is None
    assert resolve_path(_ctx(), "payload.commits") is None


def test_resolve_env_namespace_is_unreachable():
    # there is NO env namespace — ek-hygiene at the template layer
    assert resolve_path(_ctx(), "env.ACH_TOKEN") is None


def test_render_substitutes_scalar():
    out = render_template("Review {{ payload.project.full_name }} now", _ctx())
    assert out == "Review backend/payments now"


def test_render_whitespace_insensitive():
    assert render_template("{{payload.project.full_name}}", _ctx()) == "backend/payments"


def test_render_default_used_when_missing():
    out = render_template('{{ payload.missing | default("unknown") }}', _ctx())
    assert out == "unknown"


def test_render_missing_no_default_becomes_empty():
    out = render_template("x={{ payload.missing }};", _ctx())
    assert out == "x=;"


def test_render_internal_event_and_session():
    out = render_template("{{ internal.event.id }}/{{ internal.session.key }}", _ctx())
    assert out == "evt-1/ses-1"


def test_header_namespace_reserved_empty():
    # headers are not threaded across the seam yet — reserved, resolves missing
    assert resolve_path(_ctx(), "header.x-api-key") is None


def test_project_template_renders_session_key() -> None:
    """Task 3 contract: {{ internal.session.key }} renders to the session key; literals pass through."""
    ctx = build_template_context(
        {},
        channel_name="cron-daily",
        channel_type="cron",
        channel_source="",
        agent_name="agent",
        memory_bank="",
        event_id="evt-1",
        session_key="gitlab.com/g/repo",
    )
    assert render_template("{{ internal.session.key }}", ctx) == "gitlab.com/g/repo"
    assert render_template("ach-agent", ctx) == "ach-agent"
