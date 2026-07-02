# SPDX-License-Identifier: Apache-2.0
"""Deterministic {{ }} template substitution (zero-dependency, no env exposure)."""

from ach_agent.templating.render import build_template_context, render_template

__all__ = ["build_template_context", "render_template"]
