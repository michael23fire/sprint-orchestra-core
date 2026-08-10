"""Tests for the sprint-recovery durable workflow (app/sprint_recovery/graph.py).

Same fake-instructor-client approach as tests/test_planning.py (imported directly, not copy-pasted)
plus a fake retrieval client (mirrors RetrievalClient's dataclass return shapes) and a fake jira
actions client (mirrors JiraActionsClient). InMemorySaver for fast mechanism tests;
`test_crash_mid_commit_resumes_without_duplicate_actions` uses a real AsyncPostgresSaver, same
reasoning as `tests/test_rollout_graph.py`'s crash test.
"""
import asyncio
import uuid
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.auth.space_membership import NoopSpaceMembershipChecker, SpaceMembershipError
from app.sprint_recovery import graph as graph_module
from app.llm.types import Usage
from app.sprint_recovery.graph import (
    _TOKENS_PER_LLM_CALL_ESTIMATE,
    _await_index_catch_up,
    _describe_action,
    _detect_risk_signals,
    _gather_evidence,
    _last_update_not_by_this_workflow,
    build_escalation_summary,
    build_sprint_recovery_graph,
    flatten_escalation_summary,
    list_checkpoint_history,
    plan_node,
    reevaluate_node,
    retry_recovery_commit,
    time_travel_resume,
    trigger_reevaluation,
)
from app.sprint_recovery.jira_actions_client import JiraActionError
from app.sprint_recovery.schemas import (
    DiagnosisResult,
    RecoveryAction,
    RecoveryPlan,
    RecoveryPlanSet,
    RootCauseHypothesis,
)
from app.sprint_recovery.state import initial_recovery_state
from tests.test_planning import FakeInstructorClient


class FakeRetrieval:
    """Configurable fake for the 4 retrieval calls diagnose/reevaluate use. `issue_rows` and
    `still_at_risk` are mutable attributes so a single test can change what the *next* call returns
    (e.g. to simulate "the sprint improved after actions were committed").
    """

    def __init__(self, issue_rows=None, comments=None, details=None, attachments=None, history=None, sprints=None):
        self.issue_rows = issue_rows if issue_rows is not None else []
        self.sprints = sprints if sprints is not None else []
        # Default mentions PAY-97 in free text (not as its own flagged risk row) — every fixture that
        # doesn't override this exercises the realistic "undocumented dependency mentioned in a
        # comment, never independently flagged" case plan_node's issue-key extraction needs to handle.
        self.comments = comments if comments is not None else [{"issue_key": "PAY-142", "content": "waiting on PAY-97"}]
        self.details = details or []
        self.attachments = attachments or []
        self.history = history or []
        self.query_issues_calls = 0

    async def query_issues(self, space_ids, filters):
        self.query_issues_calls += 1
        counts_by_status = {}
        for row in self.issue_rows:
            counts_by_status[row.get("status", "")] = counts_by_status.get(row.get("status", ""), 0) + 1
        return type("R", (), {
            "total_count": len(self.issue_rows), "counts_by_status": counts_by_status,
            "counts_by_type": {}, "issues": self.issue_rows,
        })()

    async def query_issue_history(self, space_ids, filters):
        return type("R", (), {"total_count": len(self.history), "changes": self.history})()

    async def get_issue_comments(self, space_ids, issue_keys, limit=200):
        return type("R", (), {"total_count": len(self.comments), "comments": self.comments})()

    async def get_issue_details(self, space_ids, issue_keys, limit=200):
        return type("R", (), {"total_count": len(self.details), "details": self.details})()

    async def get_issue_attachments(self, space_ids, issue_keys, limit=200):
        return type("R", (), {"total_count": len(self.attachments), "attachments": self.attachments})()

    async def query_sprints(self, space_ids, filters):
        return type("R", (), {"total_count": len(self.sprints), "sprints": self.sprints})()


class FakeJiraActions:
    def __init__(self, fail_on_call: int = None, raise_type=JiraActionError, updated_at=None):
        self.calls = []
        self.idempotency_keys = []
        self._fail_on_call = fail_on_call
        self._raise_type = raise_type
        # None => no watermark recorded, so `_await_index_catch_up` has nothing to wait on. Tests that
        # care about the catch-up wait set this explicitly.
        self._updated_at = updated_at

    async def issue_updated_at(self, space_id, issue_key, user_id, username):
        return self._updated_at

    async def execute(self, space_id, action, user_id, username, idempotency_key=None):
        self.calls.append((action.action_type, action.target_issue_key))
        self.idempotency_keys.append(idempotency_key)
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise self._raise_type(f"simulated failure on call {len(self.calls)}")
        return f"{action.action_type} on {action.target_issue_key} done"


_AT_RISK_ROWS = [{"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-07-20T00:00:00Z"}]
_HEALTHY_ROWS = [{"issue_key": "PAY-142", "status": "done", "updated_at": "2026-08-01T00:00:00Z"}] * 8
_AT_RISK_TWO_ISSUES_ROWS = [
    {"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-07-20T00:00:00Z"},
    {"issue_key": "PAY-99", "status": "in_progress", "updated_at": "2026-07-20T00:00:00Z"},
]


def _hyp(evidence_ids, confidence="high"):
    return RootCauseHypothesis(
        statement="PAY-142 is delayed by an undocumented dependency on PAY-97.",
        confidence=confidence, supporting_evidence_ids=evidence_ids,
    )


def _plan_set(actions=None):
    actions = actions or [
        RecoveryAction(action_type="link_dependency", target_issue_key="PAY-142", depends_on_issue_key="PAY-97"),
        RecoveryAction(action_type="change_priority", target_issue_key="PAY-97", new_priority="highest"),
    ]
    return RecoveryPlanSet(plans=[
        RecoveryPlan(plan_id="plan_a", name="Unblock via dependency", rationale="r", impact_on_goal="keeps goal", actions=actions),
        RecoveryPlan(plan_id="plan_b", name="Add resource", rationale="r2", impact_on_goal="costs a person", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="pairing another engineer"),
        ]),
    ])


def _client(diagnosis, plans):
    return FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [plans]})


def _graph(client, retrieval, jira, space_membership=None):
    return build_sprint_recovery_graph(
        client, "fake-model", space_membership or NoopSpaceMembershipChecker(), retrieval, jira,
    ).compile(checkpointer=InMemorySaver())


def _state():
    return initial_recovery_state(
        5000014, 7, "Sprint 7", "u1", "alice",
        max_clarification_rounds=1, max_escalation_rounds=1, max_plan_revision_rounds=1,
    )


def _cfg():
    return {"configurable": {"thread_id": str(uuid.uuid4())}}


async def test_gather_evidence_uses_description_for_non_field_changes_and_drops_issue_order():
    """Found live against real AtlasCart data: vectorization-service's /issues/history returns rows
    where field_name/from_value/to_value are all null but `description` carries the real content
    (issue_created, comment_created, code_link_added, ...) — the old unconditional f-string template
    rendered these as literal "None: None -> None" evidence. Also: issueOrder (backlog drag-and-drop
    position) is real field-change data but pure noise, never diagnostically relevant.
    """
    retrieval = FakeRetrieval(
        history=[
            {"issue_key": "PAY-1", "field_name": None, "from_value": None, "to_value": None,
             "event_type": "code_link_added", "description": "linked pull request 119", "changed_at": "t1"},
            {"issue_key": "PAY-1", "field_name": None, "from_value": None, "to_value": None,
             "event_type": None, "description": None, "changed_at": "t2"},  # truly nothing to say
            {"issue_key": "PAY-1", "field_name": "issueOrder", "from_value": "1", "to_value": "2", "changed_at": "t3"},
            {"issue_key": "PAY-1", "field_name": "status", "from_value": "planned", "to_value": "in_progress", "changed_at": "t4"},
        ],
    )
    evidence = await _gather_evidence(retrieval, 5000014, 7, ["PAY-1"])
    history_items = [e for e in evidence if e.source_type == "history"]
    assert len(history_items) == 2  # the null-everything row and issueOrder are both dropped
    assert history_items[0].content == "code_link_added: linked pull request 119 (t1)"
    assert history_items[1].content == "status: planned -> in_progress (t4)"
    assert "None: None -> None" not in " ".join(e.content for e in history_items)
    assert not any("issueOrder" in e.content for e in history_items)


async def test_happy_path_diagnose_plan_approve_commit_reevaluate_recovered():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS), comments=[{"issue_key": "PAY-142", "content": "waiting on PAY-97"}])
    jira = FakeJiraActions()
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert "__interrupt__" in r1
    assert r1["status"] == "awaiting_plan_approval"
    assert {p["plan_id"] for p in r1["__interrupt__"][0].value["plans"]} == {"plan_a", "plan_b"}

    r2 = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert "__interrupt__" in r2  # paused again, inside wait_for_reevaluation
    assert r2["status"] == "waiting_reevaluation"  # set by commit_one_action once every action succeeded
    assert jira.calls == [("link_dependency", "PAY-142"), ("change_priority", "PAY-97")]

    # sprint has improved by the time we re-check
    retrieval.issue_rows = list(_HEALTHY_ROWS)
    r3 = await trigger_reevaluation(graph, cfg["configurable"]["thread_id"], source="manual")
    assert r3["status"] == "recovered"


