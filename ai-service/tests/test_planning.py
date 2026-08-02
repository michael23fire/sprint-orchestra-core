"""Tests for AI-assisted epic planning (app/planning/) — same fake-instructor-client, no-network
approach as tests/test_drafting.py.

Covers: (1) a successful call returns the validated plan untouched and degraded=False; (2) any
failure degrades to a safe single-issue fallback instead of raising; (3) `validate_and_order` breaks
dependency cycles deterministically instead of failing; (4) `allocate_sprints` bin-packs correctly
both in open-ended (capacity-driven) mode and fixed-sprint-count mode.
"""
from app.planning.prompts import render_epic_plan_prompt, render_epic_plan_refine_prompt
from app.planning.schemas import EpicDraft, EpicPlanDraft, IssueDraft
from app.planning.service import allocate_sprints, plan_epic, refine_plan, validate_and_order


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


def _issue(temp_id, depends_on=None, points=None, title=None):
    return IssueDraft(
        temp_id=temp_id,
        title=title or f"Issue {temp_id}",
        description=f"Description for {temp_id}",
        issue_type="task",
        labels=[],
        estimate_story_points=points,
        estimate_rationale="because reasons" if points is not None else None,
        depends_on=depends_on or [],
    )


async def test_plan_epic_returns_validated_plan_on_success():
    expected = EpicPlanDraft(
        epic=EpicDraft(title="Add dark mode", description="Support a dark theme app-wide.", goals=["Toggle in settings"]),
        issues=[_issue("1"), _issue("2", depends_on=["1"])],
    )
    client = FakeInstructorClient(expected)

    result = await plan_epic(client, "fake-model", "Add dark mode support across the app")

    assert result.plan == expected
    assert result.degraded is False
    assert result.error is None
    assert client.completions.calls[0]["response_model"] is EpicPlanDraft


async def test_plan_epic_degrades_safely_when_the_model_call_fails():
    client = FakeInstructorClient(RuntimeError("model unreachable"))

    result = await plan_epic(client, "fake-model", "Add dark mode\nmore detail nobody will read")

    assert result.degraded is True
    assert result.plan.epic.title == "Add dark mode"
    assert len(result.plan.issues) == 1
    assert result.plan.issues[0].estimate_story_points is None
    assert result.error == "model unreachable"


async def test_plan_epic_fallback_handles_blank_proposal():
    client = FakeInstructorClient(RuntimeError("boom"))

    result = await plan_epic(client, "fake-model", "   ")

    assert result.plan.epic.title == "Untitled epic"


def test_render_with_existing_labels_includes_them_joined():
    prompt = render_epic_plan_prompt(["backend", "urgent"])
    assert "backend, urgent" in prompt


async def test_refine_plan_returns_validated_plan_on_success():
    epic = EpicDraft(title="Add dark mode", description="Support a dark theme app-wide.", goals=[])
    issues = [_issue("1")]
    expected = EpicPlanDraft(epic=epic, issues=[_issue("1"), _issue("2", title="Add QA task")])
    client = FakeInstructorClient(expected)

    result = await refine_plan(client, "fake-model", epic, issues, "add a QA task")

    assert result.plan == expected
    assert result.degraded is False
    assert client.completions.calls[0]["response_model"] is EpicPlanDraft
    sent_user_message = client.completions.calls[0]["messages"][1]["content"]
    assert "Add dark mode" in sent_user_message
    assert "Instruction: add a QA task" in sent_user_message


async def test_refine_plan_degrades_to_the_unchanged_input_plan_on_failure():
    epic = EpicDraft(title="Add dark mode", description="Support a dark theme app-wide.", goals=[])
    issues = [_issue("1"), _issue("2", depends_on=["1"])]
    client = FakeInstructorClient(RuntimeError("model unreachable"))

    result = await refine_plan(client, "fake-model", epic, issues, "add a QA task")

    assert result.degraded is True
    assert result.plan.epic == epic
    assert result.plan.issues == issues
    assert result.error == "model unreachable"


def test_render_refine_prompt_with_existing_labels_includes_them_joined():
    prompt = render_epic_plan_refine_prompt(["backend", "urgent"])
    assert "backend, urgent" in prompt


def test_validate_and_order_respects_dependencies():
    issues = [_issue("2", depends_on=["1"]), _issue("1"), _issue("3", depends_on=["2"])]

    ordered = validate_and_order(issues)

    assert [i.temp_id for i in ordered] == ["1", "2", "3"]


def test_validate_and_order_drops_unknown_temp_id_reference():
    issues = [_issue("1", depends_on=["does-not-exist"])]

    ordered = validate_and_order(issues)

    assert ordered[0].depends_on == []


def test_validate_and_order_breaks_a_cycle_instead_of_hanging():
    issues = [_issue("1", depends_on=["2"]), _issue("2", depends_on=["1"])]

    ordered = validate_and_order(issues)

    assert {i.temp_id for i in ordered} == {"1", "2"}
    # the cycle must be broken: at least one edge involved in it no longer round-trips
    remaining_edges = {i.temp_id: set(i.depends_on) for i in ordered}
    assert not (remaining_edges["1"] == {"2"} and remaining_edges["2"] == {"1"})


def test_allocate_sprints_packs_by_capacity():
    issues = [_issue("1", points=5), _issue("2", points=5), _issue("3", points=5)]

    buckets = allocate_sprints(issues, sprint_capacity_points=8, target_sprint_count=None)

    assert [b.issue_temp_ids for b in buckets] == [["1"], ["2"], ["3"]]
    assert [b.total_points for b in buckets] == [5, 5, 5]


def test_allocate_sprints_packs_multiple_issues_into_one_bucket_when_capacity_allows():
    issues = [_issue("1", points=3), _issue("2", points=3), _issue("3", points=3)]

    buckets = allocate_sprints(issues, sprint_capacity_points=8, target_sprint_count=None)

    assert [b.issue_temp_ids for b in buckets] == [["1", "2"], ["3"]]


def test_allocate_sprints_places_unestimated_issues_without_blocking_on_missing_points():
    issues = [_issue("1", points=None), _issue("2", points=5)]

    buckets = allocate_sprints(issues, sprint_capacity_points=5, target_sprint_count=None)

    assert [b.issue_temp_ids for b in buckets] == [["1", "2"]]
    assert buckets[0].total_points == 5


def test_allocate_sprints_respects_target_sprint_count():
    issues = [_issue(str(i), points=2) for i in range(1, 7)]  # 6 issues, 12 points total

    buckets = allocate_sprints(issues, sprint_capacity_points=None, target_sprint_count=3)

    assert len(buckets) == 3
    assert sum(len(b.issue_temp_ids) for b in buckets) == 6


def test_allocate_sprints_returns_empty_list_for_no_issues():
    assert allocate_sprints([], sprint_capacity_points=10) == []
