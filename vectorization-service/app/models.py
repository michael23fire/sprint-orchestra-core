"""Wire models for inbound Kafka payloads and the internal chunk representation.

jira-backend serializes with Jackson, so JSON keys are camelCase (``issueId``, ``issueKey``). These
models accept camelCase via an alias generator while exposing snake_case attributes in Python.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class IssueIngestionMessage(_CamelModel):
    event_id: str
    event_type: Literal["issue_upserted", "issue_deleted"]
    emitted_at: datetime
    issue_id: int
    issue_key: str
    space_id: int
    title: Optional[str] = None
    description: Optional[str] = None
    # Structured metadata for the /issues/query path (counting, type/status/date filtering, recency).
    # Optional because the current jira-backend Kafka producer doesn't emit them yet — when absent the
    # issue is still recorded (so it's counted), just with null type/status/timestamps until the
    # producer is extended (or a backfill fills them from source). See migrations/004.
    issue_type: Optional[str] = None
    status: Optional[str] = None
    # Which sprint the issue is CURRENTLY in — null means backlog (no sprint), not "unknown"; jira-
    # backend sends both the id (for filtering) and the name (for display without a second lookup).
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None
    # Null unless this issue is a subtask. A denormalized snapshot of the PARENT's key/title (same
    # idiom as sprint_id/sprint_name above) — see IngestPipeline.handle_issue for why this gets
    # embedded into the subtask's own chunk text instead of staying pure metadata.
    parent_issue_id: Optional[int] = None
    parent_key: Optional[str] = None
    parent_title: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class SprintIngestionMessage(_CamelModel):
    """One sprint's structured metadata + goal text, from jira-backend's SprintContentChangedEvent.

    Unlike an issue, a sprint has no comment/attachment chunks — its ``goal`` is the only embeddable
    text, and it's the whole reason this message exists: without it, "which sprint's goal was about
    accessibility" has nothing real to search against (see SprintContentChangedEvent's docstring).
    """

    event_id: str
    event_type: Literal["sprint_upserted", "sprint_deleted"]
    emitted_at: datetime
    sprint_id: int
    sprint_name: str
    space_id: int
    goal: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = None
    initial_committed_points: Optional[int] = None
    initial_completed_points: Optional[int] = None
    final_scope_points: Optional[int] = None
    completed_points: Optional[int] = None
    initial_issue_count: Optional[int] = None
    completed_issue_count: Optional[int] = None
    final_issue_count: Optional[int] = None
    unestimated_issue_count: Optional[int] = None


class IssueHistoryIngestionMessage(_CamelModel):
    """One issue-history entry from jira-backend (``eventType=issue_history_added``).

    ``history_id`` is jira-backend's issue_history primary key and becomes this service's
    ``issue_changes.id`` — the idempotency key that makes at-least-once delivery and backfill overlap
    safe. ``change_event_type`` is the history row's own kind (field_change / issue_created / ...),
    deliberately distinct from the envelope's ``event_type`` discriminator.
    """

    event_id: str
    event_type: Literal["issue_history_added"]
    emitted_at: datetime
    history_id: int
    issue_id: int
    issue_key: str
    space_id: int
    change_event_type: str
    field_name: Optional[str] = None
    from_value: Optional[str] = None
    to_value: Optional[str] = None
    description: Optional[str] = None
    actor_name: Optional[str] = None


class CommentIngestionMessage(_CamelModel):
    event_id: str
    event_type: Literal["comment_upserted", "comment_deleted"]
    emitted_at: datetime
    comment_id: int
    issue_id: int
    issue_key: str
    space_id: int
    content: Optional[str] = None


class AttachmentIngestionMessage(_CamelModel):
    event_id: str
    # jira-backend emits "attachment_uploaded" / "attachment_deleted".
    event_type: Literal["attachment_uploaded", "attachment_deleted"]
    emitted_at: datetime
    attachment_id: int
    issue_id: int
    issue_key: str
    space_id: int
    filename: Optional[str] = None
    mime_type: Optional[str] = None
    byte_size: Optional[int] = None
    storage_backend: Optional[str] = None
    bucket: Optional[str] = None
    storage_key: Optional[str] = None
    storage_uri: Optional[str] = None


ChunkType = Literal["issue", "comment", "attachment", "sprint"]


@dataclass(slots=True)
class SearchHit:
    """One ranked retrieval result, returned by VectorStore.search_* and the /search endpoint.

    ``retrievers`` names which signal(s) surfaced this chunk (``["vector"]``, ``["lexical"]``, or
    both) — useful for demoing hybrid fusion and for debugging why a result did or didn't rank.
    """

    id: str
    chunk_type: ChunkType
    issue_id: int
    issue_key: str
    space_id: int
    source_id: int
    content: str
    score: float
    retrievers: List[str]


@dataclass(slots=True)
class IssueMetadata:
    """One issue's structured metadata, upserted into the `issues` table on every issue event.

    Separate from `Chunk` on purpose: a chunk is a unit of *text* for semantic retrieval, this is a
    unit of *fact* for structured/aggregate queries (count, filter, order-by-recency). See
    migrations/004 for why these live in their own table rather than as columns on `chunks`.
    """

    issue_id: int
    issue_key: str
    space_id: int
    issue_type: Optional[str] = None
    status: Optional[str] = None
    # Null until a priority field_change history event self-heals it (see migrations/008 and
    # VectorStore.append_issue_change) — not carried by IssueIngestionMessage itself, same "nullable
    # until the producer emits it or a history event self-heals it" convention as status/sprint above.
    priority: Optional[str] = None
    sprint_id: Optional[int] = None
    sprint_name: Optional[str] = None
    title: Optional[str] = None
    # Null unless this issue is a subtask — see migrations/007. Denormalized (the parent's KEY, not a
    # FK to issue_id) for the same reason IssueIngestionMessage.parent_key already is: a plain,
    # queryable "is X actually the parent of these subtasks" fact, not guessed from prose. See
    # docs/RAG_ACCURACY_CASE_STUDIES.md Case Study 17.
    parent_key: Optional[str] = None
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


@dataclass(slots=True)
class IssueRow:
    """One row returned by a structured /issues/query — the issue-level view, not a chunk."""

    issue_id: int
    issue_key: str
    space_id: int
    issue_type: Optional[str]
    status: Optional[str]
    priority: Optional[str]
    sprint_id: Optional[int]
    sprint_name: Optional[str]
    title: Optional[str]
    parent_key: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]


@dataclass(slots=True)
class SprintMetadata:
    """One sprint's structured metadata, upserted into the `sprints` table on every sprint event."""

    sprint_id: int
    sprint_name: str
    space_id: int
    goal: Optional[str] = None
    start_date: Optional[date] = None
    end_date: Optional[date] = None
    status: Optional[str] = None
    initial_committed_points: Optional[int] = None
    initial_completed_points: Optional[int] = None
    final_scope_points: Optional[int] = None
    completed_points: Optional[int] = None
    initial_issue_count: Optional[int] = None
    completed_issue_count: Optional[int] = None
    final_issue_count: Optional[int] = None
    unestimated_issue_count: Optional[int] = None


