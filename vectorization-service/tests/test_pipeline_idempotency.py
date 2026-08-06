"""Pipeline behaviour with a fake embedder + in-memory store.

The key property under test is idempotency: re-processing the same event (which Kafka's
at-least-once delivery makes routine) must not accumulate duplicate vectors, and deletes must
actually purge — this is what makes the "commit-after-handle, redeliver-on-failure" consumer safe.
"""
from datetime import datetime, timezone

import pytest

from app.config import Settings
from app.ingest.embedder import FakeEmbedder
from app.ingest.pipeline import IngestPipeline
from app.models import (
    CommentIngestionMessage,
    IssueHistoryIngestionMessage,
    IssueIngestionMessage,
    SprintIngestionMessage,
)


class InMemoryStore:
    """Mimics VectorStore's keyed replace/delete semantics with a dict."""

    def __init__(self):
        self.rows = {}  # id -> chunk
        self.issues = {}  # issue_id -> IssueMetadata (the structured side, see migrations/004)
        self.sprints = {}  # sprint_id -> SprintMetadata (see migrations/006)

    async def upsert_sprint(self, meta) -> None:
        self.sprints[meta.sprint_id] = meta

    async def delete_sprint(self, sprint_id) -> int:
        before = len(self.rows)
        self.rows = {
            cid: c for cid, c in self.rows.items()
            if not (c.chunk_type == "sprint" and c.source_id == sprint_id)
        }
        self.sprints.pop(sprint_id, None)
        return before - len(self.rows)

    async def replace_source(self, chunk_type, source_id, chunks, vectors):
        # Delete-then-insert, same as the real store.
        self.rows = {
            cid: c for cid, c in self.rows.items()
            if not (c.chunk_type == chunk_type and c.source_id == source_id)
        }
        for c in chunks:
            self.rows[c.id] = c

    async def delete_source(self, chunk_type, source_id) -> int:
        before = len(self.rows)
        self.rows = {
            cid: c for cid, c in self.rows.items()
            if not (c.chunk_type == chunk_type and c.source_id == source_id)
        }
        return before - len(self.rows)

    async def upsert_issue(self, meta) -> None:
        self.issues[meta.issue_id] = meta

    async def append_issue_change(self, msg) -> None:
        # Mirrors the real store: keyed on jira's history id (idempotent), and a status field_change
        # refreshes the snapshot row's status — first insert only.
        if not hasattr(self, "changes"):
            self.changes = {}
        if msg.history_id in self.changes:
            return
        self.changes[msg.history_id] = msg
        if msg.change_event_type == "field_change" and msg.field_name == "status" and msg.issue_id in self.issues:
            self.issues[msg.issue_id].status = msg.to_value
        if msg.change_event_type == "field_change" and msg.field_name == "priority" and msg.issue_id in self.issues:
            self.issues[msg.issue_id].priority = msg.to_value

    async def delete_issue(self, issue_id) -> int:
        before = len(self.rows)
        self.rows = {cid: c for cid, c in self.rows.items() if c.issue_id != issue_id}
        self.issues.pop(issue_id, None)
        return before - len(self.rows)


def _settings() -> Settings:
    return Settings(embedding_provider="fake", embedding_dim=8)


def _issue_msg(event_type="issue_upserted") -> IssueIngestionMessage:
    return IssueIngestionMessage(
        eventId="e1",
        eventType=event_type,
        emittedAt=datetime.now(timezone.utc),
        issueId=42,
        issueKey="PROJ-42",
        spaceId=7,
        title="Payment service outage",
        description="<p>root cause was a connection pool leak</p>",
        issueType="bug",
        status="done",
        priority="high",
        createdAt=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updatedAt=datetime(2026, 1, 2, tzinfo=timezone.utc),
    )


def _comment_msg(event_type="comment_upserted") -> CommentIngestionMessage:
    return CommentIngestionMessage(
        eventId="e2",
        eventType=event_type,
        emittedAt=datetime.now(timezone.utc),
        commentId=981,
        issueId=42,
        issueKey="PROJ-42",
        spaceId=7,
        content="<p>raising HikariCP max-lifetime fixed it</p>",
    )


@pytest.fixture
def pipeline():
    settings = _settings()
    return IngestPipeline(settings, FakeEmbedder(settings.embedding_dim), InMemoryStore()), settings