async def test_confidence_gate_asks_one_clarifying_question_then_proceeds():
    low_conf = DiagnosisResult(
        hypotheses=[_hyp(["ev1"], confidence="low")], overall_confidence="insufficient",
        clarifying_question="Was PAY-97 actually deprioritized in a Slack discussion?",
    )
    high_conf = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    client = FakeInstructorClient({DiagnosisResult: [low_conf, high_conf], RecoveryPlanSet: [_plan_set()]})
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    graph = _graph(client, retrieval, FakeJiraActions())
    cfg = _cfg()

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert r1["__interrupt__"][0].value["question"] == low_conf.clarifying_question
    assert r1["clarification_rounds"] == 0

    r2 = await graph.ainvoke(Command(resume={"answer": "No — it's still required, just undocumented."}), config=cfg)
    assert r2["clarification_rounds"] == 1
    assert r2["status"] == "awaiting_plan_approval"  # proceeded to plan after the second, confident diagnosis


async def test_grounding_drops_hypothesis_citing_unknown_evidence_id():
    diagnosis = DiagnosisResult(
        hypotheses=[_hyp(["ev1"]), _hyp(["not-a-real-id"])], overall_confidence="sufficient",
    )
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS), comments=[{"issue_key": "PAY-142", "content": "note"}])
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, FakeJiraActions())
    cfg = _cfg()

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert len(r1["hypotheses"]) == 1  # the ungrounded one was dropped, not the whole diagnosis rejected


async def test_reject_never_executes_any_action():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "reject"}), config=cfg)
    assert result["status"] == "rejected"
    assert jira.calls == []


async def test_revise_with_feedback_loops_back_and_produces_new_plans():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    plan_v2 = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_c", name="Revised per human feedback", rationale="addresses the feedback",
        impact_on_goal="different approach", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="revised"),
        ],
    )])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set(), plan_v2]})
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()

    r1 = await graph.ainvoke(_state(), config=cfg)
    assert {p["plan_id"] for p in r1["__interrupt__"][0].value["plans"]} == {"plan_a", "plan_b"}

    r2 = await graph.ainvoke(
        Command(resume={"decision": "revise", "feedback": "Neither plan touches PAY-97 directly."}), config=cfg,
    )
    assert "__interrupt__" in r2  # paused again at approval, this time with the NEW plans
    assert r2["status"] == "awaiting_plan_approval"
    assert r2["plan_revision_round"] == 1
    assert {p.plan_id for p in r2["plans"]} == {"plan_c"}
    assert jira.calls == []  # revise never reaches commit — nothing was executed

    r3 = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_c"}), config=cfg)
    assert jira.calls == [("add_comment", "PAY-142")]
    assert r3["status"] == "waiting_reevaluation"


async def test_revise_past_the_cap_degrades_to_reject():
    """_state() caps max_plan_revision_rounds=1: round 0's revise (0 < 1) succeeds and produces new
    plans; a second revise attempt (1 >= 1) degrades to a plain reject instead of looping forever.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set(), _plan_set()]})
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    r2 = await graph.ainvoke(Command(resume={"decision": "revise", "feedback": "try again"}), config=cfg)
    assert r2["status"] == "awaiting_plan_approval"
    assert r2["plan_revision_round"] == 1

    r3 = await graph.ainvoke(Command(resume={"decision": "revise", "feedback": "one more try"}), config=cfg)
    assert r3["status"] == "rejected"
    assert "revision limit" in r3["error"]
    assert jira.calls == []


async def test_revise_without_feedback_text_is_treated_as_reject():
    """Backstop for direct callers bypassing the HTTP layer's own `feedback` validation (see
    sprint_recovery_routes.py's DecisionRequest) — approval_node itself must not loop on empty feedback.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "revise"}), config=cfg)  # no feedback key at all
    assert result["status"] == "rejected"
    assert jira.calls == []


async def test_edit_commits_the_human_modified_action_list_not_the_generated_one():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    edited_actions = [{"action_type": "add_comment", "target_issue_key": "PAY-142", "comment_body": "human-written note"}]
    await graph.ainvoke(
        Command(resume={"decision": "edit", "plan_id": "plan_a", "actions": edited_actions}), config=cfg,
    )
    assert jira.calls == [("add_comment", "PAY-142")]


async def test_rbac_recheck_failure_rejects_without_committing():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()

    class _AlwaysDenies:
        async def validate(self, user_id, username, space_ids):
            raise SpaceMembershipError(user_id, space_ids)

    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira, space_membership=_AlwaysDenies())
    cfg = _cfg()
    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert result["status"] == "rejected"
    assert jira.calls == []


async def test_escalation_loop_replans_once_then_stops_at_the_cap():
    """max_escalation_rounds=1 (see _state()): round 0's reevaluate finds still-at-risk (0 < cap 1) ->
    replans once, escalation_round becomes 1; round 1's reevaluate is still at risk but escalation_round
    (1) is no longer < cap (1) -> 'escalated', not a third replan.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))  # never improves
    jira = FakeJiraActions()
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set(), _plan_set()]})
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    r2 = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert r2["status"] == "waiting_reevaluation"

    # Round 0 reevaluate: still at risk, 0 < max_escalation_rounds(1) -> replans, pauses at approval again.
    r3 = await trigger_reevaluation(graph, thread_id, source="manual")
    assert "__interrupt__" in r3
    assert r3["escalation_round"] == 1

    r4 = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert r4["status"] == "waiting_reevaluation"

    # Round 1 reevaluate: still at risk, but 1 is no longer < max_escalation_rounds(1) -> escalated, terminal.
    r5 = await trigger_reevaluation(graph, thread_id, source="manual")
    assert r5["status"] == "escalated"
    assert r5["escalation_round"] == 1


async def test_build_escalation_summary_covers_every_round_and_the_final_risk_reason():
    """Same two-round-then-cap shape as the test above, but with a DIFFERENT plan approved each round
    (unlike that test's plan_a/plan_a) so the summary's per-round distinctness is actually exercised,
    not just its round-count.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))  # never improves
    jira = FakeJiraActions()
    plan_round_0 = _plan_set()  # "Unblock via dependency" (plan_a) / "Add resource" (plan_b)
    plan_round_1 = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Escalate to a second engineer", rationale="round 0 plan did not resolve it",
        impact_on_goal="costs a second person", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="pairing"),
        ],
    )])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [plan_round_0, plan_round_1]})
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    await trigger_reevaluation(graph, thread_id, source="manual")
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    final = await trigger_reevaluation(graph, thread_id, source="manual")
    assert final["status"] == "escalated"

    summary = await build_escalation_summary(graph, thread_id)
    assert [r["round"] for r in summary["rounds"]] == [1, 2]
    assert summary["rounds"][0]["plan_name"] == "Unblock via dependency"
    assert summary["rounds"][1]["plan_name"] == "Escalate to a second engineer"
    assert summary["still_at_risk_reasons"]

    # Each action must be auditable on its own terms — a human reading this handoff has to see what
    # was actually written to Jira, not just that *something* was. Found live: the round-2 entry read
    # "comment added to PAY-142", which never showed the reviewer the comment's text.
    assert summary["rounds"][0]["actions"] == [
        "Marked PAY-142 as blocked by PAY-97", "Raised PAY-97 priority to highest",
    ]
    assert summary["rounds"][1]["actions"] == ['Commented on PAY-142: "pairing"']

    # PAY-142 (the only flagged issue here) was acted on in both rounds — a focused handoff, not an
    # unresolved one. See test_build_escalation_summary_flags_issues_that_were_never_attempted below
    # for the other case.
    assert summary["unaddressed_issue_keys"] == []

    flat = flatten_escalation_summary(summary)
    assert "Round 1" in flat and "Unblock via dependency" in flat
    assert "Round 2" in flat and "Escalate to a second engineer" in flat
    assert "Still at risk because" in flat
    assert "Never got attempted" not in flat