@dataclass(slots=True)
class SprintRow:
    """One row returned by a structured /sprints/query."""

    sprint_id: int
    sprint_name: str
    space_id: int
    goal: Optional[str]
    start_date: Optional[date]
    end_date: Optional[date]
    status: Optional[str]
    initial_committed_points: Optional[int]
    initial_completed_points: Optional[int]
    final_scope_points: Optional[int]
    completed_points: Optional[int]
    initial_issue_count: Optional[int]
    completed_issue_count: Optional[int]
    final_issue_count: Optional[int]
    unestimated_issue_count: Optional[int]


@dataclass(slots=True)
class SprintQueryResult:
    """Aggregate + list result of a structured sprint query — same honesty contract as
    IssueQueryResult: ``total_count`` counts every match, ``rows`` is only the limited sample."""

    total_count: int
    counts_by_status: dict[str, int]
    rows: List[SprintRow]


@dataclass(slots=True)
class IssueChange:
    """One append-only change record (see migrations/005) — a transition, not a state."""

    id: int
    issue_id: int
    issue_key: str
    space_id: int
    event_type: str
    field_name: Optional[str]
    from_value: Optional[str]
    to_value: Optional[str]
    description: Optional[str]
    actor_name: Optional[str]
    changed_at: datetime


@dataclass(slots=True)
class IssueChangeQueryResult:
    """Aggregate + list result of a history query: exact match count plus the ordered sample."""

    total_count: int
    changes: List[IssueChange]


