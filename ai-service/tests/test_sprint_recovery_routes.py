"""Tests for app/api/sprint_recovery_routes.py — currently just the one function worth unit testing
in isolation (no FastAPI TestClient/app.state scaffolding elsewhere in this codebase; the rest of this
router is exercised via graph-level tests plus live verification, same pattern as app/api/routes.py).
"""
import types
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import sprint_recovery_routes as routes
from app.sprint_recovery.schemas import RecoveryAction, RecoveryPlan


def _fake_request(user_id: str = "me") -> types.SimpleNamespace:
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=types.SimpleNamespace(
            sprint_recovery_checkpoint_db_url="postgresql://fake",
            sprint_recovery_graph=types.SimpleNamespace(aget_state=AsyncMock()),
        )),
        headers={"x-user-id": user_id, "x-username": user_id},
    )


async def test_auto_reevaluate_is_a_passthrough_when_not_actually_waiting_on_a_reevaluation():
    """Every status other than `waiting_reevaluation` means this call is a no-op passthrough — the
    caller's own values come straight back and nothing is re-triggered."""
    with patch.object(routes, "trigger_reevaluation", new=AsyncMock()) as trigger:
        values = {"status": "awaiting_plan_approval"}
        result = await routes._auto_reevaluate_after_commit(graph=object(), thread_id="t1", values=values)

    assert result is values
    trigger.assert_not_called()


async def test_auto_reevaluate_rechecks_once_when_committing_just_finished():
    """The staleness guard this used to need a fixed sleep for now lives in `reevaluate_node`'s
    watermark wait (see `_await_index_catch_up`), so this route just chains the one re-check.
    """
    trigger_mock = AsyncMock(return_value={"status": "recovered"})
    with patch.object(routes, "trigger_reevaluation", new=trigger_mock):
        result = await routes._auto_reevaluate_after_commit(
            graph=object(), thread_id="t1", values={"status": "waiting_reevaluation"},
        )

    trigger_mock.assert_awaited_once()
    assert result == {"status": "recovered"}


def test_status_response_exposes_the_actually_approved_plan_separately_from_the_stale_proposals():
    """**Found live**: `plans` is always the *original* pre-edit proposals from `plan_node` — a human
    `decision="edit"` replaces the executing plan (`state["approved_plan"]`) with a modified
    `RecoveryPlan`, but the API never exposed that object, only the stale pre-edit list. Reproduced
    live: edited a plan to target a nonexistent issue key, watched it fail partway through — `plans`
    still showed the original, un-edited action list, not what had actually been sent to Jira or what
    `committed_actions`' indices actually refer to.
    """
    from app.sprint_recovery.schemas import RecoveryAction, RecoveryPlan

    edited_plan = RecoveryPlan(
        plan_id="plan_a", name="Edited by a human", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-EDITED", comment_body="hi")],
    )
    original_plan = RecoveryPlan(
        plan_id="plan_a", name="Original proposal", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-ORIGINAL", comment_body="hi")],
    )
    values = {"status": "failed", "plans": [original_plan], "approved_plan": edited_plan}

    response = routes._status_response("t1", values)

    assert response.approved_plan is not None
    assert response.approved_plan.actions[0].target_issue_key == "PAY-EDITED"
    # The stale proposal list is still there for the awaiting-approval case, but must not be confused
    # with what's actually executing.
    assert response.plans[0].actions[0].target_issue_key == "PAY-ORIGINAL"


def test_status_response_approved_plan_is_none_before_anything_is_approved():
    values = {"status": "awaiting_plan_approval", "plans": []}
    response = routes._status_response("t1", values)
    assert response.approved_plan is None


