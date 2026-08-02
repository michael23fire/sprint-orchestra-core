"""AI-assisted task creation: one LLM call -> validated title/labels/estimate/dependencies.

Degradation strategy (the point isn't just "try/except", it's *what* the fallback returns): if
`instructor`'s own retries (re-prompting the model with its own validation error) are exhausted —
model unreachable, or a small local model that just can't produce parseable structured output after
a couple of tries — this must never surface as a 500 to a caller trying to create a task. It falls
back to a safe, always-valid `TaskDraft` built from the raw description (first line as the title,
everything else left empty/null) and reports `degraded=True` so the caller can tell the difference
between "the AI drafted this" and "the AI failed, here's an unassisted starting point." A degraded
draft is still a usable task — worse suggestions, never a blocked user.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import List, Optional

from app.drafting.prompts import render_task_draft_prompt
from app.drafting.schemas import TaskDraft

logger = logging.getLogger(__name__)

# instructor's own re-prompt-on-validation-error retries, on top of whatever the underlying HTTP
# client already retries for transient network failures. Kept small: this is a synchronous,
# user-facing call (not a background job), so the cost of one more retry round trip is directly felt
# as latency by whoever's waiting on it.
_MAX_VALIDATION_RETRIES = 2
# Found live, twice: 500 was too tight, then 1500 still wasn't enough against real (longer,
# multi-paragraph) M3 issue descriptions — a reasoning-capable local model (qwen3.6-35b-a3b) spends a
# variable, sometimes large number of tokens on its own thinking preamble before emitting the JSON
# block Mode.MD_JSON parses, and repeatedly hit the ceiling mid-output ("The output is incomplete due
# to a max_tokens length limit"), which read as a structured-output failure and triggered the
# fallback path even though the model wasn't actually struggling with the schema — it just ran out of
# room to finish thinking. Generous headroom here is cheap relative to the cost of spuriously
# degrading a call that would have succeeded; max_tokens is a ceiling, not a target, so this doesn't
# slow down calls that finish well under it.
_MAX_OUTPUT_TOKENS = 4000


@dataclass(slots=True)
class DraftResult:
    draft: TaskDraft
    degraded: bool
    latency_seconds: float
    error: Optional[str] = None


def _fallback_draft(description: str) -> TaskDraft:
    first_line = description.strip().splitlines()[0] if description.strip() else ""
    title = first_line[:120] or "Untitled task"
    return TaskDraft(title=title, issue_type="task", labels=[], estimate_story_points=None, dependencies=[])


async def draft_task(
    client, model: str, description: str, existing_labels: Optional[List[str]] = None
) -> DraftResult:
    system_prompt = render_task_draft_prompt(existing_labels)
    start = time.perf_counter()
    try:
        draft = await client.chat.completions.create(
            model=model,
            response_model=TaskDraft,
            max_retries=_MAX_VALIDATION_RETRIES,
            max_tokens=_MAX_OUTPUT_TOKENS,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": description},
            ],
        )
        return DraftResult(draft=draft, degraded=False, latency_seconds=time.perf_counter() - start)
    except Exception as exc:  # noqa: BLE001 - a structured-output failure must degrade, never 500
        logger.warning(
            "task draft generation failed after retries; falling back to a safe default",
            extra={"error": str(exc)},
        )
        return DraftResult(
            draft=_fallback_draft(description),
            degraded=True,
            latency_seconds=time.perf_counter() - start,
            error=str(exc),
        )
