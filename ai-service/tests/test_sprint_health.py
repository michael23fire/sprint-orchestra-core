"""Tests for AI-assisted sprint health readouts (app/sprint_health/) — same fake-instructor-client,
no-network approach as tests/test_drafting.py and tests/test_planning.py.

Covers: (1) a successful call returns the validated insight untouched; (2) a failure degrades to a
mechanically-assembled summary (no LLM) built directly from the stats, never a raised exception.
"""
from app.sprint_health.prompts import render_sprint_stats_message
from app.sprint_health.schemas import FlaggedIssue, SprintHealthInsight, SprintStats
from app.sprint_health.service import summarize_sprint_health


class _FakeCompletions:
    def __init__(self, result):
        self._result = result
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class _FakeChat:
    def __init__(self, completions):
        self.completions = completions


class FakeInstructorClient:
    def __init__(self, result):
        self.completions = _FakeCompletions(result)
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


async def test_summarize_sprint_health_returns_validated_insight_on_success():
    expected = SprintHealthInsight(summary="Behind pace but recoverable.", recommendations=["Unblock SCRUM-10"])
    client = FakeInstructorClient(expected)
    stats = _stats()

    result = await summarize_sprint_health(client, "fake-model", stats)

    assert result.insight == expected
    assert result.degraded is False
    assert result.error is None
    assert client.completions.calls[0]["response_model"] is SprintHealthInsight


async def test_summarize_sprint_health_degrades_to_mechanical_summary_on_failure():
    client = FakeInstructorClient(RuntimeError("model unreachable"))
    stats = _stats()

    result = await summarize_sprint_health(client, "fake-model", stats)

    assert result.degraded is True
    assert "at risk" in result.insight.summary
    assert any("SCRUM-10" in r for r in result.insight.recommendations)
    assert any("SCRUM-14" in r for r in result.insight.recommendations)
    assert result.error == "model unreachable"


async def test_summarize_sprint_health_fallback_has_no_recommendations_when_nothing_flagged():
    client = FakeInstructorClient(RuntimeError("boom"))
    stats = _stats(blocked_issues=[], stale_issues=[], unestimated_issues=[], risk_level="on_track")

    result = await summarize_sprint_health(client, "fake-model", stats)

    assert result.insight.recommendations == []
    assert "on track" in result.insight.summary


def test_render_stats_message_includes_flagged_issue_keys():
    stats = _stats()

    message = render_sprint_stats_message(stats)

    assert "SCRUM-10" in message
    assert "SCRUM-14" in message
    assert "at_risk" in message