async def test_build_escalation_summary_flags_issues_that_were_never_attempted():
    """Distinguishes a 'focused' handoff (every flagged issue was acted on at least once) from an
    unresolved one — see `build_escalation_summary`'s docstring. PAY-99 is flagged at every risk check
    here but no plan across either round ever targets it, only PAY-142.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_TWO_ISSUES_ROWS))  # never improves
    jira = FakeJiraActions()
    plan_round_0 = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Unblock PAY-142", rationale="r", impact_on_goal="keeps goal",
        actions=[RecoveryAction(action_type="change_priority", target_issue_key="PAY-142", new_priority="highest")],
    )])
    plan_round_1 = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Comment on PAY-142 again", rationale="round 0 plan did not resolve it",
        impact_on_goal="keeps goal",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="status?")],
    )])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [plan_round_0, plan_round_1]})
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    await trigger_reevaluation(graph, thread_id, source="manual")
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    final = await trigger_reevaluation(graph, thread_id, source="manual")
    assert final["status"] == "escalated"

    summary = await build_escalation_summary(graph, thread_id)
    assert summary["unaddressed_issue_keys"] == ["PAY-99"]

    flat = flatten_escalation_summary(summary)
    assert "Never got attempted: PAY-99" in flat


async def test_token_budget_exhausted_before_diagnosis_fails_cleanly():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, FakeJiraActions())
    cfg = _cfg()

    state = initial_recovery_state(5000014, 7, "Sprint 7", "u1", "alice", max_token_budget=100)  # too small for even 1 call
    result = await graph.ainvoke(state, config=cfg)
    assert result["status"] == "failed"
    assert "budget" in result["error"]


async def test_action_failure_mid_commit_then_retry_recovery_commit_finishes():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions(fail_on_call=2)  # 2nd action fails once
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    failed = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert failed["status"] == "failed"
    assert len(failed["committed_actions"]) == 1

    result = await retry_recovery_commit(graph, thread_id)
    assert "__interrupt__" in result  # reached wait_for_reevaluation
    assert len(result["committed_actions"]) == 2
    assert jira.calls == [("link_dependency", "PAY-142"), ("change_priority", "PAY-97"), ("change_priority", "PAY-97")]
    # **Found live**: the failed attempt and the retry both act on the same (thread, round, action index)
    # — jira-backend's IdempotencyFilter can only recognize the retry as a repeat if the key is identical
    # both times, not freshly generated per call.
    assert jira.idempotency_keys[1] == jira.idempotency_keys[2]
    assert jira.idempotency_keys[0] != jira.idempotency_keys[1]  # different action index, different key


async def test_approval_captures_every_action_targets_baseline_up_front_before_any_commit():
    """**Found live in review, empirically reproduced with a real Postgres crash-resume harness (see
    `test_a_genuine_crash_between_a_write_landing_and_the_checkpoint_does_not_poison_the_baseline`
    below)**: capturing each issue's pre-write baseline lazily inside `commit_one_action_node` — right
    before that specific write — has a gap a caught `JiraActionError` can't close: a genuine process
    crash between the write landing on jira-backend and the node returning (so LangGraph never
    checkpoints that attempt) means a resumed attempt recomputes the baseline via a GET that now runs
    *after* the crashed write already landed, reintroducing the false-`recovered` bug through the
    resume path. Fixed by capturing every target/dependency issue's baseline in `approval_node`,
    on the final (post-edit) action list, before commit_one_action_node ever runs for any of them —
    this test locks in that both actions' baselines land in the SAME checkpoint approval_node produces,
    not spread across each action's own commit step.
    """
    reads_by_key = {
        "PAY-142": iter(["2026-08-01T00:00:00Z"]), "PAY-97": iter(["2026-07-30T00:00:00Z"]),
    }

    class _DriftingJira(FakeJiraActions):
        async def issue_updated_at(self, space_id, issue_key, user_id, username):
            seq = reads_by_key.setdefault(issue_key, iter([]))
            # A read past the first one for a key means something re-fetched a baseline that should
            # already have been captured at approval time — return an obviously-wrong sentinel so a
            # regression shows up as a wrong value, not a silent pass.
            return next(seq, "1999-01-01T00:00:00Z")

    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = _DriftingJira(fail_on_call=1)  # the very first commit attempt still fails
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    failed = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert failed["status"] == "failed"
    # Both actions' baselines are already present after approval — PAY-97's included, even though its
    # own commit step hasn't even been attempted yet.
    assert failed["pre_write_updated_at"] == {
        "PAY-142": "2026-08-01T00:00:00Z", "PAY-97": "2026-07-30T00:00:00Z",
    }

    result = await retry_recovery_commit(graph, thread_id)
    assert "__interrupt__" in result
    # Unchanged by the retry — nothing re-fetched them.
    assert result["pre_write_updated_at"] == {
        "PAY-142": "2026-08-01T00:00:00Z", "PAY-97": "2026-07-30T00:00:00Z",
    }


async def test_a_genuine_crash_between_a_write_landing_and_the_checkpoint_does_not_poison_the_baseline():
    """The real fault-injection reproduction of the bug the commit above fixes — same two-process,
    real `AsyncPostgresSaver` shape as `test_crash_mid_commit_resumes_without_duplicate_actions`, with
    a shared `real_world` dict standing in for jira-backend's actual database (the one thing both
    "processes" have in common — a crash resets ai-service's in-memory progress, not jira-backend's).
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = await psycopg.AsyncConnection.connect(
            "postgresql://poc:poc123@localhost:5432/pocdb", connect_timeout=2,
        )
        await conn.close()
    except Exception:
        pytest.skip("dev Postgres (poc-postgres) not reachable")

    from app.planning.checkpoint import build_checkpointer
    from contextlib import AsyncExitStack

    db_url = "postgresql://poc:poc123@localhost:5432/pocdb"
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    thread_id = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": thread_id}}

    class _SimulatedCrash(Exception):
        pass

    real_world = {"PAY-142": "2026-08-01T00:00:00Z", "PAY-97": "2026-08-01T00:00:00Z"}

    class _RealisticCrashJira:
        def __init__(self):
            self.calls = []

        async def issue_updated_at(self, space_id, issue_key, user_id, username):
            return real_world.get(issue_key)

        async def execute(self, space_id, action, user_id, username, idempotency_key=None):
            self.calls.append((action.action_type, action.target_issue_key))
            if len(self.calls) == 2:
                # The write actually lands on jira-backend's DB (what @PreUpdate does) before the
                # process dies — the same ambiguity the idempotency key exists for, via a crash
                # instead of a client-observed timeout.
                real_world[action.target_issue_key] = "2026-08-09T12:00:00Z"
                raise _SimulatedCrash("process died right after the write landed, before checkpointing")
            return f"{action.action_type} on {action.target_issue_key} done"

    stack1 = AsyncExitStack()
    try:
        checkpointer1 = await build_checkpointer(db_url, stack1)
        graph1 = build_sprint_recovery_graph(
            _client(diagnosis, _plan_set()), "fake-model", NoopSpaceMembershipChecker(),
            FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), _RealisticCrashJira(),
        ).compile(checkpointer=checkpointer1)

        await graph1.ainvoke(_state(), config=cfg)
        with pytest.raises(_SimulatedCrash):
            await graph1.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)

        mid_state = await graph1.aget_state(cfg)
        # Captured at approval time, before either action was attempted — survives the crash untouched.
        assert mid_state.values["pre_write_updated_at"] == {
            "PAY-142": "2026-08-01T00:00:00Z", "PAY-97": "2026-08-01T00:00:00Z",
        }
    finally:
        await stack1.aclose()

    stack2 = AsyncExitStack()
    try:
        checkpointer2 = await build_checkpointer(db_url, stack2)
        graph2 = build_sprint_recovery_graph(
            _client(diagnosis, _plan_set()), "fake-model", NoopSpaceMembershipChecker(),
            FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), _RealisticCrashJira(),
        ).compile(checkpointer=checkpointer2)

        final = await graph2.ainvoke(None, config=cfg)
        assert "__interrupt__" in final
        # The critical assertion: PAY-97's baseline is still the TRUE pre-write value, even though the
        # crashed write already landed in real_world before this resumed attempt ever ran.
        assert final["pre_write_updated_at"]["PAY-97"] == "2026-08-01T00:00:00Z"
    finally:
        await stack2.aclose()
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                await conn.execute(
                    f"DELETE FROM planning_workflows.{table} WHERE thread_id = %s", (thread_id,)
                )


async def test_retry_recovery_commit_resumes_a_crash_between_diagnose_and_plan_without_replaying_stale_actions():
    """**Found live**, via a real UI crash-resume demo: a kill -9 landed between `reevaluate_node`
    looping back into `diagnose_node` for the next escalation round (which completed and checkpointed)
    and `plan_node` finishing (which did not). The resulting checkpoint had `status == "diagnosing"`
    and a real pending task (`next == ("plan",)`), but `approved_plan` still held the *previous*
    round's plan — already fully committed to Jira, never cleared because nothing between
    `reevaluate_node` and a completed `plan_node` touches that field.

    Before `is_resumable_after_a_crash`/`_has_uninterrupted_pending_task` existed, `retry_recovery_commit`
    had no case for `status == "diagnosing"`: it fell through to the `approved_plan is not None` branch,
    which would have re-armed `commit_one_action_node` against those already-committed actions —
    reposting the exact same Jira comment a second time. The fix checks LangGraph's own pending-task/
    interrupt state instead of a status string, so this now takes the plain bare-`ainvoke` resume path
    and correctly re-enters `plan_node`, not `commit_one_action_node`.
    """
    stale_committed_plan = RecoveryPlan(
        plan_id="plan_a", name="Round 1 plan (already fully committed)", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="round 1")],
    )
    round_2_plans = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_b", name="Round 2 plan", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-142", comment_body="round 2")],
    )])
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set(), round_2_plans]})
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    jira = FakeJiraActions()
    graph = _graph(client, retrieval, jira)
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)  # reaches awaiting_plan_approval, round 0 — no Jira calls yet

    # Simulate the exact live crash: a checkpoint at status="diagnosing" for round 2, with `plan` as
    # the only pending task, but `approved_plan`/`committed_actions` still reflecting round 1's
    # already-finished commit — `commit_one_action_node` was never re-entered, this is what
    # `reevaluate_node` -> `diagnose_node` alone would leave behind if killed right after.
    await graph.aupdate_state(
        cfg,
        {
            "status": "diagnosing", "escalation_round": 1, "committed_actions": {},
            "approved_plan": stale_committed_plan, "clarification_question": None,
        },
        as_node="diagnose",
    )
    snap = await graph.aget_state(cfg)
    assert snap.next == ("plan",)  # confirms this fixture matches the live bug's exact shape

    result = await retry_recovery_commit(graph, thread_id)

    assert jira.calls == []  # must NOT have re-posted round 1's already-committed comment
    assert "__interrupt__" in result  # reached awaiting_plan_approval again, this time for round 2
    final = await graph.aget_state(cfg)
    assert final.values["status"] == "awaiting_plan_approval"
    assert final.values["plans"][0].name == "Round 2 plan"  # plan_node actually reran, not a stale replay


