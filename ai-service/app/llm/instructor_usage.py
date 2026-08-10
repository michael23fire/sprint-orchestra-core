"""Normalizes `instructor`'s raw provider response into `Usage` — shared by every caller that uses
`create_with_completion` for real token/cost tracking (app/sprint_recovery/graph.py's `_call_model`,
app/sprint_pace/service.py's `summarize_sprint_pace`).

**Found live**: this was written twice, independently, with a real behavioral gap between the two
copies. Both correctly branched on Anthropic's `usage.input_tokens`/`output_tokens` vs. OpenAI's
`usage.prompt_tokens`/`completion_tokens`, but only one of them accounted for OpenAI's
`prompt_tokens_details.cached_tokens` — and OpenAI's `prompt_tokens` is *inclusive* of cached tokens
while Anthropic reports them separately, so the copy that didn't subtract it would overstate an
OpenAI-compatible call's fresh input by the cached amount (and, since cached input prices at a tenth
of fresh input, overstate cost on exactly that portion). Extracted here so there is one place this
logic can be correct in, not two places it can silently drift apart in.
"""
from __future__ import annotations

from app.llm.types import Usage


def usage_from_completion(completion) -> Usage:
    """Anthropic reports `input_tokens`/`output_tokens` (plus cache counters); OpenAI-compatible
    servers report `prompt_tokens`/`completion_tokens`, with cached input under
    `prompt_tokens_details.cached_tokens`. Anything missing counts as 0: a local server that reports
    no usage at all (a bare llama.cpp) must degrade to "free and unmeasured", never to an exception —
    same principle `Usage` itself already documents.
    """
    raw = getattr(completion, "usage", None)
    if raw is None:
        return Usage()

    def _int(*names: str) -> int:
        for name in names:
            value = getattr(raw, name, None)
            if isinstance(value, (int, float)):
                return int(value)
        return 0

    cached = 0
    details = getattr(raw, "prompt_tokens_details", None)
    if details is not None and isinstance(getattr(details, "cached_tokens", None), (int, float)):
        cached = int(details.cached_tokens)
    input_tokens = _int("input_tokens", "prompt_tokens")
    return Usage(
        # OpenAI's `prompt_tokens` is inclusive of cached tokens; Anthropic's `input_tokens` excludes
        # them (they're reported separately). Subtracting here keeps the two providers priced the same
        # way — cached input is billed at a tenth of fresh input, so folding them together would
        # overstate cost by ~10x on the cached portion.
        input_tokens=max(0, input_tokens - cached) if cached else input_tokens,
        output_tokens=_int("output_tokens", "completion_tokens"),
        cache_creation_input_tokens=_int("cache_creation_input_tokens"),
        cache_read_input_tokens=_int("cache_read_input_tokens") or cached,
    )
