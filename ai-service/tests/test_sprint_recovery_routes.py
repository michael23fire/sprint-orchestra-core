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