async def test_retry_after_plan_generation_failure_reenters_plan_instead_of_crashing():
    """Found live: `status="failed"` can happen *before* any plan was ever approved (here,
    `_validate_plan_issue_keys` rejects every action in the only proposed plan) — not just mid-commit.
    The original `retry_recovery_commit` only handled the mid-commit case; retrying this one re-armed
    `commit_one_action_node`, which unconditionally reads `state["approved_plan"].actions` and would
    crash on the `None` left behind here. Retry must detect "no plan was ever approved" and re-enter
    `plan` instead.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    bogus_plans = RecoveryPlanSet(plans=[
        RecoveryPlan(plan_id="plan_x", name="Bogus", rationale="r", impact_on_goal="n/a", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="ZZZ-999", comment_body="not a real issue"),
        ]),
    ])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [bogus_plans, _plan_set()]})
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    graph = _graph(client, retrieval, FakeJiraActions())
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    failed = await graph.ainvoke(_state(), config=cfg)
    assert failed["status"] == "failed"
    assert "invalid/unknown issue keys" in failed["error"]
    assert failed.get("approved_plan") is None

    result = await retry_recovery_commit(graph, thread_id)  # must not crash on approved_plan=None
    assert "__interrupt__" in result  # reached awaiting_plan_approval again, this time with valid plans

    snap = await graph.aget_state(cfg)
    assert snap.values["status"] == "awaiting_plan_approval"
    assert len(snap.values["plans"]) == 2


async def test_time_travel_rewinds_to_post_diagnosis_checkpoint_and_produces_new_plans():
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    plan_v1 = _plan_set()
    plan_v2 = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Revised after human note", rationale="PAY-97 was descoped per Slack",
        impact_on_goal="different approach", actions=[
            RecoveryAction(action_type="move_out_of_sprint", target_issue_key="PAY-142"),
        ],
    )])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis, diagnosis], RecoveryPlanSet: [plan_v1, plan_v2]})
    graph = _graph(client, retrieval, FakeJiraActions())
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)  # reaches awaiting_plan_approval with plan_v1

    history = await list_checkpoint_history(graph, thread_id)
    post_diagnosis = next(s for s in history if s.next == ("plan",))

    result = await time_travel_resume(
        graph, thread_id, post_diagnosis.config["configurable"]["checkpoint_id"],
        note="PAY-97 was actually descoped per a Slack thread yesterday — re-plan around that.",
    )
    assert result["plans"][0].name == "Revised after human note"

    # The rewrite happened on the SAME thread — its own latest state now reflects the new branch.
    latest = await graph.aget_state(cfg)
    assert latest.values["plans"][0].name == "Revised after human note"


async def test_crash_mid_commit_resumes_without_duplicate_actions():
    """The real fault-injection test, same shape as tests/test_rollout_graph.py's: a genuine
    `AsyncPostgresSaver`, two fully independent connections/graphs/clients sharing nothing but the
    Postgres row and thread_id — "process 2" resumes exactly where "process 1" died.
    """
    psycopg = pytest.importorskip("psycopg")
    try:
        conn = await psycopg.AsyncConnection.connect(
            "postgresql://poc:poc123@localhost:5432/pocdb", connect_timeout=2,
        )
        await conn.close()
    except Exception:
        pytest.skip("dev Postgres (poc-postgres) not reachable")

    from app.planning.checkpoint import build_checkpointer
    from contextlib import AsyncExitStack

    db_url = "postgresql://poc:poc123@localhost:5432/pocdb"
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    thread_id = str(uuid.uuid4())
    cfg = {"configurable": {"thread_id": thread_id}}

    class _SimulatedCrash(Exception):
        pass

    stack1 = AsyncExitStack()
    try:
        checkpointer1 = await build_checkpointer(db_url, stack1)
        client1 = _client(diagnosis, _plan_set())
        retrieval1 = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
        jira1 = FakeJiraActions(fail_on_call=2, raise_type=_SimulatedCrash)  # 2nd action "crashes"
        graph1 = build_sprint_recovery_graph(
            client1, "fake-model", NoopSpaceMembershipChecker(), retrieval1, jira1,
        ).compile(checkpointer=checkpointer1)

        await graph1.ainvoke(_state(), config=cfg)
        with pytest.raises(_SimulatedCrash):
            await graph1.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)

        mid_state = await graph1.aget_state(cfg)
        assert mid_state.values["committed_actions"] == {"0": "link_dependency on PAY-142 done"}
        assert mid_state.values["status"] == "committing"
    finally:
        await stack1.aclose()

    stack2 = AsyncExitStack()
    try:
        checkpointer2 = await build_checkpointer(db_url, stack2)
        jira2 = FakeJiraActions()  # fresh ledger — proves action 0 is NOT re-requested
        graph2 = build_sprint_recovery_graph(
            _client(diagnosis, _plan_set()), "fake-model", NoopSpaceMembershipChecker(),
            FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), jira2,
        ).compile(checkpointer=checkpointer2)

        final = await graph2.ainvoke(None, config=cfg)
        assert "__interrupt__" in final  # reached wait_for_reevaluation
        assert final["committed_actions"] == {
            "0": "link_dependency on PAY-142 done", "1": "change_priority on PAY-97 done",
        }
        # The critical assertion: process 2 only ever attempted action 1 (change_priority) — action 0
        # was never re-requested.
        assert jira2.calls == [("change_priority", "PAY-97")]
    finally:
        await stack2.aclose()
        async with await psycopg.AsyncConnection.connect(db_url, autocommit=True) as conn:
            for table in ("checkpoints", "checkpoint_writes", "checkpoint_blobs"):
                await conn.execute(
                    f"DELETE FROM planning_workflows.{table} WHERE thread_id = %s", (thread_id,)
                )


async def test_healthy_sprint_short_circuits_to_no_risk_found_without_calling_the_llm():
    """Found live against all 7 real AtlasCart sprints: a sprint with zero deterministic risk signals
    still went to the LLM with an empty prompt, which came back overall_confidence='insufficient' and
    asked the *user* "what risk signals were gathered for this sprint?" — the model asking a human to
    supply the data the system gathers for itself. `diagnose_node` now answers that structurally.
    """
    client = _client(DiagnosisResult(hypotheses=[], overall_confidence="sufficient"), _plan_set())
    retrieval = FakeRetrieval(issue_rows=list(_HEALTHY_ROWS))  # all done -> no signals fire
    graph = _graph(client, retrieval, FakeJiraActions())

    result = await graph.ainvoke(_state(), config=_cfg())

    assert result["status"] == "no_risk_found"
    assert result["risk_signals"] == []
    assert result["clarification_question"] is None
    # The whole point: no LLM call was made at all, so no clarifying question could be invented.
    assert client.completions.calls == []


async def test_risk_signals_are_citable_evidence_so_hypotheses_can_be_grounded():
    """A sprint-level signal (low_completion_forecast) carries no issue_key, so `_gather_evidence`
    fetches nothing for it. Before signals were restated as evidence, the model was shown facts it was
    structurally forbidden to cite — every hypothesis it produced then got dropped by
    `_apply_grounding` for citing an unknown id, cascading into a bare plan-generation failure.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["risk1"])], overall_confidence="sufficient")
    # 1 issue, in_progress -> long_in_progress fires; 0 done of 1 -> low_completion_forecast fires too.
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS), comments=[], details=[], history=[])
    graph = _graph(_client(diagnosis, _plan_set()), retrieval, FakeJiraActions())

    result = await graph.ainvoke(_state(), config=_cfg())

    signal_evidence = [e for e in result["evidence"] if e.source_type == "risk_signal"]
    assert signal_evidence, "every deterministic risk signal must be restated as citable evidence"
    assert {e.citation_id for e in signal_evidence} >= {"risk1"}
    # A hypothesis citing risk1 survives grounding instead of being silently discarded.
    assert len(result["hypotheses"]) == 1