@dataclass(slots=True)
class IssueComment:
    """One comment chunk, returned complete and unranked — not a semantic-search hit.

    Same underlying `chunks` row a search hit would return, but fetched by exact issue_key match
    rather than embedding similarity: the deterministic "read everything on this issue" fallback for
    when a specific detail (a reason, a decision) might be in a comment that didn't rank in a top-K
    semantic search. See app/agent VectorStore.get_issue_comments docstring for the full reasoning.
    """

    id: str
    issue_id: int
    issue_key: str
    source_id: int
    content: str
    chunk_index: int


@dataclass(slots=True)
class IssueCommentsQueryResult:
    """Aggregate + list result of a get_issue_comments fetch: exact count plus every comment
    (up to `limit`) for the requested issues, ordered by issue then chunk_index."""

    total_count: int
    comments: List[IssueComment]


@dataclass(slots=True)
class IssueDetail:
    """One issue's own title+description chunk, returned complete and unranked — not a
    semantic-search hit.

    The `chunk_type='issue'` counterpart to `IssueComment`: same "read it deterministically by key,
    don't trust top-K ranking" rationale, but for the issue's OWN record instead of its comments.
    Found live: an issue with many comments (e.g. an incident with a dozen follow-ups) can have its
    own single title+description chunk rank below several of its comments in both lexical (no length
    normalization in `ts_rank_cd` — a short chunk mentioning the key once doesn't out-rank comments
    that also mention it) and vector search, so "what does issue X actually say" could come back
    empty even though the content is right there in `chunks`. See
    VectorStore.get_issue_details docstring for the full reasoning.
    """

    id: str
    issue_id: int
    issue_key: str
    source_id: int
    content: str
    chunk_index: int


@dataclass(slots=True)
class IssueDetailsQueryResult:
    """Aggregate + list result of a get_issue_details fetch: exact count plus every issue-body
    chunk (up to `limit`) for the requested issues, ordered by issue then chunk_index."""

    total_count: int
    details: List[IssueDetail]


@dataclass(slots=True)
class IssueQueryResult:
    """Aggregate + list result of a structured issue query. ``total_count`` counts every match
    (ignoring the row `limit`); ``counts_by_type`` / ``counts_by_status`` break that total down; and
    ``rows`` is the (ordered, limited) sample of matching issues for listing."""

    total_count: int
    counts_by_type: dict[str, int]
    counts_by_status: dict[str, int]
    rows: List[IssueRow]


@dataclass(slots=True)
class Chunk:
    """A single unit that gets embedded and stored as one row in the vector store.

    ``id`` is a deterministic key (``issue:{id}``, ``comment:{id}``, ``attachment:{id}#{index}``) so
    re-processing an upsert overwrites the same row instead of accumulating stale duplicates.
    ``source_id`` links back to the originating issue/comment/attachment so a semantic-search hit can
    be resolved to a clickable citation; ``space_id`` powers the same per-space permission filter the
    FTS path already uses in jira-backend.

    For ``chunk_type="sprint"`` specifically: a sprint has no parent issue, so ``issue_id``/
    ``issue_key`` are reused as the sprint's own id/name (matching ``source_id``) rather than left
    null — the `chunks` schema has no separate "owning entity" column, and adding one for a single
    chunk_type wasn't worth a schema change when the existing columns already mean "what is this
    chunk about," which for a sprint chunk is the sprint itself.
    """

    id: str
    chunk_type: ChunkType
    issue_id: int
    issue_key: str
    space_id: int
    source_id: int
    chunk_index: int
    content: str
