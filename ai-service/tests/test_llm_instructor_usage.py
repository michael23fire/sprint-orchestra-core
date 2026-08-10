"""Tests for app/llm/instructor_usage.py — extracted after the same normalization existed twice
(app/sprint_recovery/graph.py and app/sprint_pace/service.py) with a real behavioral gap between the
copies. See test_sprint_recovery_graph.py for the equivalent tests exercised through the old
`_usage_from_completion` alias, kept to prove the extraction didn't change that call site's behavior.
"""
from types import SimpleNamespace

from app.llm.instructor_usage import usage_from_completion
from app.llm.types import Usage


def test_anthropic_shape_is_read_directly():
    completion = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=1234, output_tokens=567,
        cache_creation_input_tokens=10, cache_read_input_tokens=20,
    ))

    usage = usage_from_completion(completion)

    assert usage == Usage(
        input_tokens=1234, output_tokens=567,
        cache_creation_input_tokens=10, cache_read_input_tokens=20,
    )


def test_openai_shape_with_no_caching_is_read_via_the_fallback_field_names():
    completion = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=120, completion_tokens=45))

    usage = usage_from_completion(completion)

    assert usage == Usage(input_tokens=120, output_tokens=45)


def test_openai_cached_input_tokens_are_not_double_counted_as_fresh_input():
    """OpenAI reports `prompt_tokens` *inclusive* of cached tokens, unlike Anthropic's separate
    counters — the bug that motivated extracting this into a shared module in the first place, since
    one of the two original copies never accounted for it at all."""
    completion = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    ))

    usage = usage_from_completion(completion)

    assert usage.input_tokens == 200  # the genuinely fresh portion
    assert usage.cache_read_input_tokens == 800
    assert usage.output_tokens == 100


def test_a_provider_reporting_no_usage_degrades_to_free_rather_than_raising():
    """A bare llama.cpp server reports no usage block at all. Cost visibility must never be the thing
    that breaks a real workflow — see app/llm/types.py's Usage."""
    assert usage_from_completion(SimpleNamespace(usage=None)) == Usage()
    assert usage_from_completion(SimpleNamespace()) == Usage()