async def test_sprint_level_risk_is_planned_directly_using_the_sprint_roster():
    """Sprint-level risk (nothing started, no single broken ticket) used to hand straight off to a
    human with nothing attached, because `plan_node` only ever knew about issues that had
    independently tripped a per-issue signal — a signal with no issue_key (like the sprint-wide
    completion forecast) left it with literally no legal target. The sprint snapshot (see
    `_build_sprint_snapshot`) widens `known_issue_keys` to the whole roster, so a plan can still act on
    an issue that never tripped a signal of its own.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["risk1"])], overall_confidence="sufficient")
    plan_set = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Move non-critical scope out", rationale="protects the goal",
        impact_on_goal="protects the goal",
        actions=[RecoveryAction(action_type="move_out_of_sprint", target_issue_key="PAY-1")],
    )])
    # `planned` never trips a per-issue signal, so the only signal is the sprint-level forecast —
    # which carries no issue_key. Previously that meant "nothing to act on".
    planned_rows = [{"issue_key": "PAY-1", "status": "planned", "updated_at": "2026-07-20T00:00:00Z"}]
    retrieval = FakeRetrieval(issue_rows=planned_rows, comments=[], details=[], history=[])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [plan_set]})
    graph = _graph(client, retrieval, FakeJiraActions())

    result = await graph.ainvoke(_state(), config=_cfg())

    assert result["status"] == "awaiting_plan_approval"
    # PAY-1 never tripped a per-issue signal — it is actionable purely because the snapshot carried it.
    assert result["plans"][0].actions[0].target_issue_key == "PAY-1"
    assert [i.issue_key for i in result["sprint_snapshot"].issues] == ["PAY-1"]


async def test_blocked_status_fires_its_own_signal_not_just_the_sprint_level_forecast():
    """Found live: `blocked` is a first-class status in the board's own type system, but
    `_detect_risk_signals` only ever checked for `in_progress` — a genuinely blocked issue tripped no
    per-issue signal at all, only the sprint-level completion forecast. That pushed real "blocked"
    situations to look identical to the "nothing is attributable to any one issue" handoff case, and
    forced demo data to lie (status=in_progress on an issue whose own description said "blocked") just
    to make a per-issue signal fire.
    """
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "blocked", "updated_at": "2026-07-20T00:00:00Z"},
    ])
    signals = await _detect_risk_signals(retrieval, 5000014, 7)
    assert len(signals) == 2  # blocked_no_flag (per-issue) + low_completion_forecast (sprint-level)
    per_issue = [s for s in signals if s.signal_type == "blocked_no_flag"]
    assert len(per_issue) == 1
    assert per_issue[0].issue_key == "PAY-1"


async def test_in_progress_signal_requires_actual_staleness_not_just_the_status():
    """Direct product feedback: "can the system tell if an owner just isn't updating their ticket?"
    Before this, `long_in_progress` fired on ANY in_progress issue regardless of how long it had sat
    there — a ticket that moved to in_progress five minutes ago looked exactly as risky as one
    untouched for two weeks. Now it requires real staleness (`_STALE_AFTER_DAYS`).
    """
    # Companion "done" rows keep the sprint-level completion forecast (no end_date here, so it uses
    # the flat 70% cutoff) out of the way, so only the per-issue staleness behavior under test can
    # produce a signal either way.
    companions = [{"issue_key": f"PAY-{i}", "status": "done", "updated_at": "2026-08-01T00:00:00Z"} for i in range(2, 6)]

    fresh = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "in_progress", "updated_at": datetime.now(timezone.utc).isoformat()},
        *companions,
    ])
    assert await _detect_risk_signals(fresh, 5000014, 7) == []

    stale = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "in_progress", "updated_at": "2026-07-01T00:00:00Z"},
        *companions,
    ])
    signals = await _detect_risk_signals(stale, 5000014, 7)
    assert any(s.signal_type == "long_in_progress" and s.issue_key == "PAY-1" for s in signals)


async def test_completion_forecast_gets_stricter_near_the_sprint_deadline():
    """80% done reads completely differently on day 2 of a 10-day sprint than with 1 day left. The
    flat 70% cutoff alone can't tell those apart; this adds a second, independent rule that only
    engages once the sprint is nearly over.
    """
    rows_80pct = [{"issue_key": f"PAY-{i}", "status": "done", "updated_at": "x"} for i in range(8)] + [
        {"issue_key": "PAY-9", "status": "planned", "updated_at": "x"},
        {"issue_key": "PAY-10", "status": "planned", "updated_at": "x"},
    ]
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    far_off = (date.today() + timedelta(days=20)).isoformat()

    ending_soon = FakeRetrieval(
        issue_rows=rows_80pct,
        sprints=[{"sprint_id": 7, "end_date": tomorrow}],
    )
    signals = await _detect_risk_signals(ending_soon, 5000014, 7)
    assert any(s.signal_type == "low_completion_forecast" for s in signals)  # 80% is not enough here

    plenty_of_runway = FakeRetrieval(
        issue_rows=rows_80pct,
        sprints=[{"sprint_id": 7, "end_date": far_off}],
    )
    signals = await _detect_risk_signals(plenty_of_runway, 5000014, 7)
    assert not any(s.signal_type == "low_completion_forecast" for s in signals)  # 80% is fine this early


async def test_completion_forecast_is_suppressed_in_the_first_fifth_of_the_sprint():
    """A sprint's completion forecast starts at 0% by definition on day 1 — without a grace period,
    running a health check that early would flag "low completion" every single time, which isn't a
    finding, it's just what day 1 looks like. Direct product question this answers: "wouldn't a real
    PM only run this mid-sprint or later?" — yes, and this is what makes running it on day 1 harmless
    instead of a guaranteed false positive.
    """
    zero_done = [{"issue_key": f"PAY-{i}", "status": "planned", "updated_at": "x"} for i in range(5)]
    today = date.today()

    day_one = FakeRetrieval(
        issue_rows=zero_done,
        sprints=[{"sprint_id": 7, "start_date": today.isoformat(), "end_date": (today + timedelta(days=10)).isoformat()}],
    )
    signals = await _detect_risk_signals(day_one, 5000014, 7)
    assert not any(s.signal_type == "low_completion_forecast" for s in signals)  # too early to call this a finding

    mid_sprint = FakeRetrieval(
        issue_rows=zero_done,
        sprints=[{"sprint_id": 7, "start_date": (today - timedelta(days=6)).isoformat(), "end_date": (today + timedelta(days=4)).isoformat()}],
    )
    signals = await _detect_risk_signals(mid_sprint, 5000014, 7)
    assert any(s.signal_type == "low_completion_forecast" for s in signals)  # day 6 of 10, still 0% -> real finding


def test_describe_action_names_every_action_type_in_plain_language():
    """Locks in the plain-language rendering `build_escalation_summary` and the checkpoint-history
    endpoint both depend on — including the full comment body, which the old `committed_actions`
    result string ("comment added to X") never carried.
    """
    assert _describe_action(RecoveryAction(
        action_type="add_comment", target_issue_key="PAY-1", comment_body="pairing another engineer",
    )) == 'Commented on PAY-1: "pairing another engineer"'
    assert _describe_action(RecoveryAction(
        action_type="change_priority", target_issue_key="PAY-1", new_priority="highest",
    )) == "Raised PAY-1 priority to highest"
    assert _describe_action(RecoveryAction(
        action_type="move_out_of_sprint", target_issue_key="PAY-1",
    )) == "Moved PAY-1 out of the sprint"
    assert _describe_action(RecoveryAction(
        action_type="link_dependency", target_issue_key="PAY-1", depends_on_issue_key="PAY-97",
    )) == "Marked PAY-1 as blocked by PAY-97"


async def test_plan_prompt_lists_prior_round_actions_so_it_wont_repeat_a_no_op():
    """Found live: escalation round 2 raised the same issue's priority to "highest" a second time —
    a no-op, since it was already there from round 1 — because the prompt only said "the previous plan
    didn't work" without ever saying what it actually did. This locks in that round 2's prompt now
    names the concrete round-1 action.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["risk1"])], overall_confidence="sufficient")
    round0_plan = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Raise priority", rationale="r", impact_on_goal="visibility", actions=[
            RecoveryAction(action_type="change_priority", target_issue_key="PAY-1", new_priority="highest"),
        ],
    )])
    round1_plan = RecoveryPlanSet(plans=[RecoveryPlan(
        plan_id="plan_a", name="Escalate further", rationale="r2", impact_on_goal="visibility", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="PAY-1", comment_body="escalating"),
        ],
    )])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [round0_plan, round1_plan]})
    # Blocked, never changes -> `still_at_risk` (now `bool(signals)`) stays true every round even though
    # nothing here is `in_progress`/has a low completion forecast at all — the exact case the
    # `still_at_risk` fix covers.
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "blocked", "updated_at": "2026-07-01T00:00:00Z"},
    ] * 10)  # 10 done-less rows keeps completion forecast low too, but blocked alone must be sufficient
    graph = _graph(client, retrieval, FakeJiraActions())
    cfg = _cfg()
    thread_id = cfg["configurable"]["thread_id"]

    await graph.ainvoke(_state(), config=cfg)
    result = await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)
    assert "__interrupt__" in result  # reached wait_for_reevaluation
    await trigger_reevaluation(graph, thread_id, source="manual")
    snap = await graph.aget_state(cfg)
    assert snap.values["status"] == "awaiting_plan_approval"  # still at risk -> looped into round 1
    assert snap.values["prior_committed_actions"] == ["Raised PAY-1 priority to highest"]

    round1_prompt = client.completions.calls[-1]["messages"][0]["content"]
    assert "Raised PAY-1 priority to highest" in round1_prompt
    assert "do not repeat" in round1_prompt.lower()