async def test_by_sprint_treats_another_owners_thread_as_nothing_found_not_a_403():
    """**Found live**: a thread started with no caller identity (an internal/eval-style call with no
    x-user-id header, same "trusted internal caller" convention `_caller_identity` documents) left
    `state["user_id"]` empty. A real, authenticated browser session then hit `/by-sprint` for that same
    sprint and got a raw 403 "workflow is not owned by the authenticated user" surfaced straight into
    the UI — instead of the graceful "nothing to resume, offer a fresh start" every other not-found
    path takes. Every other endpoint in this router enforces per-thread ownership via
    `_authorize_workflow_state`; `/by-sprint` was the one exception, checking only space membership.
    Folding the 403 into the same `None` return as "no active thread" (rather than letting it
    propagate) also avoids leaking to a caller that *someone else* has a check running for this sprint.
    """
    request = _fake_request(user_id="me")
    request.app.state.sprint_recovery_graph.aget_state.return_value = types.SimpleNamespace(
        values={"status": "awaiting_plan_approval", "user_id": "someone-else", "space_id": 1},
    )

    with patch.object(routes, "_authorize_space_ids", new=AsyncMock()), \
         patch.object(routes, "find_active_thread_id", new=AsyncMock(return_value="thread-1")), \
         patch.object(
             routes, "_authorize_workflow_state",
             new=AsyncMock(side_effect=HTTPException(status_code=403, detail="not owned")),
         ):
        result = await routes.find_active_recovery_endpoint(space_id=1, sprint_id=2, request=request)

    assert result is None


async def test_by_sprint_returns_the_thread_when_the_caller_is_the_owner():
    request = _fake_request(user_id="me")
    request.app.state.sprint_recovery_graph.aget_state.return_value = types.SimpleNamespace(
        values={"status": "awaiting_plan_approval", "user_id": "me", "space_id": 1, "plans": []},
        next=("approval",), tasks=(),
    )

    with patch.object(routes, "_authorize_space_ids", new=AsyncMock()), \
         patch.object(routes, "find_active_thread_id", new=AsyncMock(return_value="thread-1")), \
         patch.object(routes, "_authorize_workflow_state", new=AsyncMock()):
        result = await routes.find_active_recovery_endpoint(space_id=1, sprint_id=2, request=request)

    assert result is not None
    assert result.thread_id == "thread-1"
    assert result.status == "awaiting_plan_approval"


def _checkpoint(
    checkpoint_id: str, status: str, committed_actions: dict, approved_plan=None, next_node: str = None,
    clarification_answer: str = None,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        config={"configurable": {"checkpoint_id": checkpoint_id}},
        values={
            "status": status, "committed_actions": committed_actions, "approved_plan": approved_plan,
            "clarification_answer": clarification_answer,
        },
        next=(next_node,) if next_node else (),
    )


def test_real_actions_committed_after_lists_actions_across_a_round_reset_not_just_the_latest_dict():
    """**Found live**: a user's own reaction to the time-travel picker on a thread already 3 rounds
    deep — every checkpoint was selectable with no way to tell "rewinding here is the clean, intended
    case" apart from "rewinding here would set aside real Jira writes this same thread already made in
    a *later* round." A bare count wasn't enough either — found live, the immediate next question was
    "which ones?" — so this lists descriptions, not just a number. `committed_actions` resets to `{}`
    at the start of every new escalation round (see `reevaluate_node`'s "still at risk" branch), so
    reading only the most recent dict would silently miss every action from an earlier round once a
    newer round has reset it — this test builds exactly that shape (round 0 commits 2 actions, resets,
    round 1 commits 1 more) and checks the accumulated list, oldest-to-newest, includes all three.
    """
    from app.sprint_recovery.schemas import RecoveryAction, RecoveryPlan

    plan_r0 = RecoveryPlan(plan_id="p0", name="Round 0", rationale="r", impact_on_goal="n/a", actions=[
        RecoveryAction(action_type="add_comment", target_issue_key="PAY-1", comment_body="r0 a"),
        RecoveryAction(action_type="change_priority", target_issue_key="PAY-2", new_priority="highest"),
    ])
    plan_r1 = RecoveryPlan(plan_id="p1", name="Round 1", rationale="r", impact_on_goal="n/a", actions=[
        RecoveryAction(action_type="move_out_of_sprint", target_issue_key="PAY-3"),
    ])

    # Newest first, matching `list_checkpoint_history`'s own ordering.
    history = [
        _checkpoint("c6", "committing", {"0": "x"}, approved_plan=plan_r1),                  # round 1's own commit
        _checkpoint("c5", "awaiting_plan_approval", {}, approved_plan=None),                  # round 1's plan, pre-approve
        _checkpoint("c4", "diagnosing", {}, approved_plan=None),                              # round 1 diagnosis (reset)
        _checkpoint("c3", "committing", {"0": "x", "1": "y"}, approved_plan=plan_r0),          # round 0's 2nd commit
        _checkpoint("c2", "committing", {"0": "x"}, approved_plan=plan_r0),                    # round 0's 1st commit
        _checkpoint("c1", "awaiting_plan_approval", {}, approved_plan=None),                   # round 0's plan, pre-approve
    ]

    after = routes._real_actions_committed_after(history)

    assert after["c6"] == []  # nothing committed after the very last step
    assert after["c5"] == ["Moved PAY-3 out of the sprint"]
    assert after["c4"] == ["Moved PAY-3 out of the sprint"]
    assert after["c3"] == ["Moved PAY-3 out of the sprint"]  # only round 1's action is after round 0's *2nd* commit
    assert after["c2"] == ["Raised PAY-2 priority to highest", "Moved PAY-3 out of the sprint"]
    # Round 0 had 2 actions total, round 1 had 1 more — rewinding to before round 0 even started must
    # surface all 3, which reading only the latest `committed_actions` dict would miss entirely (that
    # dict only ever holds round 1's single action, having been reset when round 1 began).
    assert after["c1"] == [
        'Commented on PAY-1: "r0 a"', "Raised PAY-2 priority to highest", "Moved PAY-3 out of the sprint",
    ]


