"""Jinja2 prompt rendering for AI-assisted epic planning. Same rationale and setup as
`app/drafting/prompts.py::render_task_draft_prompt` (conditional `existing_labels` section).
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Optional

from jinja2 import Environment, FileSystemLoader, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(_TEMPLATES_DIR),
    autoescape=select_autoescape(disabled_extensions=("jinja",)),
    trim_blocks=True,
    lstrip_blocks=True,
)


def render_epic_plan_prompt(existing_labels: Optional[List[str]] = None) -> str:
    template = _env.get_template("epic_plan_system.jinja")
    return template.render(existing_labels=existing_labels or [])


def render_epic_plan_refine_prompt(existing_labels: Optional[List[str]] = None) -> str:
    template = _env.get_template("epic_plan_refine_system.jinja")
    return template.render(existing_labels=existing_labels or [])


def render_critic_prompt() -> str:
    """No `existing_labels` parameter: the critic reports findings and never edits the plan, so it has
    no use for the label vocabulary.
    """
    return _env.get_template("critic_system.jinja").render()