async def test_plan_prompt_tells_the_model_not_to_ignore_an_external_blocker_in_comments():
    """Found live: a plan asked an owner to "confirm you have resumed [X] and provide a completion
    date" — while the plan's own rationale, one paragraph above, said progress was zero because the
    team had been diverted to a production incident. The comment ignored the very blocker the plan was
    built on, asking someone doing incident response to commit to an unrelated delivery date as if the
    incident weren't happening. Locks in that the prompt now tells the model to acknowledge and ask
    about an external blocker's status instead of writing around it.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["risk1"])], overall_confidence="sufficient")
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "planned", "updated_at": "2026-07-20T00:00:00Z"},
    ])
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set()]})
    graph = _graph(client, retrieval, FakeJiraActions())

    await graph.ainvoke(_state(), config=_cfg())

    plan_prompt = client.completions.calls[-1]["messages"][0]["content"]
    assert "external blocker" in plan_prompt
    assert "do not ask them to simply confirm" in plan_prompt.lower()


async def test_signals_name_the_owner_so_generated_comments_can_address_a_person():
    """Direct product ask: "if a plan is accepted and it's sensible to notify the issue owner, that
    needs to exist." The blocker was data, not logic — `IssueRowOut` carried no assignee until
    vectorization-service migrations/012. With it plumbed through, every per-issue signal names the
    owner, which is what lets `plan_node` address a real person instead of asking a comment's reader
    to identify themselves.
    """
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-1", "status": "blocked", "updated_at": "2026-07-01T00:00:00Z",
         "assignee_name": "Maya Chen", "story_points": 5},
    ])
    signals = await _detect_risk_signals(retrieval, 5000014, 7)
    blocked = next(s for s in signals if s.signal_type == "blocked_no_flag")
    assert "Maya Chen" in blocked.description

    # Unassigned is a real state worth saying out loud, not silently omitting.
    unassigned = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-2", "status": "blocked", "updated_at": "2026-07-01T00:00:00Z"},
    ])
    signals = await _detect_risk_signals(unassigned, 5000014, 7)
    assert "No one is assigned" in next(s for s in signals if s.signal_type == "blocked_no_flag").description


async def test_owner_overloaded_falls_back_to_issue_count_when_nothing_is_estimated():
    """No story points anywhere in the sprint is a legitimate state (not every team points every
    ticket) — the signal must still be computable, just on issue count instead of weight.
    `owner_overloaded` was declared in `RiskSignal.signal_type` from the start but never computed at
    all — impossible without assignee data. It must carry an issue_key: `plan_node` hands off to a
    human when no signal names a concrete issue, so a keyless signal would be undiagnosable.
    """
    rows = [
        {"issue_key": f"PAY-{i}", "status": "in_progress", "updated_at": "2026-08-08T00:00:00Z",
         "assignee_name": "Maya Chen"}
        for i in range(1, 4)
    ]
    signals = await _detect_risk_signals(FakeRetrieval(issue_rows=rows), 5000014, 7)
    overloaded = [s for s in signals if s.signal_type == "owner_overloaded"]
    assert len(overloaded) == 1
    assert "Maya Chen" in overloaded[0].description
    assert "no story points estimated" in overloaded[0].description
    assert overloaded[0].issue_key  # must be actionable, not sprint-level


async def test_owner_overloaded_uses_points_share_not_raw_issue_count_when_estimated():
    """Direct product feedback: issues have different sizes, so a raw issue-count threshold treats a
    1-point ticket and an 8-point ticket as identical load — a points-weighted share is the more
    honest measure of who's actually the bottleneck.
    """
    # Maya holds 1 large ticket (8 points) + 1 small one (2 points) = 10/14 = 71% of unfinished work,
    # across only 2 issues — would NOT trip a raw "3+ issues" count rule, but clearly should trip a
    # points-share rule.
    rows = [
        {"issue_key": "PAY-1", "status": "in_progress", "updated_at": "2026-08-08T00:00:00Z",
         "assignee_name": "Maya Chen", "story_points": 8},
        {"issue_key": "PAY-2", "status": "in_progress", "updated_at": "2026-08-08T00:00:00Z",
         "assignee_name": "Maya Chen", "story_points": 2},
        {"issue_key": "PAY-3", "status": "planned", "updated_at": "2026-08-08T00:00:00Z",
         "assignee_name": "Liam Ortiz", "story_points": 4},
    ]
    signals = await _detect_risk_signals(FakeRetrieval(issue_rows=rows), 5000014, 7)
    overloaded = [s for s in signals if s.signal_type == "owner_overloaded"]
    assert len(overloaded) == 1
    assert "Maya Chen" in overloaded[0].description
    assert "10/14" in overloaded[0].description
    assert "71%" in overloaded[0].description

    # A single big ticket being the only unfinished work left for anyone isn't "overloaded" — that's
    # just what's left. The `_OVERLOADED_MIN_ISSUES` floor exists specifically for this case.
    one_ticket = [
        {"issue_key": "PAY-1", "status": "in_progress", "updated_at": "2026-08-08T00:00:00Z",
         "assignee_name": "Maya Chen", "story_points": 8},
    ]
    signals = await _detect_risk_signals(FakeRetrieval(issue_rows=one_ticket), 5000014, 7)
    assert not any(s.signal_type == "owner_overloaded" for s in signals)


async def test_completion_is_measured_in_story_points_when_they_exist():
    """Real sprints commit and track in points. By issue count alone, 8 trivial tickets done out of 10
    reads as "80% complete" even when the two left carry most of the weight — 3/30 points is the
    honest number, and it's the one that trips the risk threshold.
    """
    rows = [{"issue_key": f"PAY-{i}", "status": "done", "updated_at": "x", "story_points": 1} for i in range(8)]
    rows += [{"issue_key": f"PAY-{8+i}", "status": "planned", "updated_at": "x", "story_points": 20} for i in range(2)]
    signals = await _detect_risk_signals(FakeRetrieval(issue_rows=rows), 5000014, 7)
    forecast = next(s for s in signals if s.signal_type == "low_completion_forecast")
    assert "story points" in forecast.description
    assert "8/48" in forecast.description  # 8 of 48 points, not "8/10 issues (80%)"

    # No estimates anywhere is a legitimate state — fall back to counting issues, and say so.
    unestimated = [{"issue_key": f"PAY-{i}", "status": "planned", "updated_at": "x"} for i in range(5)]
    signals = await _detect_risk_signals(FakeRetrieval(issue_rows=unestimated), 5000014, 7)
    forecast = next(s for s in signals if s.signal_type == "low_completion_forecast")
    assert "no story points estimated" in forecast.description


async def test_sprint_goal_reaches_both_prompts():
    """`RecoveryPlan.impact_on_goal` has always been a required schema field the prompt asked the model
    to fill in — while never showing it the goal. "Impact on goal" was the model inferring a plausible
    goal from ticket titles. Both prompts must now carry the real one.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["risk1"])], overall_confidence="sufficient")
    client = FakeInstructorClient({DiagnosisResult: [diagnosis], RecoveryPlanSet: [_plan_set()]})
    retrieval = FakeRetrieval(
        issue_rows=list(_AT_RISK_ROWS),
        sprints=[{"sprint_id": 7, "goal": "Ship the redesigned checkout to 100% of traffic"}],
    )
    graph = _graph(client, retrieval, FakeJiraActions())
    await graph.ainvoke(_state(), config=_cfg())

    prompts = [c["messages"][0]["content"] for c in client.completions.calls]
    assert len(prompts) == 2  # diagnose + plan
    assert all("Ship the redesigned checkout to 100% of traffic" in p for p in prompts)


async def test_scope_added_late_flags_an_issue_moved_in_after_the_sprint_midpoint():
    """Real example this catches: a 10-day sprint starts with committed tickets; on day 7 someone
    drags in 2 "quick" extras during grooming. Every other signal reads that as the team executing
    worse (falling completion %, an overloaded assignee) — this is the one deterministic fact that
    names the actual cause as scope moving, not execution.
    """
    start = date.today() - timedelta(days=7)
    end = date.today() + timedelta(days=3)  # 10-day sprint, today is day 7 -> 70% elapsed
    sprint_name = "Sprint 7"
    retrieval = FakeRetrieval(
        issue_rows=[{"issue_key": "PAY-1", "status": "planned", "updated_at": "x"}],
        sprints=[{"sprint_id": 7, "sprint_name": sprint_name,
                  "start_date": start.isoformat(), "end_date": end.isoformat()}],
        history=[
            {"issue_key": "PAY-1", "field_name": "sprint", "to_value": sprint_name,
             "changed_at": (start + timedelta(days=6)).isoformat() + "T00:00:00Z"},  # day 6 of 10 = late
        ],
    )
    signals = await _detect_risk_signals(retrieval, 5000014, 7)
    late = [s for s in signals if s.signal_type == "scope_added_late"]
    assert len(late) == 1
    assert late[0].issue_key == "PAY-1"
    assert sprint_name in late[0].description

    # Added on day 1 (pre-sprint grooming refinement, not scope creep) must NOT fire.
    early_retrieval = FakeRetrieval(
        issue_rows=[{"issue_key": "PAY-2", "status": "planned", "updated_at": "x"}],
        sprints=[{"sprint_id": 7, "sprint_name": sprint_name,
                  "start_date": start.isoformat(), "end_date": end.isoformat()}],
        history=[
            {"issue_key": "PAY-2", "field_name": "sprint", "to_value": sprint_name,
             "changed_at": start.isoformat() + "T00:00:00Z"},
        ],
    )
    signals = await _detect_risk_signals(early_retrieval, 5000014, 7)
    assert not any(s.signal_type == "scope_added_late" for s in signals)