async def test_time_travel_endpoint_rejects_a_checkpoint_with_real_actions_after_it():
    """**Found live**: the picker only disables the radio for a locked checkpoint client-side — a
    stale frontend build (hit for real mid-session, an HMR lag) could still submit a locked
    checkpoint_id directly, and nothing server-side stopped it. Reproduced live: rewinding past 6
    already-committed Jira writes silently reset `escalation_round` to 0, so "Attempt 1 of 3" showed up
    on a thread that had actually already burned all 3 attempts — confusing in exactly the way the
    picker was built to prevent. This locks in the same check enforced server-side: a locked
    checkpoint_id must 409, not silently rewind.
    """
    request = _fake_request(user_id="me")
    request.app.state.sprint_recovery_graph.aget_state.return_value = types.SimpleNamespace(
        values={"status": "diagnosing", "user_id": "me", "space_id": 1},
    )
    plan = RecoveryPlan(
        plan_id="p", name="n", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-1", comment_body="hi")],
    )
    locked_history = [
        _checkpoint("newest", "committing", {"0": "x"}, approved_plan=plan, next_node="commit_one_action"),
        _checkpoint("target", "awaiting_plan_approval", {}, next_node="approval"),  # decision before it
    ]

    time_travel_mock = AsyncMock()
    with patch.object(routes, "_authorize_workflow_state", new=AsyncMock()), \
         patch.object(routes, "list_checkpoint_history", new=AsyncMock(return_value=locked_history)), \
         patch.object(routes, "time_travel_resume", new=time_travel_mock):
        req = routes.TimeTravelRequest(checkpointId="target", note="something new came to light")
        try:
            await routes.time_travel_endpoint(thread_id="t1", req=req, request=request)
            assert False, "expected a 409"
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "PAY-1" in exc.detail or "Commented on PAY-1" in exc.detail

    time_travel_mock.assert_not_awaited()


def test_revision_summary_distinguishes_what_was_actually_being_decided():
    """**Found live**: once a thread has been revised more than once, every rewind target rendered as
    the identical generic "Waiting for your decision" — no way to tell revision 1 apart from revision 3
    without clicking each one. This summarizes the plan(s) on the table (or the question asked) so the
    picker can show that difference directly instead of hiding it behind an identical label.
    """
    one_plan = RecoveryPlan(
        plan_id="p1", name="Escalate ownership", rationale="r", impact_on_goal="n/a",
        actions=[
            RecoveryAction(action_type="change_priority", target_issue_key="A-1", new_priority="highest"),
            RecoveryAction(action_type="add_comment", target_issue_key="A-2", comment_body="hi"),
        ],
    )
    two_plans = [
        RecoveryPlan(plan_id="p1", name="Plan A", rationale="r", impact_on_goal="n/a", actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="A-1", comment_body="hi"),
        ]),
        RecoveryPlan(plan_id="p2", name="Plan B", rationale="r", impact_on_goal="n/a", actions=[
            RecoveryAction(action_type="move_out_of_sprint", target_issue_key="A-2"),
        ]),
    ]

    assert routes._revision_summary({"plans": [one_plan]}, "approval") == 'Plan: "Escalate ownership" (2 actions)'
    assert routes._revision_summary({"plans": two_plans}, "approval") == (
        '2 plans to choose from: "Plan A" (1 action); "Plan B" (1 action)'
    )
    assert routes._revision_summary({"plans": []}, "approval") is None
    assert routes._revision_summary(
        {"clarification_question": "Is this genuinely blocked?"}, "clarify",
    ) == 'Question: "Is this genuinely blocked?"'
    assert routes._revision_summary({"clarification_question": None}, "clarify") is None
    assert routes._revision_summary({"plans": [one_plan]}, "commit_one_action") is None


