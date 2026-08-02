"""Jinja2 prompt rendering for AI-assisted task creation.

Why a template file instead of an f-string (the pattern the rest of this codebase uses, e.g.
`crag_loop.py`'s `_SYSTEM_PROMPT`): this prompt has a genuine conditional section — whether to list
`existing_labels` at all — which Jinja2's `{% if %}` expresses directly in the prompt text, instead of
building the string up with Python branches. It also means the prompt wording itself lives in one
`.jinja` file that can be diffed/versioned on its own, separate from the code that calls it.
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


def render_task_draft_prompt(existing_labels: Optional[List[str]] = None) -> str:
    template = _env.get_template("task_draft_system.jinja")
    return template.render(existing_labels=existing_labels or [])
