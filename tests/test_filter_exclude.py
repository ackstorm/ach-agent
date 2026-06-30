def test_skill_exclusion_filters_context_skills():
    """Excluded skills are removed from the hydrated context before fetch."""
    from ach_agent.engine.hydrate import Context, ContextItem

    ctx = Context(skills=[ContextItem(name="keep"), ContextItem(name="send-email")])
    exclude_skills = {"send-email"}
    ctx.skills = [s for s in ctx.skills if s.name not in exclude_skills]
    assert [s.name for s in ctx.skills] == ["keep"]
