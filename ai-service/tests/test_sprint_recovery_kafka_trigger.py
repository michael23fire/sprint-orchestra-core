"""Tests for app/sprint_recovery/kafka_trigger.py — the space + sprint/issue-key relevance filtering.

**Found live, twice**: `_handle` used to loop over every sprint in `_waiting_by_sprint` regardless of
space (a space-A event could resume a space-B workflow), and even within a space, regardless of whether
the changed issue actually had anything to do with a given waiting workflow's sprint. These tests lock
in both fixes rather than just checking "trigger_reevaluation got called at some point."
"""
import json
from unittest.mock import AsyncMock, patch

from app.sprint_recovery.kafka_trigger import SprintRecoveryKafkaTrigger


def _trigger() -> SprintRecoveryKafkaTrigger:
    return SprintRecoveryKafkaTrigger(
        bootstrap_servers="localhost:9092", topic="jira.content.ingestion",
        group_id="ai-service-sprint-recovery", compiled_graph=object(),
    )


async def test_event_only_resumes_waiting_workflows_in_the_same_space():
    trigger = _trigger()
    trigger.register_waiting(space_id=5000014, sprint_id=7, thread_id="thread-a")
    trigger.register_waiting(space_id=5000099, sprint_id=99, thread_id="thread-b")

    reevaluate_mock = AsyncMock(return_value={"status": "recovered", "space_id": 5000014, "sprint_name": "Sprint 7"})
    with patch("app.sprint_recovery.graph.trigger_reevaluation", new=reevaluate_mock):
        await trigger._handle(json.dumps({"issueKey": "ATC-77", "spaceId": 5000014, "sprintId": 7}).encode())

    reevaluate_mock.assert_awaited_once()
    assert reevaluate_mock.await_args.args[1] == "thread-a"
    assert trigger.workflows_triggered == 1
    # the other space's waiting thread is untouched — still registered, never triggered
    assert "thread-b" in trigger._waiting_by_sprint[99]


async def test_event_for_a_space_with_nothing_waiting_triggers_nothing():
    trigger = _trigger()
    trigger.register_waiting(space_id=5000014, sprint_id=7, thread_id="thread-a")

    reevaluate_mock = AsyncMock()
    with patch("app.sprint_recovery.graph.trigger_reevaluation", new=reevaluate_mock):
        await trigger._handle(json.dumps({"issueKey": "OTHER-1", "spaceId": 999999, "sprintId": 1}).encode())

    reevaluate_mock.assert_not_called()
    assert trigger.workflows_triggered == 0
    assert "thread-a" in trigger._waiting_by_sprint[7]


async def test_event_for_an_unrelated_issue_in_the_same_space_and_sprint_id_mismatch_is_skipped():
    """Same space, but the event's sprintId belongs to a different sprint in that space, and the issue
    isn't one this thread is tracking — must not trigger just because the space matched."""
    trigger = _trigger()
    trigger.register_waiting(space_id=1, sprint_id=7, thread_id="thread-a", tracked_issue_keys={"ATC-77"})

    reevaluate_mock = AsyncMock()
    with patch("app.sprint_recovery.graph.trigger_reevaluation", new=reevaluate_mock):
        await trigger._handle(json.dumps({"issueKey": "OTHER-9", "spaceId": 1, "sprintId": 12}).encode())

    reevaluate_mock.assert_not_called()
    assert trigger.workflows_triggered == 0


async def test_move_out_of_sprint_self_trigger_matches_via_tracked_issue_key_not_sprint_id():
    """**Found live**: when this workflow's own committed `move_out_of_sprint` action is what changed
    the issue, the resulting event's sprintId is null (the issue just left the sprint) — a sprintId-only
    match would miss the single most important self-triggered case. Must still match via the tracked
    issue-key fallback.
    """
    trigger = _trigger()
    trigger.register_waiting(space_id=1, sprint_id=7, thread_id="thread-a", tracked_issue_keys={"ATC-80"})

    reevaluate_mock = AsyncMock(return_value={"status": "recovered", "space_id": 1, "sprint_name": "Sprint 7"})
    with patch("app.sprint_recovery.graph.trigger_reevaluation", new=reevaluate_mock):
        await trigger._handle(json.dumps({"issueKey": "ATC-80", "spaceId": 1, "sprintId": None}).encode())

    reevaluate_mock.assert_awaited_once()
    assert reevaluate_mock.await_args.args[1] == "thread-a"
