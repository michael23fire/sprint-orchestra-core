"""Tests for the sprint-recovery durable workflow (app/sprint_recovery/graph.py).

Same fake-instructor-client approach as tests/test_planning.py (imported directly, not copy-pasted)
plus a fake retrieval client (mirrors RetrievalClient's dataclass return shapes) and a fake jira
actions client (mirrors JiraActionsClient). InMemorySaver for fast mechanism tests;
`test_crash_mid_commit_resumes_without_duplicate_actions` uses a real AsyncPostgresSaver, same
reasoning as `tests/test_rollout_graph.py`'s crash test.
"""
import uuid

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.auth.space_membership import NoopSpaceMembershipChecker, SpaceMembershipError
from app.sprint_recovery.graph import (
    _gather_evidence,
    build_escalation_summary,
    build_sprint_recovery_graph,
    list_checkpoint_history,
    retry_recovery_commit,
    time_travel_resume,
    trigger_reevaluation,
)
from app.sprint_recovery.jira_actions_client import JiraActionError
from app.sprint_recovery.schemas import DiagnosisResult, RecoveryAction, RecoveryPlan, RecoveryPlanSet, RootCauseHypothesis
from app.sprint_recovery.state import initial_recovery_state
from tests.test_planning import FakeInstructorClient


class FakeRetrieval:
    """Configurable fake for the 4 retrieval calls diagnose/reevaluate use. `issue_rows` and
    `still_at_risk` are mutable attributes so a single test can change what the *next* call returns
    (e.g. to simulate "the sprint improved after actions were committed").
    """

    def __init__(self, issue_rows=None, comments=None, details=None, attachments=None, history=None):
        self.issue_rows = issue_rows if issue_rows is not None else []
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


class FakeJiraActions:
    def __init__(self, fail_on_call: int = None, raise_type=JiraActionError):
        self.calls = []
        self._fail_on_call = fail_on_call
        self._raise_type = raise_type

    async def execute(self, space_id, action, user_id, username):
        self.calls.append((action.action_type, action.target_issue_key))
        if self._fail_on_call is not None and len(self.calls) == self._fail_on_call:
            raise self._raise_type(f"simulated failure on call {len(self.calls)}")
        return f"{action.action_type} on {action.target_issue_key} done"


_AT_RISK_ROWS = [{"issue_key": "PAY-142", "status": "in_progress", "updated_at": "2026-07-20T00:00:00Z"}]
_HEALTHY_ROWS = [{"issue_key": "PAY-142", "status": "done", "updated_at": "2026-08-01T00:00:00Z"}] * 8


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
    assert "Round 1" in summary and "Unblock via dependency" in summary
    assert "Round 2" in summary and "Escalate to a second engineer" in summary
    assert "Still at risk because" in summary


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