async def test_issue_upsert_is_idempotent(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    first = dict(pipe._store.rows)
    assert len(first) == 1

    # Reprocessing the identical event (Kafka redelivery) must not duplicate.
    await pipe.handle_issue(_issue_msg())
    assert len(pipe._store.rows) == 1
    assert set(pipe._store.rows) == set(first)


async def test_attachment_chunks_are_tagged_with_their_source_filename(pipeline):
    # An attachment's parsed content (a CSV row, a PDF's body text) essentially never mentions its own
    # filename or issue key naturally — found live, unlike issue text (which handle_issue prepends the
    # key into before chunking). Contextual retrieval's LLM-written sentence is optional (off by
    # default) so it can't be the only thing carrying this — this deterministic tag must fire for
    # every attachment chunk, single- or multi-chunk, contextual retrieval on or off.
    pipe, _ = pipeline
    chunks = await pipe._chunks(
        "Short attachment body.",
        chunk_type="attachment", issue_id=1, issue_key="ATC-1", space_id=1, source_id=99,
        key_prefix="attachment:99", source_label="report.pdf",
    )
    assert len(chunks) == 1
    assert chunks[0].content.startswith("[Attachment: report.pdf — issue ATC-1]")
    assert "Short attachment body." in chunks[0].content


async def test_pdf_attachment_chunks_keep_structured_page_metadata_and_a_readable_prefix(pipeline):
    pipe, _ = pipeline
    chunks = await pipe._chunks(
        "Invoice total: $42.",
        chunk_type="attachment", issue_id=1, issue_key="ATC-1", space_id=1, source_id=99,
        key_prefix="attachment:99", source_label="invoice.pdf", page_number=2,
        force_numbered_ids=True,
    )

    assert len(chunks) == 1
    assert chunks[0].id == "attachment:99#0"
    assert chunks[0].page_number == 2
    assert chunks[0].content.startswith("[Attachment: invoice.pdf — issue ATC-1 — PDF page 2]")


async def test_attachment_chunks_keep_native_non_pdf_provenance(pipeline):
    pipe, _ = pipeline
    chunks = await pipe._chunks(
        "Revenue by region.",
        chunk_type="attachment", issue_id=1, issue_key="ATC-1", space_id=1, source_id=99,
        key_prefix="attachment:99", source_label="revenue.xlsx",
        provenance={"source_type": "xlsx", "sheet_name": "Q2 Revenue"},
        force_numbered_ids=True,
    )

    assert chunks[0].page_number is None
    assert chunks[0].provenance == {"source_type": "xlsx", "sheet_name": "Q2 Revenue"}
    assert chunks[0].content.startswith(
        "[Attachment: revenue.xlsx — issue ATC-1 — XLSX sheet Q2 Revenue]"
    )


async def test_comment_chunks_are_always_tagged_even_the_first_one(pipeline):
    # Found live: unlike issue text, a comment's raw text (html_to_text(msg.content)) has NO issue_key
    # prefix at all — confirmed against a real comment in the corpus, chunk #0 read as plain prose with
    # zero mention of which issue it's on. So comments are tagged unconditionally, not just past
    # chunk #0 (contrast with the issue case below).
    pipe, _ = pipeline
    chunks = await pipe._chunks(
        "The fix landed in the retry handler.",
        chunk_type="comment", issue_id=1, issue_key="ATC-1", space_id=1, source_id=5,
        key_prefix="comment:5",
    )
    assert len(chunks) == 1
    assert chunks[0].content == "[Comment on ATC-1]\nThe fix landed in the retry handler."


async def test_issue_chunk_zero_is_not_tagged_but_later_chunks_are():
    # handle_issue prepends f"{issue_key} {body}" into the text BEFORE chunking (see handle_issue), so
    # chunk #0 always naturally starts with its own key already — tagging it too would be pure
    # redundant noise. But chunk #1+ (past the split point) has lost that natural prefix — found live,
    # ATC-43's own body is long enough to produce exactly this case in the real corpus. Force a split
    # here with a small chunk_size so the test doesn't depend on a huge fixture string.
    settings = Settings(embedding_provider="fake", embedding_dim=8, chunk_size_tokens=10, chunk_overlap_tokens=2)
    pipe = IngestPipeline(settings, FakeEmbedder(settings.embedding_dim), InMemoryStore())
    long_text = "ATC-1 " + " ".join(f"word{i}" for i in range(40))

    chunks = await pipe._chunks(
        long_text,
        chunk_type="issue", issue_id=1, issue_key="ATC-1", space_id=1, source_id=1,
        key_prefix="issue:1",
    )

    assert len(chunks) > 1
    assert not chunks[0].content.startswith("[Issue ATC-1]")
    assert all(c.content.startswith("[Issue ATC-1]") for c in chunks[1:])


async def test_sprint_chunks_are_always_tagged(pipeline):
    # A sprint's goal text (html_to_text(msg.goal)) has no sprint_name prefix either — same gap as
    # comments, for the same reason (see handle_sprint: the goal is embedded on its own).
    pipe, _ = pipeline
    chunks = await pipe._chunks(
        "Make the storefront accessible and observable.",
        chunk_type="sprint", issue_id=70, issue_key="Sprint 7", space_id=1, source_id=70,
        key_prefix="sprint:70",
    )
    assert chunks[0].content == "[Sprint Sprint 7]\nMake the storefront accessible and observable."


async def test_html_is_stripped_before_storing(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    (chunk,) = pipe._store.rows.values()
    assert "<p>" not in chunk.content
    assert "connection pool leak" in chunk.content


async def test_subtask_embeds_parent_context(pipeline):
    # Case Study 13/14 (docs/RAG_ACCURACY_CASE_STUDIES.md): a subtask's own text is often too terse to
    # be findable or correctly characterized on its own — embedding the parent's key+title directly
    # into the subtask's own chunk means that context doesn't depend on the parent's chunk also
    # ranking into the same top-K.
    pipe, _ = pipeline
    msg = _issue_msg()
    msg = msg.model_copy(update={
        "parent_issue_id": 43, "parent_key": "ATC-43",
        "parent_title": "Checkout can create two orders from a double click",
    })
    await pipe.handle_issue(msg)
    (chunk,) = pipe._store.rows.values()
    assert "Subtask of ATC-43" in chunk.content
    assert "Checkout can create two orders from a double click" in chunk.content
    assert "connection pool leak" in chunk.content  # the issue's own body is still there too


async def test_non_subtask_issue_has_no_parent_prefix(pipeline):
    # No parent on the message (the common case: not every issue is a subtask) must not add any
    # "Subtask of" text — regression guard for the branch added alongside the above.
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    (chunk,) = pipe._store.rows.values()
    assert "Subtask of" not in chunk.content


async def test_comment_delete_purges_only_that_comment(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    await pipe.handle_comment(_comment_msg())
    assert len(pipe._store.rows) == 2

    await pipe.handle_comment(_comment_msg("comment_deleted"))
    remaining = list(pipe._store.rows.values())
    assert len(remaining) == 1
    assert remaining[0].chunk_type == "issue"


async def test_issue_delete_purges_issue_and_its_comments(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    await pipe.handle_comment(_comment_msg())
    assert len(pipe._store.rows) == 2

    await pipe.handle_issue(_issue_msg("issue_deleted"))
    assert pipe._store.rows == {}


async def test_blank_issue_clears_stale_vectors(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    assert len(pipe._store.rows) == 1

    blank = _issue_msg()
    blank.title = None
    blank.description = ""
    await pipe.handle_issue(blank)
    assert pipe._store.rows == {}


async def test_issue_upsert_records_structured_metadata(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    meta = pipe._store.issues[42]
    assert meta.issue_key == "PROJ-42"
    assert meta.space_id == 7
    assert meta.issue_type == "bug"
    assert meta.status == "done"
    assert meta.title == "Payment service outage"
    # Regression guard: priority must be carried on the upsert path itself, not rely solely on the
    # history-stream self-heal (see append_issue_change's "priority" branch) — most issues have
    # their priority set once at creation and never changed again, so a history-only sync would
    # never observe the initial value at all. Found live against real AtlasCart data before this
    # field was added here (docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 23's follow-up).
    assert meta.priority == "high"


async def test_blank_issue_still_records_metadata_so_it_is_counted(pipeline):
    # The key reason metadata upsert is unconditional: an issue with no embeddable text produces zero
    # chunks, but it still exists and must still be counted by /issues/query. If metadata upsert were
    # gated on having chunks, empty-description issues would silently vanish from counts/filters.
    pipe, _ = pipeline
    blank = _issue_msg()
    blank.title = None
    blank.description = ""
    await pipe.handle_issue(blank)
    assert pipe._store.rows == {}  # nothing embedded
    assert 42 in pipe._store.issues  # but still recorded for structured queries


async def test_issue_delete_purges_structured_metadata(pipeline):
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    assert 42 in pipe._store.issues

    await pipe.handle_issue(_issue_msg("issue_deleted"))
    assert 42 not in pipe._store.issues


def _history_msg(history_id=900, field="status", from_v="done", to_v="in_progress") -> IssueHistoryIngestionMessage:
    return IssueHistoryIngestionMessage(
        eventId=f"e-h-{history_id}",
        eventType="issue_history_added",
        emittedAt=datetime.now(timezone.utc),
        historyId=history_id,
        issueId=42,
        issueKey="PROJ-42",
        spaceId=7,
        changeEventType="field_change",
        fieldName=field,
        fromValue=from_v,
        toValue=to_v,
        actorName="Alex",
    )


async def test_history_event_appends_change_and_refreshes_snapshot_status(pipeline):
    # A status-only edit never fires a content event (no re-embed) — the history stream is the ONLY
    # way the snapshot learns about it, so it must both append the change and update the status.
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())  # snapshot starts at status=done
    await pipe.handle_history(_history_msg(field="status", from_v="done", to_v="in_progress"))

    assert 900 in pipe._store.changes  # transition preserved, not overwritten
    assert pipe._store.issues[42].status == "in_progress"  # snapshot refreshed


async def test_history_event_appends_change_and_refreshes_snapshot_priority(pipeline):
    # A priority-only edit never fires a content event either (priority isn't embedded text), so this
    # history stream is what keeps the snapshot current for a LATER edit — the initial value at
    # creation comes from the upsert path instead (see test_issue_upsert_records_structured_metadata),
    # since most issues get their priority set once and never changed again. Both paths are exercised
    # here: upsert sets the initial "high", then a genuine edit changes it to "urgent".
    # See migrations/008 and VectorStore.append_issue_change's "priority" branch.
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())  # snapshot starts at priority="high" via the upsert path
    assert pipe._store.issues[42].priority == "high"
    await pipe.handle_history(_history_msg(field="priority", from_v="high", to_v="urgent"))

    assert 900 in pipe._store.changes
    assert pipe._store.issues[42].priority == "urgent"


async def test_history_event_is_idempotent_on_history_id(pipeline):
    # Kafka at-least-once redelivery: replaying the same history row must not duplicate the change
    # record or re-apply a stale status over a newer one.
    pipe, _ = pipeline
    await pipe.handle_issue(_issue_msg())
    await pipe.handle_history(_history_msg(history_id=901, to_v="in_progress"))
    await pipe.handle_history(_history_msg(history_id=902, from_v="in_progress", to_v="blocked"))
    # ... and now 901 is redelivered:
    await pipe.handle_history(_history_msg(history_id=901, to_v="in_progress"))

    assert len(pipe._store.changes) == 2
    assert pipe._store.issues[42].status == "blocked"  # stale redelivery did NOT regress it


def _sprint_msg(event_type="sprint_upserted") -> SprintIngestionMessage:
    return SprintIngestionMessage(
        eventId="e-s1",
        eventType=event_type,
        emittedAt=datetime.now(timezone.utc),
        sprintId=501,
        sprintName="Sprint 7",
        spaceId=7,
        goal="Prove the storefront is accessible, observable, and ready for a larger public beta.",
        status="active",
        completedPoints=12,
        finalScopePoints=20,
    )


async def test_sprint_upsert_records_metadata_and_embeds_goal(pipeline):
    pipe, _ = pipeline
    await pipe.handle_sprint(_sprint_msg())

    meta = pipe._store.sprints[501]
    assert meta.sprint_name == "Sprint 7"
    assert meta.status == "active"
    assert meta.completed_points == 12

    (chunk,) = pipe._store.rows.values()
    assert chunk.chunk_type == "sprint"
    # No parent issue for a sprint chunk — issue_id/issue_key are reused as the sprint's own
    # id/name (see Chunk's docstring in app/models.py), not left null or pointing at a real issue.
    assert chunk.issue_id == 501
    assert chunk.issue_key == "Sprint 7"
    assert "accessible" in chunk.content


async def test_sprint_upsert_is_idempotent(pipeline):
    pipe, _ = pipeline
    await pipe.handle_sprint(_sprint_msg())
    await pipe.handle_sprint(_sprint_msg())
    assert len(pipe._store.rows) == 1
    assert len(pipe._store.sprints) == 1


async def test_sprint_delete_purges_metadata_and_goal_chunk(pipeline):
    pipe, _ = pipeline
    await pipe.handle_sprint(_sprint_msg())
    assert 501 in pipe._store.sprints
    assert len(pipe._store.rows) == 1

    await pipe.handle_sprint(_sprint_msg("sprint_deleted"))
    assert 501 not in pipe._store.sprints
    assert pipe._store.rows == {}


async def test_blank_sprint_goal_produces_no_chunk_but_still_records_metadata(pipeline):
    # Same "structured half is unconditional" contract as issues (test_blank_issue_still_records_
    # metadata_so_it_is_counted above): a future sprint with no goal written yet must still be
    # counted by /sprints/query, even though there's nothing to embed.
    pipe, _ = pipeline
    blank = _sprint_msg()
    blank.goal = None
    await pipe.handle_sprint(blank)
    assert pipe._store.rows == {}
    assert 501 in pipe._store.sprints
