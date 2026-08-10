"""Tests for AI-assisted sprint pace readouts (app/sprint_pace/) — same fake-instructor-client,
no-network approach as tests/test_drafting.py and tests/test_planning.py.

Covers: (1) a successful call returns the validated insight untouched; (2) a failure degrades to a
mechanically-assembled summary (no LLM) built directly from the stats, never a raised exception.
"""
from app.sprint_pace.prompts import render_sprint_stats_message
from app.sprint_pace.schemas import FlaggedIssue, SprintPaceInsight, SprintStats
from app.sprint_pace.service import summarize_sprint_pace


class _FakeUsage:
    def __init__(self, prompt_tokens=120, completion_tokens=45):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens


class _FakeCompletion:
    def __init__(self, usage=None):
        self.usage = usage or _FakeUsage()


class _FakeCompletions:
    def __init__(self, result, completion=None):
        self._result = result
        self._completion = completion or _FakeCompletion()
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result

    async def create_with_completion(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result, self._completion


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeInstructorClient:
    def __init__(self, result, completion=None):
        self.completions = _FakeCompletions(result, completion)
        self.chat = _FakeChat(self.completions)


def _stats(**overrides):
    defaults = dict(
        sprint_name="Sprint 12",
        risk_level="at_risk",
        days_remaining=3,
        committed_points=20,
        completed_points=8,
        total_points=22,
        issue_counts_by_status={"done": 3, "in_progress": 2, "blocked": 1},
        blocked_issues=[FlaggedIssue(issue_key="SCRUM-10", title="Payment retry", detail="blocked for 6 days")],
        stale_issues=[],
        unestimated_issues=[FlaggedIssue(issue_key="SCRUM-14", title="Polish empty state")],
    )
    defaults.update(overrides)
    return SprintStats(**defaults)


async def test_summarize_sprint_pace_returns_validated_insight_on_success():
    expected = SprintPaceInsight(summary="Behind pace but recoverable.", recommendations=["Unblock SCRUM-10"])
    client = FakeInstructorClient(expected)
    stats = _stats()

    result = await summarize_sprint_pace(client, "fake-model", stats)

    assert result.insight == expected
    assert result.degraded is False
    assert result.error is None
    assert result.input_tokens == 120
    assert result.output_tokens == 45
    assert result.estimated_cost_usd == 0.0  # "fake-model" is unpriced -> $0, not a KeyError
    assert client.completions.calls[0]["response_model"] is SprintPaceInsight


async def test_summarize_sprint_pace_does_not_double_count_openai_cached_input_tokens():
    """**Found live**: this file's own usage-normalization used to be a local copy of the same logic
    now shared via app.llm.instructor_usage — and the local copy never accounted for OpenAI's
    `prompt_tokens_details.cached_tokens`, which `prompt_tokens` already includes. Merging onto the
    shared helper fixed it here too; this pins the fix so this call site can't regress independently
    of app/llm/instructor_usage.py's own tests.
    """
    from types import SimpleNamespace

    expected = SprintPaceInsight(summary="Behind pace.", recommendations=[])
    cached_usage = SimpleNamespace(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    )
    client = FakeInstructorClient(expected, completion=_FakeCompletion(usage=cached_usage))

    result = await summarize_sprint_pace(client, "fake-model", _stats())

    assert result.input_tokens == 200  # 1000 total minus 800 already-cached, not the raw 1000
    assert result.output_tokens == 100


async def test_summarize_sprint_pace_degrades_to_mechanical_summary_on_failure():
    client = FakeInstructorClient(RuntimeError("model unreachable"))
    stats = _stats()

    result = await summarize_sprint_pace(client, "fake-model", stats)

    assert result.degraded is True
    assert "at risk" in result.insight.summary
    assert any("SCRUM-10" in r for r in result.insight.recommendations)
    assert any("SCRUM-14" in r for r in result.insight.recommendations)
    assert result.error == "model unreachable"
    assert result.input_tokens == 0
    assert result.output_tokens == 0
    assert result.estimated_cost_usd == 0.0


async def test_summarize_sprint_pace_fallback_has_no_recommendations_when_nothing_flagged():
    client = FakeInstructorClient(RuntimeError("boom"))
    stats = _stats(blocked_issues=[], stale_issues=[], unestimated_issues=[], risk_level="on_track")

    result = await summarize_sprint_pace(client, "fake-model", stats)

    assert result.insight.recommendations == []
    assert "on track" in result.insight.summary


def test_render_stats_message_includes_flagged_issue_keys():
    stats = _stats()

    message = render_sprint_stats_message(stats)

    assert "SCRUM-10" in message
    assert "SCRUM-14" in message
    assert "at_risk" in message
