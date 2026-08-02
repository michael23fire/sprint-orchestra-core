"""Consumer decision logic: what commits, what skips (poison), what rewinds (transient).

Exercises `_process` directly with a fake record + fake pipeline — no real Kafka/DB needed. This is
the failure-mode contract that makes at-least-once safe: transient errors must rewind (return False,
never crash), structural errors must skip (return True), success must commit (return True).
"""
import json
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.kafka.consumer import IngestionConsumer


def _record(payload: dict):
    return SimpleNamespace(
        value=json.dumps(payload).encode(), offset=0, topic="jira.content.ingestion", partition=0
    )


class FakePipeline:
    def __init__(self, exc: Exception | None = None):
        self.exc = exc
        self.calls = 0

    async def handle_issue(self, msg):
        self.calls += 1
        if self.exc:
            raise self.exc

    async def handle_history(self, msg):
        self.history_calls = getattr(self, "history_calls", 0) + 1

    async def handle_sprint(self, msg):
        self.sprint_calls = getattr(self, "sprint_calls", 0) + 1

    async def handle_comment(self, msg):  # pragma: no cover - unused here
        ...

    async def handle_attachment(self, msg):  # pragma: no cover - unused here
        ...


def _consumer(pipeline) -> IngestionConsumer:
    return IngestionConsumer(Settings(embedding_provider="fake"), pipeline)


VALID_ISSUE = {
    "eventId": "e1", "eventType": "issue_upserted", "emittedAt": "2026-03-01T00:00:00Z",
    "issueId": 1, "issueKey": "X-1", "spaceId": 1, "title": "t", "description": "d",
}


async def test_success_commits():
    c = _consumer(FakePipeline())
    assert await c._process(_record(VALID_ISSUE)) is True
    assert c.stats.processed == 1 and c.stats.failed == 0


async def test_issue_history_added_routes_to_history_not_issue_handler():
    # Regression pin for a dispatch-ordering trap: "issue_history_added" startswith("issue_"), so a
    # prefix-only router would send it to handle_issue and fail validation against
    # IssueIngestionMessage. It must hit the dedicated history handler.
    p = FakePipeline()
    c = _consumer(p)
    record = _record({
        "eventId": "h1", "eventType": "issue_history_added", "emittedAt": "2026-03-01T00:00:00Z",
        "historyId": 5, "issueId": 1, "issueKey": "X-1", "spaceId": 1,
        "changeEventType": "field_change", "fieldName": "status",
        "fromValue": "done", "toValue": "in_progress",
    })
    assert await c._process(record) is True
    assert getattr(p, "history_calls", 0) == 1
    assert p.calls == 0  # handle_issue untouched


async def test_transient_error_rewinds_not_crashes():
    # A DB/embedding-API style failure must return False (rewind + retry), not raise.
    c = _consumer(FakePipeline(RuntimeError("embedding API down")))
    assert await c._process(_record(VALID_ISSUE)) is False
    assert c.stats.failed == 1 and c.stats.processed == 0
    assert "embedding API down" in c.stats.last_error


async def test_malformed_payload_is_skipped_as_poison():
    # Missing required fields -> pydantic ValidationError -> poison skip (commit, don't retry forever).
    c = _consumer(FakePipeline())
    bad = {"eventType": "issue_upserted"}  # missing issueId/issueKey/...
    assert await c._process(_record(bad)) is True
    assert c.stats.skipped_poison == 1 and c.stats.failed == 0


async def test_unparseable_bytes_are_skipped():
    c = _consumer(FakePipeline())
    rec = SimpleNamespace(value=b"not json{", offset=0, topic="t", partition=0)
    assert await c._process(rec) is True
    assert c.stats.skipped_poison == 1


async def test_sprint_upserted_routes_to_sprint_handler():
    p = FakePipeline()
    c = _consumer(p)
    rec = _record({
        "eventId": "e1", "eventType": "sprint_upserted", "emittedAt": "2026-03-01T00:00:00Z",
        "sprintId": 1, "sprintName": "Sprint 1", "spaceId": 1, "goal": "Ship the thing",
    })
    assert await c._process(rec) is True
    assert getattr(p, "sprint_calls", 0) == 1
    assert p.calls == 0  # handle_issue untouched, same dispatch-isolation check as the history test


async def test_unknown_event_type_is_skipped_not_processed():
    # "widget_" matches none of the routed prefixes (issue_/comment_/attachment_/sprint_/
    # issue_history_added) — genuinely unrecognized, unlike e.g. "sprint_started", which would now
    # match the sprint_ prefix (just an invalid literal within it, a different failure mode).
    c = _consumer(FakePipeline())
    rec = _record({**VALID_ISSUE, "eventType": "widget_created"})
    assert await c._process(rec) is True
    assert c.stats.skipped_poison == 1 and c.stats.processed == 0