async def test_index_catch_up_returns_immediately_when_nothing_was_committed():
    """No watermarks means no writes to wait on — must not poll or sleep at all."""
    retrieval = FakeRetrieval(issue_rows=list(_AT_RISK_ROWS))
    assert await _await_index_catch_up(retrieval, 5000014, 7, {}) is True
    assert retrieval.query_issues_calls == 0


async def test_index_catch_up_waits_until_the_read_model_reflects_the_write():
    """**The replacement for a guessed sleep**: the indexed row starts older than the watermark
    recorded from jira-backend at commit time, so the first poll must not be accepted; once the fake
    index is updated (as a real reindex would), the wait ends. Proves it's the *condition* being
    waited on, not elapsed time.
    """
    stale = {"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-08-01T00:00:00Z"}
    retrieval = FakeRetrieval(issue_rows=[dict(stale)])
    watermarks = {"PAY-142": "2026-08-05T00:00:00Z"}  # jira-backend says the write landed on the 5th

    async def _reindex_after_first_poll():
        while retrieval.query_issues_calls < 1:
            await asyncio.sleep(0)
        retrieval.issue_rows = [{**stale, "updated_at": "2026-08-05T00:00:01Z"}]

    reindex = asyncio.create_task(_reindex_after_first_poll())
    assert await _await_index_catch_up(retrieval, 5000014, 7, watermarks) is True
    await reindex
    assert retrieval.query_issues_calls >= 2  # the first, stale read was correctly rejected


async def test_index_catch_up_treats_an_issue_that_left_the_sprint_as_caught_up():
    """A committed `move_out_of_sprint` makes the issue vanish from this sprint's rows — that absence
    is itself proof the write propagated, not a reason to keep waiting for a row that will never come.
    """
    retrieval = FakeRetrieval(issue_rows=[])  # PAY-142 is gone from the sprint
    assert await _await_index_catch_up(retrieval, 5000014, 7, {"PAY-142": "2026-08-05T00:00:00Z"}) is True
    assert retrieval.query_issues_calls == 1


async def test_index_catch_up_gives_up_after_the_timeout_instead_of_stalling_the_workflow():
    """The honest bound: a read model that never catches up must not hang the graph forever — it
    reports failure and lets the caller read anyway."""
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-08-01T00:00:00Z"},
    ])
    with patch.object(graph_module, "_INDEX_CATCH_UP_TIMEOUT_SECONDS", 0.05), \
         patch.object(graph_module, "_INDEX_CATCH_UP_POLL_SECONDS", 0.01):
        caught_up = await _await_index_catch_up(retrieval, 5000014, 7, {"PAY-142": "2026-08-05T00:00:00Z"})

    assert caught_up is False
    assert retrieval.query_issues_calls >= 2


async def test_reevaluate_node_marks_index_catch_up_timed_out_when_the_wait_gives_up():
    """The signal a user-facing caveat and the Prometheus counter both key off — must actually reach
    the returned state, not just get logged and dropped."""
    retrieval = FakeRetrieval(issue_rows=[
        {"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-08-01T00:00:00Z"},
    ])
    state = _state()
    state["index_watermarks"] = {"PAY-142": "2026-08-05T00:00:00Z"}  # never satisfied by the fake index

    with patch.object(graph_module, "_INDEX_CATCH_UP_TIMEOUT_SECONDS", 0.05), \
         patch.object(graph_module, "_INDEX_CATCH_UP_POLL_SECONDS", 0.01):
        result = await reevaluate_node(state, retrieval)

    assert result["index_catch_up_timed_out"] is True
    assert result["status"] in ("escalated", "diagnosing")  # PAY-142 is still in_progress => still at risk


async def test_reevaluate_node_does_not_flag_a_timeout_when_nothing_was_committed():
    """No watermarks (nothing committed since the last round) means nothing to wait on — this must
    never look like a degraded read when there was no write to catch up to in the first place."""
    retrieval = FakeRetrieval(issue_rows=list(_HEALTHY_ROWS))
    state = _state()
    state["index_watermarks"] = {}

    result = await reevaluate_node(state, retrieval)

    assert result["index_catch_up_timed_out"] is False
    assert result["status"] == "recovered"


def _stale_sprint_rows(ts_for_stuck):
    """8 done + 2 stuck issues: 86% complete by points, so `low_completion_forecast` does NOT fire and
    `long_in_progress` is the only thing standing between this sprint and a "recovered" verdict."""
    rows = [{"issue_key": f"PAY-{i}", "status": "done", "assignee_name": "Sam", "story_points": 3,
             "updated_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()} for i in range(8)]
    rows += [{"issue_key": "PAY-142", "status": "in_progress", "assignee_name": "Maya",
              "story_points": 2, "updated_at": ts_for_stuck},
             {"issue_key": "PAY-99", "status": "in_progress", "assignee_name": "Raj",
              "story_points": 2, "updated_at": ts_for_stuck}]
    return rows


class _CountingRetrieval(FakeRetrieval):
    """FakeRetrieval with a real counts_by_status — `low_completion_forecast` reads it, and leaving it
    empty makes every sprint look 0% done, which masks exactly the bug these tests are about."""

    async def query_issues(self, space_ids, filters):
        self.query_issues_calls += 1
        counts: dict = {}
        for row in self.issue_rows:
            counts[row.get("status", "")] = counts.get(row.get("status", ""), 0) + 1
        return type("R", (), {
            "total_count": len(self.issue_rows), "counts_by_status": counts,
            "counts_by_type": {}, "issues": self.issue_rows,
        })()


async def test_our_own_write_does_not_reset_the_staleness_clock_into_a_false_recovered():
    """**Found live, the most consequential bug in this feature.** jira-backend's `@PreUpdate` stamps
    `updatedAt = now()` on any issue write, so committing a `change_priority` on a six-day-stale ticket
    made `_days_since(updated_at)` read 0 — `long_in_progress` vanished, and a sprint with two tickets
    still sitting untouched in `in_progress` reported `status="recovered"`. The workflow was declaring
    victory by touching the ticket. Staleness must be measured from before *our own* write.
    """
    stale = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    our_write = datetime.now(timezone.utc).isoformat()
    state = _state()
    state["pre_write_updated_at"] = {"PAY-142": stale, "PAY-99": stale}
    state["index_watermarks"] = {"PAY-142": our_write, "PAY-99": our_write}

    # The index now shows both tickets "updated just now" — by us, not by anyone doing the work.
    result = await reevaluate_node(state, _CountingRetrieval(issue_rows=_stale_sprint_rows(our_write)))

    assert result["status"] != "recovered"
    assert sorted(s.issue_key for s in result["risk_signals"] if s.signal_type == "long_in_progress") == [
        "PAY-142", "PAY-99",
    ]


async def test_a_genuine_human_update_after_our_write_still_counts_as_real_activity():
    """The other half of the same rule — discounting our own write must not blind the check to somebody
    actually picking the ticket back up. A timestamp newer than our own write is real activity."""
    stale = (datetime.now(timezone.utc) - timedelta(days=6)).isoformat()
    our_write = datetime.now(timezone.utc) - timedelta(minutes=5)
    human_touched_after = (our_write + timedelta(minutes=1)).isoformat()
    state = _state()
    state["pre_write_updated_at"] = {"PAY-142": stale, "PAY-99": stale}
    state["index_watermarks"] = {"PAY-142": our_write.isoformat(), "PAY-99": our_write.isoformat()}

    result = await reevaluate_node(state, _CountingRetrieval(issue_rows=_stale_sprint_rows(human_touched_after)))

    assert result["status"] == "recovered"


async def test_commit_records_the_pre_write_timestamp_once_and_keeps_the_original_baseline():
    """`commit_one_action_node` must capture the baseline *before* writing, and a second action on the
    same issue (or a later escalation round) must not overwrite it with a timestamp this workflow
    itself produced — otherwise round 2 reintroduces exactly the bug round 1's baseline prevents.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    jira = FakeJiraActions(updated_at="2026-08-09T00:00:00Z")
    graph = _graph(_client(diagnosis, _plan_set()), FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)

    snap = await graph.aget_state(cfg)
    pre = snap.values["pre_write_updated_at"]
    assert set(pre) == {"PAY-142", "PAY-97"}  # one per action target in plan_a
    assert all(v == "2026-08-09T00:00:00Z" for v in pre.values())


async def test_index_catch_up_survives_a_transient_poll_failure_instead_of_crashing():
    """**Found in review**: the polling loop's `query_issues` call wasn't guarded — before this, the
    single call `_detect_risk_signals` used to make had a small failure window, but polling up to ~40
    times over the timeout budget meaningfully widened it. A single flaky request must not abort a
    wait that still has time budget left; it should just count as "not caught up yet" and retry.
    """
    calls = {"n": 0}

    class _FlakyThenFineRetrieval(FakeRetrieval):
        async def query_issues(self, space_ids, filters):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("simulated transient vectorization-service blip")
            return await super().query_issues(space_ids, filters)

    retrieval = _FlakyThenFineRetrieval(issue_rows=[])  # empty -> the watched issue has left the sprint
    caught_up = await _await_index_catch_up(retrieval, 5000014, 7, {"PAY-142": "2026-08-05T00:00:00Z"})

    assert caught_up is True
    assert calls["n"] == 2  # failed once, succeeded on retry — never raised out of the function


async def test_depends_on_issue_key_never_gets_a_baseline_since_it_never_gets_a_watermark():
    """**Found live, in the very fix meant to close the crash-resume baseline bug**: `approval_node`
    originally captured a pre-write baseline for `depends_on_issue_key` too, "for completeness."
    `link_dependency` never writes to jira-backend's row for the depended-on issue (it only creates an
    `IssueLink` entity), so `commit_one_action_node` never produces a watermark for that key — its
    `own_writes` entry would be stuck at `after=None` forever, and `_last_update_not_by_this_workflow`
    reads that as "can't prove a human touched it since," permanently freezing its staleness
    measurement at the pre-approval snapshot. A real update to it later in the same round must not be
    silently ignored.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    plan = _plan_set(actions=[
        # PAY-97 here is ONLY ever a depends_on_issue_key in this plan — never a target of anything.
        RecoveryAction(action_type="link_dependency", target_issue_key="PAY-142", depends_on_issue_key="PAY-97"),
    ])
    jira = FakeJiraActions(updated_at="2026-08-09T00:00:00Z")
    graph = _graph(_client(diagnosis, plan), FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), jira)
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    await graph.ainvoke(Command(resume={"decision": "approve", "plan_id": "plan_a"}), config=cfg)

    snap = await graph.aget_state(cfg)
    # Only PAY-142 (the actual target) gets a baseline — PAY-97 (depends_on only) does not.
    assert set(snap.values["pre_write_updated_at"]) == {"PAY-142"}

    # A real human update to PAY-97 later in the same round must be read normally, not ignored.
    own_writes = {
        key: (before, snap.values["index_watermarks"].get(key))
        for key, before in snap.values["pre_write_updated_at"].items()
    }
    fresh_human_update = {"issue_key": "PAY-97", "status": "blocked", "updated_at": "2026-08-08T00:00:00Z"}
    assert _last_update_not_by_this_workflow(fresh_human_update, own_writes) == "2026-08-08T00:00:00Z"


