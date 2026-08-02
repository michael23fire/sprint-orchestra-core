"""Push a prompt template's current content to Phoenix as a new version, for tracking/comparison.

Run manually when you deliberately change a prompt and want the change on record (not on every
service start — a version per meaningful edit, not per process restart).

Scope, stated precisely rather than implied: Phoenix's native template formats (`MUSTACHE`,
`F_STRING`) don't support Jinja2's `{% if %}`/`{% for %}` control flow, so this pushes the raw
`.jinja` source with `template_format="NONE"` — Phoenix stores and versions the *content* for you to
browse/diff/compare in its UI, but does **not** render it. Rendering still happens exactly as before,
via `app/drafting/prompts.py`'s Jinja2 `Environment`. This script is a version-tracking side-channel,
not a runtime dependency — the drafting feature works identically whether or not this has ever been run.

Usage:
    docker run -d -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest   # if not already running
    python -m scripts.push_prompt_to_phoenix
"""
from __future__ import annotations

from pathlib import Path

from phoenix.client import Client
from phoenix.client.types import PromptVersion

_TEMPLATE_PATH = Path(__file__).parent.parent / "app/drafting/templates/task_draft_system.jinja"
_PROMPT_NAME = "task-draft-system-prompt"


def main() -> None:
    template_text = _TEMPLATE_PATH.read_text()
    client = Client()  # defaults to PHOENIX_COLLECTOR_ENDPOINT / http://localhost:6006

    version = client.prompts.create(
        name=_PROMPT_NAME,
        version=PromptVersion(
            [{"role": "system", "content": template_text}],
            model_name="qwen/qwen3.6-35b-a3b",
            model_provider="OPENAI",
            template_format="NONE",  # see module docstring — Jinja2 control flow isn't representable
        ),
        prompt_description=(
            f"Jinja2 source for AI-assisted task creation ({_TEMPLATE_PATH.relative_to(_TEMPLATE_PATH.parent.parent.parent)}). "
            "Stored as-is for version tracking/comparison in the Phoenix UI -- actual rendering "
            "happens via app/drafting/prompts.py's Jinja2 Environment, not by Phoenix."
        ),
    )
    print(f"pushed prompt version: {version.id}")
    print(f"view it at: http://localhost:6006/prompts")


if __name__ == "__main__":
    main()
