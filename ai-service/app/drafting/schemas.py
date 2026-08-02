"""Structured output schema for AI-assisted task creation.

This is the Pydantic model `instructor` validates the LLM's response against — the model *is* the
API contract. Fields are deliberately conservative about what the LLM should commit to: `labels` and
`dependencies` default to empty (nothing invented if the description doesn't support it), and
`estimate_story_points` is `Optional` specifically so the model has a documented way to say "I don't
have enough signal to guess" instead of being forced to hallucinate a number to satisfy the schema.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class TaskDraft(BaseModel):
    title: str = Field(
        ..., min_length=1, max_length=120,
        description="Concise, actionable issue title in imperative mood (e.g. 'Add rate limiting to "
                    "login endpoint', not 'We should probably add some rate limiting maybe').",
    )
    issue_type: Literal["bug", "task", "story"] = Field(
        "task", description="Best guess at the kind of work this is."
    )
    labels: List[str] = Field(
        default_factory=list,
        description="Relevant labels, lowercase, only if clearly implied by the description — an "
                    "empty list is correct when nothing is clearly implied, not a failure to fill it in.",
    )
    estimate_story_points: Optional[int] = Field(
        None, ge=1, le=13,
        description="A rough Fibonacci-like estimate (1, 2, 3, 5, 8, 13), only if the description "
                    "gives enough signal to guess complexity. Null is the correct answer when it's "
                    "genuinely unclear — never guess a number just to fill the field.",
    )
    dependencies: List[str] = Field(
        default_factory=list,
        description="Other work this explicitly depends on (issue keys or short phrases), only if "
                    "the description actually mentions something blocking — never invented.",
    )
