"""Route-level coverage for Plan Epic active-workflow discovery."""
import types
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.api import routes
from app.planning.schemas import EpicDraft, IssueDraft


def _request(values: dict, user_id: str = "user-a"):
    graph = types.SimpleNamespace(
        aget_state=AsyncMock(return_value=types.SimpleNamespace(values=values)),
    )
    state = types.SimpleNamespace(
        rollout_graph=graph,
        epic_rollout_checkpoint_db_url="postgresql://unused",
    )
    return types.SimpleNamespace(
        app=types.SimpleNamespace(state=state),
        headers={"x-user-id": user_id, "x-username": "alice"},
    )


def _pending_values(user_id: str = "user-a") -> dict:
    return {
        "status": "pending_approval",
        "space_id": 7,
        "user_id": user_id,
        "epic": EpicDraft(title="Epic", description="Description", goals=[]),
        "issues": [
            IssueDraft(
                temp_id="1", title="Child", description="Work", issue_type="task",
                labels=[], estimate_story_points=2, estimate_rationale="Small", depends_on=[],
            )
        ],
        "sprint_buckets": [{"sprint_index": 0, "issue_temp_ids": ["1"], "total_points": 2}],
        "sprint_targets": [],
        "committed": {},
        "degraded": False,
        "error": None,
    }


async def test_active_rollout_returns_the_current_users_pending_workflow():
    request = _request(_pending_values())
    with patch.object(routes, "_authorize_space_ids", new=AsyncMock()), \
         patch.object(
             routes, "find_plan_epic_active_thread_id", new=AsyncMock(return_value="thread-1"),
         ), \
         patch.object(routes, "_authorize_workflow_state", new=AsyncMock()):
        result = await routes.find_active_rollout_endpoint(space_id=7, request=request)

    assert result is not None
    assert result.thread_id == "thread-1"
    assert result.status == "pending_approval"
    assert result.plan is not None
    assert result.plan.issues[0].title == "Child"


async def test_active_rollout_does_not_reopen_a_terminal_workflow():
    request = _request({"status": "committed", "space_id": 7, "user_id": "user-a"})
    with patch.object(routes, "_authorize_space_ids", new=AsyncMock()), \
         patch.object(
             routes, "find_plan_epic_active_thread_id", new=AsyncMock(return_value="thread-1"),
         ):
        result = await routes.find_active_rollout_endpoint(space_id=7, request=request)

    assert result is None


async def test_active_rollout_hides_a_thread_that_fails_owner_authorization():
    request = _request(_pending_values(user_id="someone-else"))
    with patch.object(routes, "_authorize_space_ids", new=AsyncMock()), \
         patch.object(
             routes, "find_plan_epic_active_thread_id", new=AsyncMock(return_value="thread-1"),
         ), \
         patch.object(
             routes,
             "_authorize_workflow_state",
             new=AsyncMock(side_effect=HTTPException(status_code=403, detail="not owned")),
         ):
        result = await routes.find_active_rollout_endpoint(space_id=7, request=request)

    assert result is None
