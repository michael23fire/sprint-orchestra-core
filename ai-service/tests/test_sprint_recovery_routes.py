"""Tests for app/api/sprint_recovery_routes.py — currently just the one function worth unit testing
in isolation (no FastAPI TestClient/app.state scaffolding elsewhere in this codebase; the rest of this
router is exercised via graph-level tests plus live verification, same pattern as app/api/routes.py).
"""
from unittest.mock import AsyncMock, patch

from app.api import sprint_recovery_routes as routes


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
