"""Tests for the multi-agent planning graph (app/planning/graph.py).

The per-node LLM functions themselves are already covered in tests/test_planning.py; what's tested
here is what LangGraph actually owns — the routing. Specifically: that each failure edge terminates
early with the right `degraded_reason`, that the loop-back edge fires *because of* a critique (not
coincidentally), and that it terminates at MAX_REVISION_ROUNDS without falsely reporting `degraded`.

Reuses tests/test_planning.py's FakeInstructorClient — its `_FakeCompletions` takes a
{response_model: [result, ...]} dict specifically so planner and critic can return a different answer
on each round of a revision loop.
"""
from app.planning.graph import (
    MAX_REVISION_ROUNDS,
    build_planning_graph,
    plan_epic_multiagent,
)
from app.planning.schemas import EpicDraft, EpicPlanDraft, IssueDraft, PlanCritique
from tests.test_planning import FakeInstructorClient

_EPIC = EpicDraft(title="Add dark mode", description="Support a dark theme app-wide.", goals=[])


def _issue(temp_id, title=None, points=None):
    return IssueDraft(
        temp_id=temp_id,
        title=title or f"Issue {temp_id}",
        description=f"Description for {temp_id}",
        issue_type="task",
        labels=[],
        estimate_story_points=points,
        estimate_rationale="because reasons" if points is not None else None,
        depends_on=[],
    )


def _plan(*issues):
    return EpicPlanDraft(epic=_EPIC, issues=list(issues))


_APPROVED = PlanCritique()
_HAS_FINDING = PlanCritique(coverage_gaps=["no issue covers data migration"])


async def test_happy_path_runs_both_nodes_once_and_is_not_degraded():
    client = FakeInstructorClient(
        {
            EpicPlanDraft: [_plan(_issue("1", points=5), _issue("2", points=5))],
            PlanCritique: [_APPROVED],
        }
    )

    result = await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert result.degraded is False
    assert result.error is None
    assert [i.estimate_story_points for i in result.plan.issues] == [5, 5]
    assert [c["response_model"] for c in client.completions.calls] == [EpicPlanDraft, PlanCritique]


async def test_planner_failure_ends_immediately_without_critiquing():
    client = FakeInstructorClient(
        {EpicPlanDraft: RuntimeError("model unreachable"), PlanCritique: [_APPROVED]}
    )

    result = await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert result.degraded is True
    assert result.error == "model unreachable"
    # exactly one call: the planner's. The critic must not run against a stub fallback plan.
    assert len(client.completions.calls) == 1
    assert client.completions.calls[0]["response_model"] is EpicPlanDraft


async def test_critic_failure_does_not_trigger_a_revision_round():
    """A broken reviewer must not be able to force revision work — it reports degraded and stops."""
    client = FakeInstructorClient(
        {EpicPlanDraft: [_plan(_issue("1"))], PlanCritique: RuntimeError("critic down")}
    )

    result = await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert result.degraded is True
    assert result.error == "critic down"
    assert len(client.completions.calls) == 2  # no loop-back


async def test_a_critique_loops_back_to_the_planner_and_the_revised_plan_is_what_is_returned():
    """The core claim of the whole design: the second planner call happens *because of* the critique.
    Proven by the returned plan being the revised one, plus an exact call count — 4, i.e. two full
    passes — rather than just "more than 2".
    """
    revised = _plan(_issue("1"), _issue("2", title="Migrate existing rows"))
    client = FakeInstructorClient(
        {
            EpicPlanDraft: [_plan(_issue("1")), revised],
            PlanCritique: [_HAS_FINDING, _APPROVED],
        }
    )

    result = await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert [i.title for i in result.plan.issues] == ["Issue 1", "Migrate existing rows"]
    assert result.degraded is False
    assert len(client.completions.calls) == 4
    # the revision pass must have gone through the refine prompt, carrying the critique's finding
    revision_call = client.completions.calls[2]
    assert "no issue covers data migration" in revision_call["messages"][1]["content"]


async def test_the_revision_loop_stops_at_the_cap_without_reporting_degraded():
    """A critic that never approves must not loop forever — and hitting the cap is a budget outcome,
    not a component failure, so `degraded` (what the frontend's warning banner keys on) stays False.
    """
    rounds = MAX_REVISION_ROUNDS + 2
    client = FakeInstructorClient(
        {
            EpicPlanDraft: [_plan(_issue("1"))] * rounds,
            PlanCritique: [_HAS_FINDING] * rounds,
        }
    )

    result = await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert result.degraded is False
    assert result.error is None
    # (1 initial + MAX_REVISION_ROUNDS revision) passes, 2 calls each
    assert len(client.completions.calls) == 2 * (MAX_REVISION_ROUNDS + 1)


async def test_the_planner_prompt_still_asks_for_estimates_after_the_estimator_node_was_cut():
    """Guards the deletion: when the separate estimator node was removed (delta 0.00, see
    eval/results/planning_multiagent_comparison.md), the planner had to go back to estimating. If a
    stale "leave estimates null" instruction survived, every multi-agent plan would silently come back
    unestimated and sprint allocation would pack on zeros.
    """
    client = FakeInstructorClient(
        {EpicPlanDraft: [_plan(_issue("1"))], PlanCritique: [_APPROVED]}
    )

    await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    planner_system_prompt = client.completions.calls[0]["messages"][0]["content"]
    assert "Only set an estimate if there's enough signal" in planner_system_prompt
    assert "Do NOT estimate" not in planner_system_prompt


async def test_revision_rounds_are_recorded_as_a_metric():
    from app.observability import PLANNING_REVISION_ROUNDS

    baseline = PLANNING_REVISION_ROUNDS._sum.get()
    client = FakeInstructorClient(
        {
            EpicPlanDraft: [_plan(_issue("1")), _plan(_issue("1"), _issue("2"))],
            PlanCritique: [_HAS_FINDING, _APPROVED],
        }
    )

    await plan_epic_multiagent(build_planning_graph(client, "fake-model"), "Add dark mode")

    assert PLANNING_REVISION_ROUNDS._sum.get() == baseline + 1