def test_only_the_decision_currently_being_waited_on_is_revisable():
    """**The rule a user stated plainly** after watching three identical-looking "Revision" rows all
    claim to be selectable at once: only the decision this check is *currently waiting on* can be
    changed. An older revision — even one with no Jira writes after it — has already been superseded,
    and rewinding to it produces an outcome indistinguishable from revising the current decision
    (`time_travel_resume` re-enters via the same `clarify` edge and `diagnose_node` re-derives evidence
    from live data either way) while quietly discarding everything in between.
    """
    # Two plan revisions back to back, nothing committed in between (a plain "request different plans"
    # loop) — the older one must still be refused, which the "no real actions after it" check alone
    # would have happily allowed.
    two_revisions = [
        _checkpoint("newer", "awaiting_plan_approval", {}, next_node="approval"),
        _checkpoint("older", "revising", {}, next_node="plan"),
        _checkpoint("oldest", "awaiting_plan_approval", {}, next_node="approval"),
    ]
    assert routes._newest_revisable_checkpoint_id(two_revisions) == "newer"

    # A thread mid-commit: its newest decision point has already been acted on, so nothing is revisable
    # — the honest answer, rather than silently falling through to an older decision point.
    plan = RecoveryPlan(
        plan_id="p", name="n", rationale="r", impact_on_goal="n/a",
        actions=[RecoveryAction(action_type="add_comment", target_issue_key="PAY-1", comment_body="hi")],
    )
    mid_commit = [
        _checkpoint("commit", "committing", {"0": "x"}, approved_plan=plan, next_node="commit_one_action"),
        _checkpoint("approved", "awaiting_plan_approval", {}, next_node="approval"),
        _checkpoint("first", "awaiting_plan_approval", {}, next_node="approval"),
    ]
    assert routes._newest_revisable_checkpoint_id(mid_commit) is None

    # No decision point at all yet (a check still in its very first diagnosis).
    assert routes._newest_revisable_checkpoint_id(
        [_checkpoint("d", "diagnosing", {}, next_node="plan")],
    ) is None


async def test_time_travel_endpoint_rejects_a_superseded_revision_even_with_no_jira_writes_after_it():
    """The half of the rule the "real actions after it" guard alone would have let through — see
    `test_only_the_decision_currently_being_waited_on_is_revisable`. Enforced server-side, not just by
    the picker: found live, a stale frontend build was exactly how the previous gap got hit."""
    request = _fake_request(user_id="me")
    request.app.state.sprint_recovery_graph.aget_state.return_value = types.SimpleNamespace(
        values={"status": "awaiting_plan_approval", "user_id": "me", "space_id": 1},
    )
    history = [
        _checkpoint("newer", "awaiting_plan_approval", {}, next_node="approval"),
        _checkpoint("oldest", "awaiting_plan_approval", {}, next_node="approval"),
    ]

    time_travel_mock = AsyncMock()
    with patch.object(routes, "_authorize_workflow_state", new=AsyncMock()), \
         patch.object(routes, "list_checkpoint_history", new=AsyncMock(return_value=history)), \
         patch.object(routes, "time_travel_resume", new=time_travel_mock):
        req = routes.TimeTravelRequest(checkpointId="oldest", note="new info")
        try:
            await routes.time_travel_endpoint(thread_id="t1", req=req, request=request)
            assert False, "expected a 409"
        except HTTPException as exc:
            assert exc.status_code == 409
            assert "superseded" in exc.detail

    time_travel_mock.assert_not_awaited()