async def test_plan_node_answers_a_human_note_directly_and_does_not_re_answer_it_next_round():
    """**Found live, from direct product feedback**: a human rewound with "we will add 2 extra
    engineers to this sprint" and got back plans that never mentioned engineers. The note had genuinely
    been used (it became evidence, and a hypothesis weighed it explicitly) — but nothing in the plan
    output acknowledged it, which is indistinguishable from the input having been thrown away. Telling
    someone *why* their input can't change the answer is part of the interaction, not an extra.

    Also locks in the staleness half, which is the easy thing to get wrong: `clarification_answer`
    persists in state for the rest of the run, so a later round with nobody saying anything must NOT
    keep re-answering the same note as though it had just been said. `unanswered_human_note` is the
    field that gets cleared; `note_response` is reset per run so it can only describe the plans beside it.
    """
    answered = RecoveryPlanSet(
        plans=_plan_set().plans,
        response_to_human_note=(
            "Adding two engineers won't help here — all three tickets are waiting on external parties, "
            "so there's no engineering work for them to pick up."
        ),
    )
    later_round = RecoveryPlanSet(plans=_plan_set().plans)  # no note outstanding, so no reply requested
    diagnosis_unsure = DiagnosisResult(
        hypotheses=[_hyp(["ev1"], confidence="low")], overall_confidence="insufficient",
        clarifying_question="Is this genuinely blocked?",
    )
    diagnosis_sure = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    client = FakeInstructorClient({
        DiagnosisResult: [diagnosis_unsure, diagnosis_sure],
        RecoveryPlanSet: [answered, later_round],
    })
    graph = _graph(client, FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), FakeJiraActions())
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)  # pauses at clarify
    await graph.ainvoke(Command(resume={"answer": "we will add 2 extra engineers"}), config=cfg)

    snap = await graph.aget_state(cfg)
    assert snap.values["status"] == "awaiting_plan_approval"
    assert "Adding two engineers won't help" in snap.values["note_response"]
    # Answered — so it must not be re-answered on any later run.
    assert snap.values["unanswered_human_note"] is None
    # The prompt only asks for a reply when a note is actually outstanding.
    plan_prompts = [
        c["messages"][0]["content"] for c in client.completions.calls
        if c["response_model"] is RecoveryPlanSet
    ]
    assert "we will add 2 extra engineers" in plan_prompts[0]
    assert "response_to_human_note" in plan_prompts[0]

    # Re-plan with nothing new said — `clarification_answer` is still sitting in state from before, so
    # this is exactly the case where reading that field instead of the cleared one would re-answer a
    # note nobody just gave. The reply must come back `None`, and the prompt must not ask for one.
    assert snap.values["clarification_answer"] == "we will add 2 extra engineers"  # still there
    replanned = await plan_node(
        dict(snap.values), client, "fake-model", FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)),
    )
    assert replanned["note_response"] is None
    latest_plan_prompt = [
        c["messages"][0]["content"] for c in client.completions.calls
        if c["response_model"] is RecoveryPlanSet
    ][-1]
    assert "response_to_human_note" not in latest_plan_prompt


async def test_token_usage_and_cost_are_measured_from_the_provider_not_estimated():
    """**Found live, from a direct question about what a run of this workflow costs**: every node used
    to advance `token_usage` by a flat `_TOKENS_PER_LLM_CALL_ESTIMATE` (4000), so the reported number
    was a guess multiplied by a call count and any cost derived from it was fiction. `_call_model` now
    reads the real counts off the raw completion (`create_with_completion`) and prices them per model.

    The pre-call budget guard still uses the flat estimate on purpose — nothing can know a call's size
    before making it — so this asserts the *recorded* number is the measured one, which is exactly the
    distinction that was collapsed before.
    """
    diagnosis = DiagnosisResult(hypotheses=[_hyp(["ev1"])], overall_confidence="sufficient")
    client = _client(diagnosis, _plan_set())
    recorded: list = []
    graph = build_sprint_recovery_graph(
        client, "gpt-5.6-luna", NoopSpaceMembershipChecker(),
        FakeRetrieval(issue_rows=list(_AT_RISK_ROWS)), FakeJiraActions(),
        on_usage=lambda usage, cost: recorded.append((usage, cost)),
    ).compile(checkpointer=InMemorySaver())
    cfg = _cfg()

    await graph.ainvoke(_state(), config=cfg)
    snap = await graph.aget_state(cfg)

    # Two real LLM calls (diagnose + plan), each reporting 1234 in / 567 out — see _FakeCompletions.
    assert len(recorded) == 2
    assert snap.values["token_usage"] == 2 * (1234 + 567)
    assert snap.values["token_usage"] % _TOKENS_PER_LLM_CALL_ESTIMATE != 0  # not the old flat estimate
    # gpt-5.6-luna: $0.20/1M input, $1.20/1M output — the price actually on the pricing page, which the
    # table had wrong (5x too high) until this was checked.
    expected = 2 * ((1234 / 1_000_000) * 0.20 + (567 / 1_000_000) * 1.20)
    assert snap.values["cost_usd"] == pytest.approx(expected)
    assert sum(cost for _, cost in recorded) == pytest.approx(expected)


def test_openai_cached_input_tokens_are_not_double_counted_as_fresh_input():
    """OpenAI reports `prompt_tokens` *inclusive* of cached tokens while Anthropic reports them
    separately, and cached input is billed at a tenth of fresh input — folding the two together would
    overstate the cached portion by 10x. Normalizing at the boundary keeps one pricing path correct for
    both providers.
    """
    from types import SimpleNamespace

    completion = SimpleNamespace(usage=SimpleNamespace(
        prompt_tokens=1000, completion_tokens=100,
        prompt_tokens_details=SimpleNamespace(cached_tokens=800),
    ))

    usage = graph_module._usage_from_completion(completion)

    assert usage.input_tokens == 200  # the genuinely fresh portion
    assert usage.cache_read_input_tokens == 800
    assert usage.output_tokens == 100


def test_a_provider_reporting_no_usage_degrades_to_free_rather_than_raising():
    """A bare llama.cpp server reports no usage block at all. Cost visibility must never be the thing
    that breaks a real workflow — see app/llm/types.py's Usage. Token accounting falls back to the flat
    estimate so the budget ceiling stays enforceable instead of silently becoming infinite."""
    from types import SimpleNamespace

    assert graph_module._usage_from_completion(SimpleNamespace(usage=None)) == Usage()
    assert graph_module._usage_from_completion(SimpleNamespace()) == Usage()