def test_a_human_note_is_attributed_to_the_revision_it_produced_not_every_later_one():
    """**Found live**: a user rewound with the note "we will add 2 extra engineers to this sprint" and
    saw no trace of it anywhere in the timeline — the resulting plan didn't mention engineers, so the
    only visible outcome looked exactly like the input had been thrown away (it hadn't: the note became
    evidence and the resulting hypothesis weighed it explicitly).

    Two traps this locks in. The note first lands on a graph-internal checkpoint (`next=("diagnose",)`)
    that the readable timeline filters out, so it has to be attributed forward to the decision it
    produced. And `clarification_answer` *persists* in state afterwards, so reading the field directly
    would make every later revision keep claiming the same stale note as its own cause.
    """
    history_newest_first = [
        # A later revision, with the note still sitting in state — must NOT claim it.
        _checkpoint("rev4", "awaiting_plan_approval", {}, next_node="approval", clarification_answer="add 2 engineers"),
        _checkpoint("internal2", "diagnosing", {}, next_node="plan", clarification_answer="add 2 engineers"),
        # The revision the note actually produced.
        _checkpoint("rev3", "awaiting_plan_approval", {}, next_node="approval", clarification_answer="add 2 engineers"),
        _checkpoint("internal1", "diagnosing", {}, next_node="plan", clarification_answer="add 2 engineers"),
        # Where the note entered — a time-travel fork, filtered out of the readable timeline.
        _checkpoint("fork", "awaiting_plan_approval", {}, next_node="diagnose", clarification_answer="add 2 engineers"),
        _checkpoint("rev2", "awaiting_plan_approval", {}, next_node="approval"),
    ]

    notes = routes._notes_that_triggered_each_revision(history_newest_first)

    assert notes == {"rev3": "add 2 engineers"}


def test_the_final_action_of_a_plan_is_counted_and_named_despite_its_different_status():
    """**Found live, from a user asking why the rewind lock kept saying "2 changes"**: a 3-action plan
    showed only 2 "In Jira" rows and warned about 2 changes, while all three comments were genuinely in
    Jira (verified against the comments table). `commit_one_action_node` returns
    `status="waiting_reevaluation"` — not `"committing"` — on the step that commits a plan's *final*
    action, so both helpers' `status == "committing"` filter silently dropped the last action of every
    single plan: missing from the audit trail, and undercounted in the guard that decides whether a
    rewind is safe. Keying off the `committed_actions` diff instead of the status string is what makes
    this honest, since the diff is what actually says an action landed.
    """
    plan = RecoveryPlan(
        plan_id="p", name="n", rationale="r", impact_on_goal="n/a",
        actions=[
            RecoveryAction(action_type="add_comment", target_issue_key="A-1", comment_body="one"),
            RecoveryAction(action_type="add_comment", target_issue_key="A-2", comment_body="two"),
            RecoveryAction(action_type="move_out_of_sprint", target_issue_key="A-3"),
        ],
    )
    # Newest first, exactly the shape a real 3-action commit leaves behind — note the final action
    # lands on a `waiting_reevaluation` checkpoint, not a `committing` one.
    history = [
        _checkpoint("done", "waiting_reevaluation", {"0": "x", "1": "y", "2": "z"}, approved_plan=plan,
                    next_node="wait_for_reevaluation"),
        _checkpoint("c2", "committing", {"0": "x", "1": "y"}, approved_plan=plan, next_node="commit_one_action"),
        _checkpoint("c1", "committing", {"0": "x"}, approved_plan=plan, next_node="commit_one_action"),
        _checkpoint("armed", "committing", {}, approved_plan=plan, next_node="commit_one_action"),
        _checkpoint("decision", "awaiting_plan_approval", {}, next_node="approval"),
    ]

    details = routes._committing_step_details(history)
    # All three actions get named, including the last one.
    assert details["c1"] == 'Commented on A-1: "one"'
    assert details["c2"] == 'Commented on A-2: "two"'
    assert details["done"] == "Moved A-3 out of the sprint"
    assert "armed" not in details  # the pre-commit checkpoint added nothing

    after = routes._real_actions_committed_after(history)
    # The decision point that produced the plan must know about all three, not two.
    assert after["decision"] == [
        'Commented on A-1: "one"', 'Commented on A-2: "two"', "Moved A-3 out of the sprint",
    ]
    assert after["done"] == []
